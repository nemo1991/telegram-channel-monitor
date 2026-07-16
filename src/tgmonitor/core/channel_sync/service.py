"""ChannelSyncService — 用户多选频道后的全量同步(元数据 + 历史消息)。

触发:`AppService.sync_channels(channel_ids, options)` 由 UI "全量同步…"
按钮经进度对话框调用。

设计:
- **手动触发**(不是周期后台 task;周期功能不在本期范围)
- **逐频道串行**:GetSupergroup / getChatHistory 顺序处理(单条 API 间隔
  `options.chat_delay_ms`);翻页(`page_delay_ms`)在 getChatHistory 之间
- **续拉**:从 `storage.get_max_telegram_msg_id(channel_id)` 之后拉
  (`options.resume_from_saved=True`)
- **限流归一**:tdlib 抛 `TelegramRateLimitError` → 等准确 `retry_after` →
  继续;网络错误也退避
- **取消**:`cancel()` 唤醒所有 `asyncio.sleep`,可中断长任务
- **进度事件**:每阶段发 `ChannelSyncProgress` → UI 实时显示

事件总线 依赖:由 `AppService.sync_channels` 拿 `bus` 发事件。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tgmonitor.core.dto import (
    ChannelSyncResult,
    SyncOptions,
    SyncResult,
)
from tgmonitor.core.events import (
    ChannelSyncDone,
    ChannelSyncProgress,
)
from tgmonitor.core.telegram.client import TelegramClient
from tgmonitor.core.telegram.tdlib_client import TelegramRateLimitError

if TYPE_CHECKING:
    from tgmonitor.core.events import EventBus
    from tgmonitor.core.storage.repository import StorageRepository

log = logging.getLogger(__name__)


class ChannelSyncService:
    def __init__(
        self,
        bus: EventBus,
        client: TelegramClient,
        storage: StorageRepository,
    ) -> None:
        self.bus = bus
        self.client = client
        self.storage = storage
        self._cancel = asyncio.Event()
        # 进度 throttle:同一频道不连续 publish(避免 N×100 条消息时
        # 事件风暴把 UI 卡死)— 50ms 节流
        self._last_progress_emit: dict[int, float] = {}
        self._last_stage: dict[int, str] = {}

    def cancel(self) -> None:
        """UI 进度对话框"取消"按钮调,立刻唤醒长 sleep。"""
        self._cancel.set()

    async def sync_channels(
        self,
        channel_ids: list[int],
        options: SyncOptions,
    ) -> SyncResult:
        """主入口:对每个被选频道拉元数据 + 历史。

        不并发(单条 API 间隔 sleep 控制节奏);用户点取消立刻退出。
        """
        self._cancel.clear()
        result = SyncResult(per_channel={})
        t0 = time.monotonic()

        for cid in channel_ids:
            if self._cancel.is_set():
                result.cancelled = True
                break
            ch_result = ChannelSyncResult(channel_id=cid)
            try:
                if options.include_metadata:
                    await self._emit_progress(
                        cid, "metadata", progress=0, total=1,
                        detail="",
                    )
                    dto = await self.client.get_channel_metadata(cid)
                    dto.last_synced_at = datetime.now(UTC)
                    await self.storage.upsert_channel_metadata(dto)
                    ch_result.metadata_updated = True
                    await self._emit_progress(
                        cid, "metadata", progress=1, total=1,
                        detail=f"title={dto.title}",
                    )
                    await self._sleep_or_cancel(options.chat_delay_ms / 1000.0)

                if options.include_history:
                    last_id: int | None = None
                    if options.resume_from_saved:
                        last_id = await self.storage.get_max_telegram_msg_id(cid)
                    from_id = last_id or 0
                    if last_id is not None:
                        await self._emit_progress(
                            cid, "history", progress=0, total=None,
                            detail=f"续拉 from {last_id}",
                        )
                    else:
                        await self._emit_progress(
                            cid, "history", progress=0, total=None,
                            detail="拉全部",
                        )

                    page_count = 0
                    batch_total = 0
                    added_in_channel = 0
                    async for m in self.client.iter_chat_history(
                        cid, from_msg_id=from_id, limit=100,
                    ):
                        if self._cancel.is_set():
                            result.cancelled = True
                            break
                        existed = await self.storage.get_message(
                            m.channel_id, m.telegram_msg_id
                        )
                        await self.storage.save_message(m)
                        if existed is None:
                            added_in_channel += 1
                        ch_result.messages_added += 1
                        ch_result.history_ended_at_msg_id = m.telegram_msg_id
                        batch_total += 1
                        page_count += 1
                        # 进度节流:同频道同阶段不连续 publish(防事件风暴)
                        now = time.monotonic()
                        last = self._last_progress_emit.get(cid, 0.0)
                        if now - last > 0.5 or self._last_stage.get(cid) != "history":
                            self._last_progress_emit[cid] = now
                            self._last_stage[cid] = "history"
                            await self._emit_progress(
                                cid, "history", progress=ch_result.messages_added,
                                total=None, detail="",
                            )
                        # 单条间隔(防限速)
                        if options.chat_delay_ms > 0:
                            await self._sleep_or_cancel(
                                options.chat_delay_ms / 1000.0
                            )
                        # 整百条触发分页间隔(更重的请求)
                        if (
                            page_count % 100 == 0
                            and options.page_delay_ms > 0
                        ):
                            await self._sleep_or_cancel(
                                options.page_delay_ms / 1000.0
                            )
                    result.total_messages_added += added_in_channel
                    await self._emit_progress(
                        cid, "history", progress=ch_result.messages_added,
                        total=None, detail=f"新增 {added_in_channel} 条",
                    )

                if result.cancelled:
                    # 用户取消也记入 per_channel(显示"已取消"状态)
                    ch_result.error = "cancelled"
                result.per_channel[cid] = ch_result
                await self._emit_progress(
                    cid, "done", progress=1, total=1,
                    detail=f"meta={'✓' if ch_result.metadata_updated else '—'} "
                           f"history={ch_result.messages_added}",
                )
            except TelegramRateLimitError as e:
                log.warning(
                    "channel %d rate-limited, backing off %.0fs", cid,
                    e.retry_after_seconds,
                )
                ch_result.rate_limited = True
                ch_result.error = f"FLOOD_WAIT {e.retry_after_seconds:.0f}s"
                result.per_channel[cid] = ch_result
                result.rate_limited_seconds = e.retry_after_seconds
                await self._emit_progress(
                    cid, "backoff", progress=0, total=0,
                    detail=f"等待 {e.retry_after_seconds:.0f}s",
                )
                # 等准确时间(可取消);不取消的话继续下一个频道
                cancelled = await self._sleep_or_cancel(e.retry_after_seconds)
                if cancelled:
                    result.cancelled = True
                    break
            except Exception as e:  # noqa: BLE001
                log.exception("sync channel %d failed", cid)
                ch_result.error = f"{type(e).__name__}: {e}"
                result.per_channel[cid] = ch_result
                await self._emit_progress(
                    cid, "failed", progress=0, total=0,
                    detail=ch_result.error or "",
                )

        log.info(
            "channel sync done in %.2fs: %d channels, %d messages added%s",
            time.monotonic() - t0,
            len(result.per_channel),
            result.total_messages_added,
            " (cancelled)" if result.cancelled else "",
        )
        await self.bus.publish(ChannelSyncDone(result=result))
        return result

    async def _sleep_or_cancel(self, seconds: float) -> bool:
        """睡 `seconds` 秒,但 cancel 一 set 立刻醒。

        返回 True 表示被取消(caller 应退出)。
        """
        if seconds <= 0:
            return self._cancel.is_set()
        try:
            await asyncio.wait_for(self._cancel.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True  # 醒来是因为 cancel

    async def _emit_progress(
        self,
        channel_id: int,
        stage: str,
        progress: int,
        total: int | None,
        detail: str = "",
    ) -> None:
        await self.bus.publish(ChannelSyncProgress(
            channel_id=channel_id, stage=stage, progress=progress,
            total=total, detail=detail,
        ))
