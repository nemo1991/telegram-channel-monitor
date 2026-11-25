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
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator

from tgmonitor.core.dto import (
    ExportFormat,
    ExportRequest,
    ExportResult,
    MediaExportRequest,
    MediaExportRowDTO,
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
    zip_exporter,  # 2026-09-01 v1.5.1 PR #B4:ZIP 导出注册
)
from tgmonitor.core.export.base import EXPORTERS
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.storage.repository import StorageRepository

log = logging.getLogger(__name__)

PAGE_SIZE = 500


def _message_sort_key(m: MessageDTO) -> tuple[datetime, int]:
    """导出排序键(P2-2.1.4 #8)— naive date 视作 UTC 归一 + int tie-break。

    旧实现 `(m.date or datetime.min, str(m.id))` 两个问题:
    1. `datetime.min` 是 naive,与 aware `m.date` 混比直接 TypeError;
    2. `str(id)` 字典序导致 "10" < "9" 错序。

    tie-break 改用 int 主键 `m.id`(与各存储后端 `date ASC, id ASC` 的
    SQL tie-break 一致);`m.date` 按 DTO 契约非空,None 分支仅防御。
    """
    d = m.date
    if d is None:
        return (datetime.min.replace(tzinfo=UTC), m.id)
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return (d, m.id)


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
        `_run_messages`(per-message)或 2026-09-08 v1.7.0 新增的
        `_run_selected`(右键多选条目导出)。
        """
        if isinstance(request, MediaExportRequest):
            async for _ in self._run_media(request):
                yield
            return
        if request.selected_messages is not None:
            async for _ in self._run_selected(request):
                yield
            return
        async for _ in self._run_messages(request):
            yield

    async def _run_selected(self, request: ExportRequest) -> AsyncIterator[None]:
        """2026-09-08 v1.7.0:批量导出 — 对每个 `(cid, mid)` 调
        `storage.get_message(...)`,N round-trip 暂接受(1000 条 × 3ms ≈ 3s);
        v1.8+ 推 `storage.get_messages_batch` 一次性 fetch。

        单条消息不存在(None)→ 跳过(可能在删除后立即调用,UI 重试)。
        """
        req_id = uuid.uuid4().hex[:8]
        out_path = Path(request.out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sel = request.selected_messages or []
        all_channels = {c.id: c for c in await self._storage.list_channels()}

        all_messages: list[MessageDTO] = []
        for i, (cid, mid) in enumerate(sel):
            msg = await self._storage.get_message(cid, mid)
            if msg is not None:
                all_messages.append(msg)
            # 每 5 条 yield 一次让出 loop(UI 可取消)
            if (i + 1) % 5 == 0:
                await self._bus.publish(
                    ExportProgress(request_id=req_id, written=i + 1, total=len(sel))
                )
                yield
        # 末尾 progress
        await self._bus.publish(ExportProgress(request_id=req_id, written=len(sel), total=len(sel)))

        # 收 channel 子集 — 只显示选中消息所在的频道
        used_cids = {m.channel_id for m in all_messages}
        channels = {cid: all_channels[cid] for cid in used_cids if cid in all_channels}

        try:
            exporter = EXPORTERS.get(request.format)
            object_store_arg: ObjectStore | None = (
                self._objects
                if request.format == ExportFormat.ZIP or request.include_thumbnails
                else None
            )
            bytes_written = await exporter.render(
                out_path,
                channels,
                all_messages,
                object_store=object_store_arg,
                include_thumbnails=request.include_thumbnails,
                include_metadata=request.include_metadata,  # 2026-09-10 v1.7.3
                storage=self._storage if request.include_thumbnails else None,  # 2026-09-29
            )
            result = ExportResult(
                out_path=str(out_path),
                message_count=len(all_messages),
                bytes_written=bytes_written,
            )
            await self._bus.publish(ExportDone(request_id=req_id, result=result))
            yield
        except Exception as e:  # noqa: BLE001
            log.exception("export selected failed")
            await self._bus.publish(ExportDone(request_id=req_id, error=str(e)))
            raise

    async def _run_messages(self, request: ExportRequest) -> AsyncIterator[None]:
        """既有 per-message 导出 — 历史行为,6 个老测试不变。

        2026-09-01 v1.5.1 PR #B4:若 `request.single_message_id` 非 None,
        直接 `storage.get_message(...)` 拉单条,跳过 `list_messages` 分页
        流(对 ZIP 走单条消息导出时省一趟 + 避免分页残留 N=0 的副作用)。
        """
        req_id = uuid.uuid4().hex[:8]
        out_path = Path(request.out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # 频道信息
        all_channels = {c.id: c for c in await self._storage.list_channels()}
        if request.channel_ids:
            channels = {
                cid: all_channels[cid] for cid in request.channel_ids if cid in all_channels
            }
        else:
            channels = all_channels

        all_messages: list[MessageDTO] = []
        # 2026-09-01 v1.5.1 PR #B4:单条消息入口 — `channel_ids[0]` 即
        # 消息所在频道(用户从 message view / media manager 触发时已
        # 隐含知道)。若 channel_ids 为空,降级到分页路径(避免误用)。
        if request.single_message_id is not None and request.channel_ids:
            ch_id = request.channel_ids[0]
            msg = await self._storage.get_message(ch_id, request.single_message_id)
            if msg is not None:
                all_messages.append(msg)
            await self._bus.publish(
                ExportProgress(
                    request_id=req_id, written=len(all_messages), total=len(all_messages)
                )
            )
            yield
        else:
            # 快照边界分页(P2-2.1.4 #9)— 修复 offset 从尾翻页的并发竞态。
            #
            # 旧实现:offset 每页 += len(batch),从「最新端」跳过
            # (list_messages 的 LIMIT/OFFSET 在 `date DESC, id DESC` 上取,
            # 再反转返回)。导出期间有新消息入库时,已取过的页整体后移,
            # 下一页会重复取到旧消息(offset 空间漂移 → 重复导出)。
            #
            # keyset 分页需要 storage 支持「取比游标更旧的一批」,
            # list_messages 无此参数且本次修复不动 storage/,故采用
            # 快照边界方案:
            # 1. 会话开始时逐频道取最新一条,记 telegram_msg_id 作该频道
            #    快照上界;
            # 2. 分页查询带 date_to = 各频道快照最新时间的最大值封顶
            #    (用户显式传 date_to 则尊重用户范围),快照之后的新消息
            #    在 SQL 层被排除,翻页空间不再漂移;
            # 3. 同秒落库等漏网消息由 per-channel telegram_msg_id 上界
            #    二次过滤;期间仍可能变动的区间(如同秒回填)造成的页
            #    重叠按 (channel_id, telegram_msg_id) 去重兜底。
            #
            # 结果 ≈ 快照时刻的一致性视图;导出中「删除」消息仍可能漏导
            # (storage 无快照隔离,记录为已知边界)。
            channel_ids = list(channels.keys())
            boundaries: dict[int, int] = {}
            snapshot_date_to: datetime | None = None
            snapshot_key: datetime | None = None
            for cid in channel_ids:
                newest = await self._storage.list_messages(channel_ids=[cid], limit=1)
                if newest:
                    n = newest[-1]
                    boundaries[cid] = n.telegram_msg_id
                    nd = n.date if n.date.tzinfo else n.date.replace(tzinfo=UTC)
                    if snapshot_key is None or nd > snapshot_key:
                        snapshot_key = nd
                        snapshot_date_to = n.date
            eff_date_to = request.date_to if request.date_to is not None else snapshot_date_to
            seen_keys: set[tuple[int, int]] = set()
            offset = 0
            while channel_ids:
                raw = await self._storage.list_messages(
                    channel_ids=channel_ids,
                    date_from=request.date_from,
                    date_to=eff_date_to,
                    limit=PAGE_SIZE,
                    offset=offset,
                )
                if not raw:
                    break
                # offset 推进按 raw 页计 — 快照过滤不改变 offset 空间。
                offset += len(raw)
                for m in raw:
                    # 快照后新增的消息不入导出:telegram_msg_id 超出频道
                    # 上界(频道快照时为空 → 上界 -1 全拒;TG msg_id ≥ 1)。
                    if m.telegram_msg_id > boundaries.get(m.channel_id, -1):
                        continue
                    key = (m.channel_id, m.telegram_msg_id)
                    if key in seen_keys:  # 并发写入导致的页重叠 → 去重
                        continue
                    seen_keys.add(key)
                    all_messages.append(m)
                await self._bus.publish(
                    ExportProgress(request_id=req_id, written=offset, total=None)
                )
                yield
                # 最后一页:raw 不足 PAGE_SIZE 说明数据耗尽。
                if len(raw) < PAGE_SIZE:
                    break
                # 死循环兜底:offset 已超数据上限(防 storage 端 race / bug)。
                if offset > 10_000_000:
                    log.error("export pagination 超 10M 退出,channel_ids=%s", channel_ids)
                    break

            all_messages.sort(key=_message_sort_key)

            await self._bus.publish(
                ExportProgress(
                    request_id=req_id, written=len(all_messages), total=len(all_messages)
                )
            )

        try:
            exporter = EXPORTERS.get(request.format)
            # 2026-09-01 v1.5.1 PR #B4:ZIP 永远需要 object_store(没数据
            # 等于空包);其它 exporter 仅在 include_thumbnails=True 时传。
            # `self._objects: ObjectStore`(非 Optional)— ExportService
            # 构造时已 assert `objects is not None`;两个分支统一成
            # `ObjectStore | None`,再交给 `render` 的协议签名。
            object_store_arg: ObjectStore | None = (
                self._objects
                if request.format == ExportFormat.ZIP or request.include_thumbnails
                else None
            )
            bytes_written = await exporter.render(
                out_path,
                channels,
                all_messages,
                object_store=object_store_arg,
                include_thumbnails=request.include_thumbnails,
                include_metadata=request.include_metadata,  # 2026-09-10 v1.7.3
                storage=self._storage if request.include_thumbnails else None,  # 2026-09-29
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
            await self._bus.publish(ExportDone(request_id=req_id, error=str(e)))
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

        # 包成 exporter 期望的行列表(每条含目标 media + 其在原消息的下标)。
        # P2-M7:用正式 DTO `MediaExportRowDTO.media_idx` 取代旧的动态属性
        # `_media_idx`(两处 type: ignore)—— DTO purity 恢复,类型安全。
        wrapped: list[MessageDTO] = []
        for msg, idx, med in rows:
            fields = {f.name: getattr(msg, f.name) for f in dataclasses.fields(msg)}
            fields["media"] = [med]
            wrapped.append(MediaExportRowDTO(**fields, media_idx=idx))

        try:
            exporter = EXPORTERS.get(request.format)
            bytes_written = await exporter.render(
                out_path,
                channels,
                wrapped,
                object_store=self._objects,
                include_thumbnails=False,
                include_metadata=request.include_metadata,  # 2026-09-10 v1.7.3
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
            await self._bus.publish(ExportDone(request_id=req_id, error=str(e)))
            raise
