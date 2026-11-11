"""周期补拉组件 — `MonitorService` 的 `_backfill` / `_backfill_all` /
`_backfill_loop` / `_handle_connection_state`。

2026-11-11 v2.0.0 Phase 3-C3:从 `BackfillMixin` 改为显式组合组件。
组件持 `svc: MonitorService` 引用,状态单一所有权仍在 MonitorService,
组件经 `self._svc.*` 读写 — 不再有 mixin MRO 魔法。周期补拉是独立子系统
(自管 task + 自管周期 + 自管 unavailable 标记),`_handle_connection_state`
紧耦合在补拉上(reconnect → 立即 kick),也一起搬过来。

`_backfill_all` 是测试可替换 seam:`_backfill_loop` / `_handle_connection_state`
经 `self._svc._backfill_all()` 回调宿主 delegate;补拉到的消息经
`self._svc._handle(msg)` 送入主消息流(落库 + 去重)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from tgmonitor.core.events import ConnectionStateChanged, ErrorOccurred
from tgmonitor.core.telegram.errors import ChatUnavailableError, ClientClosingError

if TYPE_CHECKING:
    from tgmonitor.core.monitor.service import MonitorService

log = logging.getLogger(__name__)


class BackfillRunner:
    """周期补拉组件。集中 4 个 method:

    - `_backfill` — 补拉单频道(`getChatHistory` → `_handle`)
    - `_backfill_all` — 扫白名单,各跑一轮(未 ready 跳过)
    - `_backfill_loop` — 周期主循环:`_BACKFILL_INTERVAL` 秒
    - `_handle_connection_state` — reconnect 立即 kick 补拉

    经 `self._svc` 访问宿主状态:`client / storage / bus / _whitelist /
    _unavailable_channels / _stop / _handled / _skip_next_tick` 及类常量
    `_BACKFILL_INTERVAL / _BACKFILL_LIMIT / _BACKFILL_MAX_PAGE`。
    """

    def __init__(self, svc: MonitorService) -> None:
        self._svc = svc

    async def _backfill(self, channel_id: int) -> None:
        """补拉该频道最近消息:Telegram 实时推送只覆盖连接期间,
        断线 / 重启期间的新消息不会重放,这里以周期兜底,顺带覆盖"实时流静默
        死亡"的场景(此时 `request()` 仍可用,消息数据照常入库)。

        锚点 = 库里该频道最大 `telegram_msg_id`;`iter_chat_history` 向旧方向
        递减,遇到 `<= 锚点` 即说明其余已覆盖,break。首次(无历史)锚点 = 0,
        只处理最近 `_BACKFILL_MAX_PAGE` 条当启动预热 — **不**让无锚点频道把
        整段历史翻完(每 30s 拉几万条会撞 flood wait,反而制造长空窗)。
        """
        try:
            max_id = await self._svc.storage.get_max_telegram_msg_id(channel_id) or 0
            n = 0
            async for msg in self._svc.client.iter_chat_history(
                channel_id,
                limit=self._svc._BACKFILL_LIMIT,
            ):
                if n >= self._svc._BACKFILL_MAX_PAGE:
                    log.warning(
                        "backfill channel %d hit cap %d; 新消息过多,建议手动全量同步",
                        channel_id,
                        self._svc._BACKFILL_MAX_PAGE,
                    )
                    break
                if max_id > 0 and msg.telegram_msg_id <= max_id:
                    break
                # `_handle` 在 MonitorService 主类(热路径),经 svc 引用调
                await self._svc._handle(msg)
                n += 1
        except ClientClosingError:
            return  # close() 中,静默退出,不打 traceback
        except ChatUnavailableError as e:
            # 频道不可访问(新 session 未加载 / 已被移除):只 warning 一次,
            # 不再每轮刷 error traceback;恢复可用(下一轮成功)后清掉标记。
            if channel_id not in self._svc._unavailable_channels:
                self._svc._unavailable_channels.add(channel_id)
                log.warning("backfill channel %d skipped: %s", channel_id, e)
        except Exception:  # noqa: BLE001
            log.exception("backfill channel %d failed", channel_id)
        else:
            # 本轮成功 → 清除不可用标记(若之前被跳过,恢复后可重新 warning)
            self._svc._unavailable_channels.discard(channel_id)

    async def _backfill_all(self) -> None:
        """对白名单里每个频道跑一轮补拉(频控:每频道每轮一次 getChatHistory)。

        未登录(非 ready)时不拉取 —— 客户端未就绪时拉历史会抛
        错误,每轮刷错误日志;登录成功 state 变 ready 后,
        下一轮补拉自动恢复,无需外部触发。
        """
        if self._svc.client.state != "ready":
            log.info("backfill skipped: not ready (state=%s)", self._svc.client.state)
            return
        for cid in list(self._svc._whitelist):
            await self._backfill(cid)

    async def _backfill_loop(self) -> None:
        """周期补拉主循环:每 `_BACKFILL_INTERVAL` 秒全量扫一轮白名单。"""
        while not self._svc._stop.is_set():
            await asyncio.sleep(self._svc._BACKFILL_INTERVAL)
            if self._svc._stop.is_set():
                break
            # 心跳:每轮补拉打一条 INFO —— 实时流挂了但补拉还活着时,
            # 日志里能看到两者不同步,定位"空窗"来源(INFO 默认可见)
            log.info(
                "backfill round start: whitelist=%d handled=%d",
                len(self._svc._whitelist),
                self._svc._handled,
            )
            await self._svc._backfill_all()

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
            self._svc._skip_next_tick = True
            await self._svc._backfill_all()
        except Exception as e:  # noqa: BLE001
            log.exception("immediate backfill on reconnect failed: %s", e)
            await self._svc.bus.publish(
                ErrorOccurred(
                    source="monitor.backfill_kick",
                    message=str(e),
                    exception=e,
                )
            )
