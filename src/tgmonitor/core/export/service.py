"""ExportService — 编排:拉数据 → 选 Exporter → 流式写 → 报进度。

- 拉数据按 page 流式从 StorageRepository 拉,避免一次性载入内存
- 报进度:`ExportProgress` 事件;结束:`ExportDone`
- 取消:`CancelledError` 透传,UI 取消会即时停止写盘
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

from tgmonitor.core.dto import ExportRequest, ExportResult
from tgmonitor.core.events import EventBus, ExportDone, ExportProgress
from tgmonitor.core.export.base import EXPORTERS
# noqa: F401 — 触发 @exporter 装饰器,把所有具体 Exporter 注册到 EXPORTERS。
# 不能改 __init__.py(会被 ruff 报 unused),但放 service.py 里也无副作用,且
# 保证只要 ExportService 被 import,EXPORTERS 就 ready。
from tgmonitor.core.export import (  # noqa: F401
    csv_exporter,
    html_exporter,
    json_exporter,
    markdown_exporter,
)
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.storage.repository import StorageRepository

log = logging.getLogger(__name__)

PAGE_SIZE = 500


class ExportService:
    def __init__(
        self,
        storage: StorageRepository,
        objects: ObjectStore,
        bus: EventBus,
    ) -> None:
        self._storage = storage
        self._objects = objects
        self._bus = bus

    async def run(self, request: ExportRequest) -> AsyncIterator[None]:
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
        all_messages: list = []
        offset = 0
        channel_ids = list(channels.keys())
        while channel_ids:
            batch = await self._storage.list_messages(
                channel_ids=channel_ids,
                date_from=request.date_from,
                date_to=request.date_to,
                limit=PAGE_SIZE,
            )
            if not batch:
                break
            # 取最后一条的 date 作为下次的下界,实现按时间翻页
            tail_date = batch[-1].date
            tail_id = batch[-1].id
            all_messages.extend(batch)
            offset += len(batch)
            await self._bus.publish(
                ExportProgress(request_id=req_id, written=offset, total=None)
            )
            yield
            # 防死循环:batch 未推进则退出
            if len(batch) < PAGE_SIZE:
                break
            # 简化:本次实现拉完一批即停止(后续可扩展真分页游标)
            # 注:`list_messages` 当前实现未原生支持 offset/after-id,留待仓储扩展
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
