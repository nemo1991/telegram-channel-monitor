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

# 结构(2026-08-02 拆分)

`sync_channels` 退化为 orchestrator(只负责循环 + 累加),4 个维度分到 3 个
单阶段 helper 里:

- `_sync_one_channel`:单频道组合 + 异常(限流 / 一般异常 / 取消)
- `_sync_metadata`:单频道拉元数据
- `_sync_history`:单频道拉历史(分页 + throttle)

`_sleep_or_cancel` / `_emit_progress` 保持 module-private 不动。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tgmonitor.core.config import MediaPolicy
from tgmonitor.core.dto import (
    ChannelSyncResult,
    MediaDownloadStatus,
    SyncOptions,
    SyncResult,
)
from tgmonitor.core.events import (
    ChannelSyncDone,
    ChannelSyncProgress,
)
from tgmonitor.core.telegram.client import TelegramClient
from tgmonitor.core.telegram.tdlib_errors import TelegramRateLimitError

if TYPE_CHECKING:
    from tgmonitor.core.events import EventBus
    from tgmonitor.core.monitor.service import MediaDownloader
    from tgmonitor.core.objectstore.base import ObjectStore
    from tgmonitor.core.storage.repository import StorageRepository

log = logging.getLogger(__name__)


class ChannelSyncService:
    """全量同步服务:对多个频道拉元数据 + 历史消息(防封号节流)。"""

    def __init__(
        self,
        bus: EventBus,
        client: TelegramClient,
        storage: StorageRepository,
        *,
        downloader: MediaDownloader | None = None,
        objects: ObjectStore | None = None,
        media_policy: MediaPolicy = MediaPolicy.METADATA,
    ) -> None:
        """`bus` = 发 ChannelSyncProgress / ChannelSyncDone 事件。

        `downloader` / `objects` / `media_policy`(2026-08-24 接入)— sync 媒体
        下载复用 monitor 的 `MediaDownloader`,FULL 策略下 sync 也会下载原文件。
        `downloader.download_one` 已自带 skip-if-stored,所以 re-sync 频道命中
        storage 已有同 file_id 时不发 TDLib 请求。
        """
        self.bus = bus
        self.client = client
        self.storage = storage
        self.downloader = downloader
        self.objects = objects
        self.media_policy = media_policy
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
        """主入口:对每个被选频道调 `_sync_one_channel`(orchestrator)。

        只负责循环控制 + `total_messages_added` 累加 + 顶部取消 + 末尾 done
        事件。具体的元数据 / 历史 / 异常处理都下沉到 helper。

        进度事件(2026-08-24):
          - 顶部发 `init`(载 total 频道数),让进度对话框先显示「准备同步 N 频道」
          - 每个频道 `_sync_one_channel` 顶部发 `channel_start`
          - 末尾每个频道发 `done`,detail 带「新增 N / 已存 M」
        """
        self._cancel.clear()
        result = SyncResult(per_channel={})
        t0 = time.monotonic()

        # 顶部 init 事件(2026-08-24):让 UI 提前显示总频道数,避免空白 N 秒
        if channel_ids:
            await self._emit_progress(
                0,
                "init",
                progress=0,
                total=len(channel_ids),
                detail=f"{len(channel_ids)} 频道待同步",
            )

        for cid in channel_ids:
            if self._cancel.is_set():
                result.cancelled = True
                break
            ch_result, rate_limited_seconds = await self._sync_one_channel(cid, options)
            # 退订 = 用户取消 — orchestrator 不 break,让 per_channel 记录
            # 全部已尝试的频道;但 result.cancelled 仍 True 让 UI 知道是中断。
            if ch_result.error == "cancelled" and not result.cancelled:
                result.cancelled = True
            result.per_channel[cid] = ch_result
            # 限流的频道不计入 total(它的 added 是"部分",不是完整一轮)
            if not ch_result.rate_limited:
                result.total_messages_added += ch_result.new_messages_added
            if rate_limited_seconds is not None:
                result.rate_limited_seconds = rate_limited_seconds
            await self._emit_progress(
                cid,
                "done",
                progress=1,
                total=1,
                detail=(
                    f"meta={'✓' if ch_result.metadata_updated else '—'} "
                    f"new={ch_result.messages_added} "
                    f"skipped={ch_result.messages_skipped}"
                ),
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

    async def _sync_one_channel(
        self,
        cid: int,
        options: SyncOptions,
    ) -> tuple[ChannelSyncResult, float | None]:
        """单频道组合:metadata + history,统一处理限流 / 一般异常 / 取消。

        返回 `(ChannelSyncResult, rate_limited_seconds)` — 第二个元素仅在
        限流路径上非 None(透传 `retry_after`),让 outer 把整体 rate_limited
        时间塞进 `SyncResult.rate_limited_seconds`。

        返回的 `ChannelSyncResult.error` 可能是 None / "cancelled" /
        "FLOOD_WAIT Ns" / "ExceptionType: msg" — UI 进度对话框直接展示。

        限流:`TelegramRateLimitError` → 写 ch_result.rate_limited + .error,
        等准确 `retry_after` 秒后返回(**不抛、不 break** — 让 outer
        继续处理下一频道)。

        一般异常:log + 写 ch_result.error,返回。

        取消:helper 内部 break / return 时写 ch_result.error = "cancelled"。
        """
        ch_result = ChannelSyncResult(channel_id=cid)
        # 频道开始事件(2026-08-24):让 UI 在 metadata 之前就先知道这个频道在动了
        await self._emit_progress(
            cid,
            "channel_start",
            progress=0,
            total=0,
            detail="开始同步",
        )
        # 限流 → 把 retry_after 透传到 outer result(供 UI 进度条展示整体退避)
        rate_limited_seconds: float | None = None
        try:
            if options.include_metadata:
                await self._sync_metadata(cid, options, ch_result)
                if self._cancel.is_set():
                    ch_result.error = "cancelled"
                    return ch_result, rate_limited_seconds

            if options.include_history:
                await self._sync_history(cid, options, ch_result)
                if self._cancel.is_set():
                    ch_result.error = "cancelled"
                    return ch_result, rate_limited_seconds
        except TelegramRateLimitError as e:
            rate_limited_seconds = e.retry_after_seconds
            log.warning(
                "channel %d rate-limited, backing off %.0fs",
                cid,
                e.retry_after_seconds,
            )
            ch_result.rate_limited = True
            ch_result.error = f"FLOOD_WAIT {e.retry_after_seconds:.0f}s"
            await self._emit_progress(
                cid,
                "backoff",
                progress=0,
                total=0,
                detail=f"等待 {e.retry_after_seconds:.0f}s",
            )
            # 等准确时间(可取消);不取消的话继续下一个频道
            cancelled = await self._sleep_or_cancel(e.retry_after_seconds)
            if cancelled:
                ch_result.error = "cancelled"
        except Exception as e:  # noqa: BLE001
            log.exception("sync channel %d failed", cid)
            ch_result.error = f"{type(e).__name__}: {e}"
            await self._emit_progress(
                cid,
                "failed",
                progress=0,
                total=0,
                detail=ch_result.error or "",
            )
        return ch_result, rate_limited_seconds

    async def _sync_metadata(
        self,
        cid: int,
        options: SyncOptions,
        ch_result: ChannelSyncResult,
    ) -> None:
        """单频道拉元数据 + 写 storage + 标记 metadata_updated。

        异常由 `_sync_one_channel` 的 try 块捕获,这里不写 error。
        """
        await self._emit_progress(
            cid,
            "metadata",
            progress=0,
            total=1,
            detail="",
        )
        dto = await self.client.get_channel_metadata(cid)
        dto.last_synced_at = datetime.now(UTC)
        await self.storage.upsert_channel_metadata(dto)
        ch_result.metadata_updated = True
        await self._emit_progress(
            cid,
            "metadata",
            progress=1,
            total=1,
            detail=f"title={dto.title}",
        )
        await self._sleep_or_cancel(options.chat_delay_ms / 1000.0)

    async def _sync_history(
        self,
        cid: int,
        options: SyncOptions,
        ch_result: ChannelSyncResult,
    ) -> None:
        """单频道拉历史 — 分页循环 + throttle + 重间隔都在内。

        写 `ch_result.messages_added`(每条 +1,不去重) +
        `ch_result.new_messages_added`(只在 existed is None 时 +1)。
        取消检测在每条迭代入口;命中后 `_sync_one_channel` 检测
        `self._cancel.is_set()` 写 ch_result.error = "cancelled"。

        异常由 `_sync_one_channel` 的 try 块捕获。
        """
        last_id: int | None = None
        if options.resume_from_saved:
            last_id = await self.storage.get_max_telegram_msg_id(cid)
        from_id = last_id or 0
        if last_id is not None:
            await self._emit_progress(
                cid,
                "history",
                progress=0,
                total=None,
                detail=f"续拉 from {last_id}",
            )
        else:
            await self._emit_progress(
                cid,
                "history",
                progress=0,
                total=None,
                detail="拉全部",
            )

        page_count = 0
        async for m in self.client.iter_chat_history(
            cid,
            before_msg_id=from_id,
            limit=100,
        ):
            if self._cancel.is_set():
                return  # outer 检测 cancel 写 ch_result.error
            existed = await self.storage.get_message(
                m.channel_id,
                m.telegram_msg_id,
            )
            if existed is not None:
                # 已在库中 — 静默跳过,只递增 skipped 与 cursor;
                # 不写 save_message,不递增 messages_added / new_messages_added。
                ch_result.messages_skipped += 1
                ch_result.history_ended_at_msg_id = m.telegram_msg_id
            else:
                await self.storage.save_message(m)
                ch_result.messages_added += 1
                ch_result.new_messages_added += 1
                ch_result.history_ended_at_msg_id = m.telegram_msg_id
                # 媒体下载(FULL 策略)— 复用 monitor 的 MediaDownloader,
                # download_one 内部已带 skip-if-stored(2026-08-24)
                if (
                    m.media
                    and self.downloader is not None
                    and self.media_policy == MediaPolicy.FULL
                ):
                    needs_resave = False
                    for idx, med in enumerate(m.media):
                        if med.download_status != MediaDownloadStatus.PENDING:
                            continue
                        updated = await self.downloader.download_one(
                            msg_pk=m.id,
                            media=med,
                        )
                        if updated is not med:
                            m.media[idx] = updated
                            needs_resave = True
                    if needs_resave:
                        await self.storage.save_message(m)
            page_count += 1
            # 进度节流:同频道同阶段不连续 publish(防事件风暴)
            now = time.monotonic()
            last = self._last_progress_emit.get(cid, 0.0)
            if now - last > 0.5 or self._last_stage.get(cid) != "history":
                self._last_progress_emit[cid] = now
                self._last_stage[cid] = "history"
                await self._emit_progress(
                    cid,
                    "history",
                    progress=ch_result.messages_added,
                    total=None,
                    detail=(f"新增 {ch_result.messages_added} 已存 {ch_result.messages_skipped}"),
                )
            # 单条间隔(防限速)
            if options.chat_delay_ms > 0:
                await self._sleep_or_cancel(
                    options.chat_delay_ms / 1000.0,
                )
            # 整百条触发分页间隔(更重的请求)
            if page_count % 100 == 0 and options.page_delay_ms > 0:
                await self._sleep_or_cancel(
                    options.page_delay_ms / 1000.0,
                )

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
        await self.bus.publish(
            ChannelSyncProgress(
                channel_id=channel_id,
                stage=stage,
                progress=progress,
                total=total,
                detail=detail,
            )
        )
