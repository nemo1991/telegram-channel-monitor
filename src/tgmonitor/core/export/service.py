"""ExportService — 编排:拉数据 → 选 Exporter → 流式写 → 报进度。

- 拉数据按 page 流式从 StorageRepository 拉,避免一次性载入内存
- 报进度:`ExportProgress` 事件;结束:`ExportDone`
- 取消:`CancelledError` 透传,UI 取消会即时停止写盘

2026-08-25 v1.3.0 PR #7:扩展 `run` 支持 `MediaExportRequest`(per-media
导出)— 走 `_run_media` 分支,共享同样的 ExportProgress / ExportDone
事件。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

from tgmonitor.core.dto import (
    ExportRequest,
    ExportResult,
    MediaExportRequest,
    MessageDTO,  # noqa: F401 — 2026-08-25 PR #7:dispatcher 用 dataclasses.replace
)
from tgmonitor.core.events import EventBus, ExportDone, ExportProgress

# noqa: F401 — 触发 @exporter 装饰器,把所有具体 Exporter 注册到 EXPORTERS。
# 不能改 __init__.py(会被 ruff 报 unused),但放 service.py 里也无副作用,且
# 保证只要 ExportService 被 import,EXPORTERS 就 ready。
from tgmonitor.core.export import (  # noqa: F401
    csv_exporter,
    html_exporter,
    json_exporter,
    markdown_exporter,
    media_list_csv_exporter,
)
from tgmonitor.core.export.base import EXPORTERS
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.storage.repository import StorageRepository

log = logging.getLogger(__name__)

PAGE_SIZE = 500

# 2026-08-25 v1.3.0 PR #7:`run` 入参联合类型 — ExportRequest (per-message)
# | MediaExportRequest (per-media)。
_ExportReq = ExportRequest | MediaExportRequest


class ExportService:
    """导出编排:拉数据 → 选 Exporter → 渲染 → 报进度 → 报完成。

    `run` 是 async generator;每 `yield` 给 UI 一个让出点(取消 / 进度刷新)。
    """

    def __init__(
        self,
        storage: StorageRepository,
        objects: ObjectStore,
        bus: EventBus,
    ) -> None:
        """`storage` = 拉数据源;`objects` = 缩略图源(若 include_thumbnails);
        `bus` = 发 `ExportProgress` / `ExportDone` 事件。
        """
        self._storage = storage
        self._objects = objects
        self._bus = bus

    async def run(self, request: _ExportReq) -> AsyncIterator[None]:
        """跑一次导出 — async generator,UI 在循环里 `break` 可即时取消。

        2026-08-25 v1.3.0 PR #7:`isinstance` 调度 — `MediaExportRequest`
        走 `_run_media` 分支(per-media 行);`ExportRequest` 走原
        `_run_messages`(per-message)。
        """
        if isinstance(request, MediaExportRequest):
            async for _ in self._run_media(request):
                yield
            return
        async for _ in self._run_messages(request):
            yield

    async def _run_messages(self, request: ExportRequest) -> AsyncIterator[None]:
        """既有 per-message 导出 — 历史行为,6 个老测试不变。"""
        req_id = uuid.uuid4().hex[:8]
        out_path = Path(request.out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # 频道信息
        all_channels = {c.id: c for c in await self._storage.list_channels()}
        if request.channel_ids:
            channels = {cid: all_channels[cid] for cid in request.channel_ids if cid in all_channels}
        else:
            channels = all_channels

        # 流式分页拉取(分页上限 PAGE_SIZE,累加并按时间排序)
        #
        # 2026-08-27 v1.4.0 PR #12:list_messages `limit` 语义是「最近 N 条」
        # (取排序尾部);要向前翻历史页就靠 `offset` 从尾部继续跳过。
        # 翻页方向:offset 0 → PAGE_SIZE → 2*PAGE_SIZE → ...
        # 翻页终止:batch < PAGE_SIZE(到顶了)/ offset 超 10M 兜底。
        all_messages: list = []
        offset = 0
        channel_ids = list(channels.keys())
        while channel_ids:
            batch = await self._storage.list_messages(
                channel_ids=channel_ids,
                date_from=request.date_from,
                date_to=request.date_to,
                limit=PAGE_SIZE,
                offset=offset,
            )
            if not batch:
                break
            all_messages.extend(batch)
            offset += len(batch)
            await self._bus.publish(
                ExportProgress(request_id=req_id, written=offset, total=None)
            )
            yield
            # 最后一页:不足 PAGE_SIZE 说明数据耗尽。
            if len(batch) < PAGE_SIZE:
                break
            # 死循环兜底:offset 已超数据上限(防 storage 端 race / bug)。
            if offset > 10_000_000:
                log.error("export pagination 超 10M 退出,channel_ids=%s", channel_ids)
                break

        all_messages.sort(key=lambda m: (m.date or datetime.min, str(m.id)))

        await self._bus.publish(
            ExportProgress(request_id=req_id, written=len(all_messages), total=len(all_messages))
        )

        try:
            exporter = EXPORTERS.get(request.format)
            bytes_written = await exporter.render(
                out_path,
                channels,
                all_messages,
                object_store=self._objects if request.include_thumbnails else None,
                include_thumbnails=request.include_thumbnails,
            )
            result = ExportResult(
                out_path=str(out_path),
                message_count=len(all_messages),
                bytes_written=bytes_written,
            )
            await self._bus.publish(ExportDone(request_id=req_id, result=result))
            yield
        except Exception as e:  # noqa: BLE001
            log.exception("export failed")
            await self._bus.publish(
                ExportDone(request_id=req_id, error=str(e))
            )
            raise

    async def _run_media(self, request: MediaExportRequest) -> AsyncIterator[None]:
        """per-media 导出 — 2026-08-25 v1.3.0 PR #7。

        走 `storage.list_media(*, channel_ids, status, media_type, search,
        sort, sort_dir, limit, offset)` 拉当前 filter 全量行(默认
        limit=100_000),把每条 `(msg, idx, med)` 包成一个临时 MessageDTO
        (覆盖 `media` 为 `[med]` 并注入 `_media_idx`)送给
        `MediaListCsvExporter.render` 写一行。`media_count` 在
        `ExportResult` 里代表行数。
        """
        import dataclasses

        req_id = uuid.uuid4().hex[:8]
        out_path = Path(request.out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        channels = {c.id: c for c in await self._storage.list_channels()}

        rows = await self._storage.list_media(
            channel_ids=[request.channel_id] if request.channel_id else None,
            status=request.status,
            media_type=request.media_type,
            search=request.search,
            sort=request.sort,
            sort_dir=request.sort_dir,
            limit=request.limit,
            offset=request.offset,
        )

        await self._bus.publish(
            ExportProgress(request_id=req_id, written=len(rows), total=len(rows))
        )
        yield

        # 包成 exporter 期望的 MessageDTO 列表(每条 message 仅含目标 media)
        # `_media_idx` 是 dispatcher ↔ exporter 的私有通道:replace 后再
        # 直接 setattr(MessageDTO 非 frozen)。
        wrapped: list[MessageDTO] = []
        for msg, idx, med in rows:
            new_msg = dataclasses.replace(msg, media=[med])
            new_msg._media_idx = idx  # type: ignore[attr-defined]
            wrapped.append(new_msg)

        try:
            exporter = EXPORTERS.get(request.format)
            bytes_written = await exporter.render(
                out_path,
                channels,
                wrapped,
                object_store=self._objects,
                include_thumbnails=False,
            )
            result = ExportResult(
                out_path=str(out_path),
                message_count=len(rows),  # 这里是 media 行数
                bytes_written=bytes_written,
            )
            await self._bus.publish(ExportDone(request_id=req_id, result=result))
            yield
        except Exception as e:  # noqa: BLE001
            log.exception("media export failed")
            await self._bus.publish(
                ExportDone(request_id=req_id, error=str(e))
            )
            raise
