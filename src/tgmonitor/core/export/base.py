"""Exporter 抽象 + 全局注册表。

新增格式 = 写一个 Exporter 子类 + `EXPORTERS.register(YourExporter)`。
UI 之下拉框 / 调度都通过注册表拿,无需改 if/elif。

2026-11-26 插件系统阶段 2:注册 key 从 `ExportFormat` 放宽到 `str`。
`ExportFormat(str, Enum)` 的值与同名 str 哈希 / 相等,但 `.value` 访问与
`ExportFormat(name)` 反解对插件格式会炸 —— 注册表内部统一规范化为纯 str,
下游消费方(result / UI / CLI)不再依赖枚举实例。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from tgmonitor.core.dto import ChannelDTO, ExportFormat, MessageDTO

if TYPE_CHECKING:
    from tgmonitor.core.objectstore.base import ObjectStore
    from tgmonitor.core.storage.repository import StorageRepository


def _format_key(fmt: str | ExportFormat) -> str:
    """把 `str` / `str`-Enum 统一规范化为纯 str key。

    `ExportFormat` 是 `str, Enum`,枚举 member 的 hash 与 `.value` 相同,
    所以内置格式查询天然兼容;但插件 str 格式没有 `.value`,必须在唯一
    入口收敛,否则下游 `.value` / `ExportFormat(...)` 会炸。
    """
    return fmt.value if isinstance(fmt, Enum) else str(fmt)


class Exporter(ABC):
    """导出器接口。

    2026-11-25 P1-E:类级元数据描述格式能力,由注册表驱动 GUI 下拉 / CLI
    choices,消除"注册表非唯一真源"漂移(M2)。默认值保持向后兼容 ——
    现有 exporter 不声明也能工作。

    2026-11-26:`format` 注解放宽到 `str` —— 插件可提供 `ExportFormat`
    枚举之外的新格式,内置 exporter 继续用枚举成员(它是 str 子类)。
    """

    format: str
    extension: str = ""  # 输出文件扩展名(含点),如 ".json";空 = 调用方自定
    needs_object_store: bool = False  # render 是否必须有 object_store(ZIP=True)
    supports_thumbnails: bool = False  # 是否消费 include_thumbnails(HTML/ZIP=True)
    uses_media_dto: bool = False  # True = MediaExportRequest(per-media 行)而非消息导出

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
        storage: StorageRepository
        | None = None,  # 2026-09-29:thumb 走独立表,读时需要 storage.get_thumbnail
    ) -> int:
        """写出到 out_path,返回写入字节数。

        `include_metadata=True` 时导出 `is_favorite` / `tags` / `notes`(JSON / ZIP
        永远含,asdict 透明);False 时只走原始字段。默认 True 保持 v1.7.2
        视觉行为对 ★ / 标签 / 备注 敏感的用户不踩坑。

        `storage`(2026-09-29):缩略图独立表后,`include_thumbnails=True` 时需要
        用 `storage.get_thumbnail(channel_id, msg_id, media_idx)` 拿 thumb 行
        (而不是从 `media.thumb_key` 读);None 时 thumb 视图保持老风格 — 仅 ZIP
        / HTML 受影响。Service 层 `ExportService._run_messages` 在
        `include_thumbnails=True` 时总会传 storage。
        """
        ...


class ExporterRegistry:
    """Exporter 注册表:format(str) → Exporter 实例,进程内单例 (`EXPORTERS`)。"""

    def __init__(self) -> None:
        """空注册;由 `@exporter(...)` 装饰器填充。"""
        self._items: dict[str, Exporter] = {}

    def register(self, exporter: Exporter) -> str:
        """注册 Exporter,返回规范化 format key;重复抛 `ValueError`。

        归一化在唯一入口完成 —— 传 `ExportFormat.JSON` 与传 `"json"` 落到
        同一个 key,重名保护因此不会漏判。插件停用后 host 用返回的 key
        调 `unregister`,保证重激活不撞这里的保护。
        """
        fmt = _format_key(exporter.format)
        if fmt in self._items:
            raise ValueError(f"format {fmt} already registered")
        self._items[fmt] = exporter
        return fmt

    def unregister(self, fmt: str) -> bool:
        """注销 format;返回是否确有移除(不存在时静默 False,幂等)。

        2026-11-26:`EventBus.subscribe_all` 没有对称的 unsubscribe 导致
        host 必须自己记账 —— exporter 侧不重蹈覆辙,注册 / 注销成对提供,
        `deactivate_all` 与激活失败回滚都走这里。
        """
        return self._items.pop(_format_key(fmt), None) is not None

    def get(self, fmt: str) -> Exporter:
        """取 Exporter;无注册时抛 `KeyError`(消息含 available 列表)。"""
        key = _format_key(fmt)
        try:
            return self._items[key]
        except KeyError as e:
            raise KeyError(f"no exporter for format {key}; available: {list(self._items)}") from e

    def available(self) -> list[str]:
        """所有已注册 format(供 UI 下拉框用)。"""
        return list(self._items.keys())

    def message_formats(self) -> list[str]:
        """走 `ExportRequest`(per-message)的格式 —— GUI 消息导出下拉 / CLI 用。

        P1-E:`MEDIA_CSV` 走 `MediaExportRequest`(per-media 行),不属消息导出,
        从这里排除;新增消息格式只要 `@exporter` 注册即自动出现,无需改 UI/CLI。
        按 `ExportFormat` 枚举声明顺序排,保持下拉顺序向后兼容;插件格式没有
        枚举序,排末尾。
        """
        order = {_format_key(f): i for i, f in enumerate(ExportFormat)}
        return sorted(
            (f for f, ex in self._items.items() if not ex.uses_media_dto),
            key=lambda f: order.get(f, len(order)),
        )

    def media_formats(self) -> list[str]:
        """走 `MediaExportRequest`(per-media)的格式 —— CLI `export media` 用。"""
        order = {_format_key(f): i for i, f in enumerate(ExportFormat)}
        return sorted(
            (f for f, ex in self._items.items() if ex.uses_media_dto),
            key=lambda f: order.get(f, len(order)),
        )

    def extension(self, fmt: str) -> str:
        """格式的默认扩展名(含点),由 exporter 元数据声明。"""
        return self._items[_format_key(fmt)].extension

    def supports_thumbnails(self, fmt: str) -> bool:
        """格式是否消费 include_thumbnails(驱动 GUI 缩略图勾选框启用态)。"""
        return self._items[_format_key(fmt)].supports_thumbnails

    def needs_object_store(self, fmt: str) -> bool:
        """格式是否必须有 object_store 才能 render(驱动 service 传参)。"""
        return self._items[_format_key(fmt)].needs_object_store


EXPORTERS = ExporterRegistry()


def exporter(fmt: str | ExportFormat) -> Callable[[type[Exporter]], type[Exporter]]:
    """类装饰器:`@exporter(ExportFormat.JSON)`(插件也可传普通 str)。"""

    def _wrap(cls: type[Exporter]) -> type[Exporter]:
        cls.format = fmt
        EXPORTERS.register(cls())
        return cls

    return _wrap
