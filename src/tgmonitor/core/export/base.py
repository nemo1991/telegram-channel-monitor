"""Exporter 抽象 + 全局注册表。

新增格式 = 写一个 Exporter 子类 + `EXPORTERS.register(YourExporter)`。
UI 之下拉框 / 调度都通过注册表拿,无需改 if/elif。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from tgmonitor.core.dto import ChannelDTO, ExportFormat, MessageDTO

if TYPE_CHECKING:
    from tgmonitor.core.objectstore.base import ObjectStore
    from tgmonitor.core.storage.repository import StorageRepository


class Exporter(ABC):
    """导出器接口。"""

    format: ExportFormat

    @abstractmethod
    async def render(
        self,
        out_path: Path,
        channels: dict[int, ChannelDTO],
        messages: list[MessageDTO],
        *,
        object_store: ObjectStore | None = None,
        include_thumbnails: bool = False,
        include_metadata: bool = True,  # 2026-09-10 v1.7.3:CSV / Markdown / HTML / MEDIA_CSV 控元数据列
        storage: StorageRepository | None = None,  # 2026-09-29:thumb 走独立表,读时需要 storage.get_thumbnail
    ) -> int:
        """写出到 out_path,返回写入字节数。

        `include_metadata=True` 时导出 `is_favorite` / `tags` / `notes`(JSON / ZIP
        永远含,asdict 透明);False 时只走原始字段。默认 True 保持 v1.7.2
        视觉行为对 ★ / 🏷 / 📝 敏感的用户不踩坑。

        `storage`(2026-09-29):缩略图独立表后,`include_thumbnails=True` 时需要
        用 `storage.get_thumbnail(channel_id, msg_id, media_idx)` 拿 thumb 行
        (而不是从 `media.thumb_key` 读);None 时 thumb 视图保持老风格 — 仅 ZIP
        / HTML 受影响。Service 层 `ExportService._run_messages` 在
        `include_thumbnails=True` 时总会传 storage。
        """
        ...


class ExporterRegistry:
    """Exporter 注册表:format → Exporter 实例,进程内单例 (`EXPORTERS`)。"""

    def __init__(self) -> None:
        """空注册;由 `@exporter(...)` 装饰器填充。"""
        self._items: dict[ExportFormat, Exporter] = {}

    def register(self, exporter: Exporter) -> None:
        """注册 Exporter;format 重复抛 `ValueError`(防双注册)。"""
        if exporter.format in self._items:
            raise ValueError(f"format {exporter.format} already registered")
        self._items[exporter.format] = exporter

    def get(self, fmt: ExportFormat) -> Exporter:
        """取 Exporter;无注册时抛 `KeyError`(消息含 available 列表)。"""
        try:
            return self._items[fmt]
        except KeyError as e:
            raise KeyError(f"no exporter for format {fmt}; available: {list(self._items)}") from e

    def available(self) -> list[ExportFormat]:
        """所有已注册 format(供 UI 下拉框用)。"""
        return list(self._items.keys())


EXPORTERS = ExporterRegistry()


def exporter(fmt: ExportFormat) -> Callable[[type[Exporter]], type[Exporter]]:
    """类装饰器:`@exporter(ExportFormat.JSON)`。"""

    def _wrap(cls: type[Exporter]) -> type[Exporter]:
        cls.format = fmt
        EXPORTERS.register(cls())
        return cls

    return _wrap
