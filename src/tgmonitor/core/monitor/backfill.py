"""周期补拉 mixin — `MonitorService` 的 `_backfill` / `_backfill_all` /
`_backfill_loop` / `_handle_connection_state`。

2026-10-10 v1.12.x plan-batch2 目标 2 从 `MonitorService` 抽出:
周期补拉是独立子系统(自管 task + 自管周期 + 自管 unavailable 标记),
与「主消息流」/「bus 事件订阅」解耦。`_handle_connection_state` 紧
耦合在补拉上(reconnect → 立即 kick),也一起搬过来。

设计:同 `WhitelistMixin` — mixin 期望宿主类持有
`self.client / storage / bus / _whitelist / _unavailable_channels /
_stop / _backfill_task`。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from tgmonitor.core.events import ConnectionStateChanged, ErrorOccurred
from tgmonitor.core.telegram.tdlib_channels import ChatUnavailableError
from tgmonitor.core.telegram.tdlib_errors import ClientClosingError

if TYPE_CHECKING:
    from tgmonitor.core.events import EventBus
    from tgmonitor.core.storage.repository import StorageRepository
    from tgmonitor.core.telegram.client import TelegramClient

log = logging.getLogger(__name__)


class BackfillMixin:
    """周期补拉子模块。集中 4 个 method:

    - `_backfill` — 补拉单频道(TDLib `getChatHistory` → `_handle`)
    - `_backfill_all` — 扫白名单,各跑一轮(未 ready 跳过)
    - `_backfill_loop` — 周期主循环:`_BACKFILL_INTERVAL` 秒
    - `_handle_connection_state` — reconnect 立即 kick 补拉

    依赖宿主属性:
    - `self.client / storage / bus` — 核心依赖
    - `self._whitelist` — 扫的频道列表
    - `self._unavailable_channels` — 不可访问频道去重 log
    - `self._stop` — 周期 loop 退出信号
    - `self._handle(msg)` — 复用主消息流入口
    - `self._BACKFILL_INTERVAL / _BACKFILL_LIMIT / _BACKFILL_MAX_PAGE` —
      周期 / 单页 / 防御性上限(类常量)
    - `self._skip_next_tick` — reconnect kick 后跳过紧接的周期 tick
      (避免重复回拉)
    """

    client: TelegramClient
    storage: StorageRepository
    bus: EventBus

    # 状态字段(在 MonitorService.__init__ 初始化)
    _whitelist: set[int]
    _unavailable_channels: set[int]
    _stop: asyncio.Event
    _handled: int
    _skip_next_tick: bool

    # 类常量(在 MonitorService 声明,mixin 通过 TYPE_CHECKING 引用)
    _BACKFILL_INTERVAL: float
    _BACKFILL_LIMIT: int
    _BACKFILL_MAX_PAGE: int

    # 跨 mixin 方法契约:orchestrator 调 `_backfill_loop` 启动周期任务,
    # backfill 调 `_handle` 把补拉到的消息送入主消息流(落库 + 去重)。
    # 注:不在这里写 `async def _handle ...: ...` —— 那会**重写** mixin
    # 提供的同名方法并被 Python 实际选中。`_handle` 在 MonitorService 真
    # 定义,mypy 解析调用点可见;`_backfill_loop` 在本 mixin 下面定义,
    # 不需要再声明。

    async def _backfill(self, channel_id: int) -> None:
        """补拉该频道最近消息:TDLib 的 `updateNewMessage` 只在连接期间推送,
        断线 / 重启期间的新消息不会重放,这里以周期兜底,顺带覆盖"实时流静默
        死亡"的场景(此时 `request()` 仍可用,消息数据照常入库)。

        锚点 = 库里该频道最大 `telegram_msg_id`;`iter_chat_history` 向旧方向
        递减,遇到 `<= 锚点` 即说明其余已覆盖,break。首次(无历史)锚点 = 0,
        只处理最近 `_BACKFILL_MAX_PAGE` 条当启动预热 — **不**让无锚点频道把
        整段历史翻完(每 30s 拉几万条会撞 flood wait,反而制造长空窗)。
        """
        try:
            max_id = await self.storage.get_max_telegram_msg_id(channel_id) or 0
            n = 0
            async for msg in self.client.iter_chat_history(
                channel_id,
                limit=self._BACKFILL_LIMIT,
            ):
                if n >= self._BACKFILL_MAX_PAGE:
                    log.warning(
                        "backfill channel %d hit cap %d; 新消息过多,建议手动全量同步",
                        channel_id,
                        self._BACKFILL_MAX_PAGE,
                    )
                    break
                if max_id > 0 and msg.telegram_msg_id <= max_id:
                    break
                # 跨 mixin 方法:`_handle` 在 MonitorService 主类(热路径)
                await self._handle(msg)  # type: ignore[attr-defined]
                n += 1
        except ClientClosingError:
            return  # close() 中,静默退出,不打 traceback
        except ChatUnavailableError as e:
            # 频道不可访问(新 session 未加载 / 已被移除):只 warning 一次,
            # 不再每轮刷 error traceback;恢复可用(下一轮成功)后清掉标记。
            if channel_id not in self._unavailable_channels:
                self._unavailable_channels.add(channel_id)
                log.warning("backfill channel %d skipped: %s", channel_id, e)
        except Exception:  # noqa: BLE001
            log.exception("backfill channel %d failed", channel_id)
        else:
            # 本轮成功 → 清除不可用标记(若之前被跳过,恢复后可重新 warning)
            self._unavailable_channels.discard(channel_id)

    async def _backfill_all(self) -> None:
        """对白名单里每个频道跑一轮补拉(频控:每频道每轮一次 getChatHistory)。

        未登录(非 ready)时不拉取 —— TDLib 未就绪时 getChatHistory 会抛
        "Client not started",每轮刷错误日志;登录成功 state 变 ready 后,
        下一轮补拉自动恢复,无需外部触发。
        """
        if self.client.state != "ready":
            log.info("backfill skipped: not ready (state=%s)", self.client.state)
            return
        for cid in list(self._whitelist):
            await self._backfill(cid)

    async def _backfill_loop(self) -> None:
        """周期补拉主循环:每 `_BACKFILL_INTERVAL` 秒全量扫一轮白名单。"""
        while not self._stop.is_set():
            await asyncio.sleep(self._BACKFILL_INTERVAL)
            if self._stop.is_set():
                break
            # 心跳:每轮补拉打一条 INFO —— 实时流挂了但补拉还活着时,
            # 日志里能看到两者不同步,定位"空窗"来源(INFO 默认可见)
            log.info(
                "backfill round start: whitelist=%d handled=%d",
                len(self._whitelist),
                self._handled,
            )
            await self._backfill_all()

    async def _handle_connection_state(
        self,
        event: ConnectionStateChanged,
    ) -> None:
        """2026-08-27 v1.4.0 PR #14:reconnect → 立即触发 backfill,不等 30s tick。

        state=`ready` 时 kick `_backfill_all`,并设 `_skip_next_tick`
        避免 tick 紧接着又跑一次造成回拉重复。
        """
        if event.state != "ready":
            return
        try:
            log.info("connection ready — kicking immediate backfill")
            self._skip_next_tick = True
            await self._backfill_all()
        except Exception as e:  # noqa: BLE001
            log.exception("immediate backfill on reconnect failed: %s", e)
            await self.bus.publish(
                ErrorOccurred(
                    source="monitor.backfill_kick",
                    message=str(e),
                    exception=e,
                )
            )
