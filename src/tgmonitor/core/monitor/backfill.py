"""周期补拉组件 — `MonitorService` 的 `_backfill` / `_backfill_all` /
`_backfill_loop` / `_handle_connection_state`。

2026-11-11 v2.0.0 Phase 3-C3:从 `BackfillMixin` 改为显式组合组件。
2026-11-25 P2-M4:组件改为持有共享 `MonitorRuntime`,不再反向持有
`MonitorService`;状态经 `self._rt.*` 读写,跨组件调用经晚绑定回调。
周期补拉是独立子系统
(自管 task + 自管周期 + 自管 unavailable 标记),`_handle_connection_state`
紧耦合在补拉上(reconnect → 立即 kick),也一起搬过来。

`_backfill_all` 是测试可替换 seam:`_backfill_loop` / `_handle_connection_state`
经 `rt.backfill_all` 晚绑定回调宿主 delegate;补拉到的消息经
`rt.handle_message(msg)` 送入主消息流(落库 + 去重)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from tgmonitor.core.events import ConnectionStateChanged, ErrorOccurred
from tgmonitor.core.monitor._runtime import MonitorRuntime
from tgmonitor.core.telegram.errors import ChatUnavailableError, ClientClosingError

if TYPE_CHECKING:
    from tgmonitor.core.dto import ChannelDTO

log = logging.getLogger(__name__)


def _channel_label(channel: ChannelDTO | None, channel_id: int) -> str:
    """补拉日志里的频道标识:名称为主、ID 为辅;元数据缺失时降级 `#id`。

    2026-11-24 v2.0.10 issue #128:旧文案只有裸 `channel -108888881`,排查时
    还得翻库二次确认是哪个频道。名称取 `ChannelDTO.title`,与 UI 侧
    `_channel_name` 的优先策略一致;core 不能 import UI,故在此独立实现。
    """
    if channel is not None and channel.title.strip():
        return f"{channel.title.strip()} (id={channel_id})"
    return f"#{channel_id}"


class BackfillRunner:
    """周期补拉组件。集中 4 个 method:

    - `_backfill` — 补拉单频道(`getChatHistory` → `_handle`)
    - `_backfill_all` — 扫白名单,各跑一轮(未 ready 跳过)
    - `_backfill_loop` — 周期主循环:`_BACKFILL_INTERVAL` 秒
    - `_handle_connection_state` — reconnect 立即 kick 补拉

    经 `self._rt` 访问共享状态:`client / storage / bus / whitelist /
    unavailable_channels / stop / handled / skip_next_tick` 及配置
    `backfill_interval / backfill_limit / backfill_max_page`。
    """

    def __init__(self, rt: MonitorRuntime) -> None:
        self._rt = rt

    async def _backfill(self, channel_id: int, channel: ChannelDTO | None = None) -> None:
        """补拉该频道最近消息:Telegram 实时推送只覆盖连接期间,
        断线 / 重启期间的新消息不会重放,这里以周期兜底,顺带覆盖"实时流静默
        死亡"的场景(此时 `request()` 仍可用,消息数据照常入库)。

        锚点 = 库里该频道最大 `telegram_msg_id`;`iter_chat_history` 向旧方向
        递减,遇到 `<= 锚点` 即说明其余已覆盖,break。首次(无历史)锚点 = 0,
        只处理最近 `_BACKFILL_MAX_PAGE` 条当启动预热 — **不**让无锚点频道把
        整段历史翻完(每 30s 拉几万条会撞 flood wait,反而制造长空窗)。

        `channel` 由 `_backfill_all` 每轮一次性预取传入(零逐条查库);单频道
        seam(测试 / 显式调用)未传时回退查一次,查失败不影响补拉本身。
        """
        if channel is None:
            try:
                channel = await self._rt.storage.get_channel(channel_id)
            except Exception:  # noqa: BLE001 — 元数据查询失败不该打断补拉
                channel = None
        label = _channel_label(channel, channel_id)
        try:
            max_id = await self._rt.storage.get_max_telegram_msg_id(channel_id) or 0
            n = 0
            async for msg in self._rt.client.iter_chat_history(
                channel_id,
                limit=self._rt.backfill_limit,
            ):
                if n >= self._rt.backfill_max_page:
                    log.warning(
                        "backfill channel %s hit cap %d; 新消息过多，已达本次回填上限",
                        label,
                        self._rt.backfill_max_page,
                    )
                    break
                if max_id > 0 and msg.telegram_msg_id <= max_id:
                    break
                # `_handle` 在 MonitorService 主类(热路径),经晚绑定回调调
                cb = self._rt.handle_message
                assert cb is not None
                await cb(msg)
                n += 1
        except ClientClosingError:
            return  # close() 中,静默退出,不打 traceback
        except ChatUnavailableError as e:
            # 频道不可访问(新 session 未加载 / 已被移除):只 warning 一次,
            # 不再每轮刷 error traceback;恢复可用(下一轮成功)后清掉标记。
            if channel_id not in self._rt.unavailable_channels:
                self._rt.unavailable_channels.add(channel_id)
                log.warning("backfill channel %s skipped: %s", label, e)
        except Exception:
            log.exception("backfill channel %s failed", label)
        else:
            # 本轮成功 → 清除不可用标记(若之前被跳过,恢复后可重新 warning)
            self._rt.unavailable_channels.discard(channel_id)

    async def _backfill_all(self) -> None:
        """对白名单里每个频道跑一轮补拉(频控:每频道每轮一次 getChatHistory)。

        未登录(非 ready)时不拉取 —— 客户端未就绪时拉历史会抛
        错误,每轮刷错误日志;登录成功 state 变 ready 后,
        下一轮补拉自动恢复,无需外部触发。
        """
        if self._rt.client.state != "ready":
            log.info("backfill skipped: not ready (state=%s)", self._rt.client.state)
            return
        # issue #128:每轮一次预取频道名,传给各 `_backfill` 打日志用 —
        # 不逐条查库;storage 查询失败时降级为空表,各频道仍按 #id 打日志。
        try:
            channels = {c.id: c for c in await self._rt.storage.list_channels()}
        except Exception:  # noqa: BLE001 — 元数据预取失败不该打断补拉
            channels = {}
        for cid in list(self._rt.whitelist):
            await self._backfill(cid, channels.get(cid))

    async def _backfill_loop(self) -> None:
        """周期补拉主循环:每 `_BACKFILL_INTERVAL` 秒全量扫一轮白名单。

        2026-11-25 P2 #30:`skip_next_tick` 在此消费 — reconnect kick
        (`_handle_connection_state`)刚跑过一轮立即补拉时置位,本循环下一轮
        tick 跳过一次,避免 30s tick 紧接着再跑一轮造成回拉重复。
        """
        while not self._rt.stop.is_set():
            await asyncio.sleep(self._rt.backfill_interval)
            if self._rt.stop.is_set():
                break
            # P2 #30:reconnect kick 后跳过一次 tick(标志消费即清零)
            if self._rt.skip_next_tick:
                self._rt.skip_next_tick = False
                log.debug("backfill tick skipped: just kicked on reconnect")
                continue
            # 心跳:每轮补拉打一条 INFO —— 实时流挂了但补拉还活着时,
            # 日志里能看到两者不同步,定位"空窗"来源(INFO 默认可见)
            log.info(
                "backfill round start: whitelist=%d handled=%d",
                len(self._rt.whitelist),
                self._rt.handled,
            )
            cb = self._rt.backfill_all
            assert cb is not None
            await cb()

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
            self._rt.skip_next_tick = True
            cb = self._rt.backfill_all
            assert cb is not None
            await cb()
        except Exception as e:
            log.exception("immediate backfill on reconnect failed: %s", e)
            await self._rt.bus.publish(
                ErrorOccurred(
                    source="monitor.backfill_kick",
                    message=str(e),
                    exception=e,
                )
            )
