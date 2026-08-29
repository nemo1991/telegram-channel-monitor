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
    ) -> int:
        """写出到 out_path,返回写入字节数。"""
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
