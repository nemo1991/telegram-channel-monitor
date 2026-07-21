"""图标加载 — 从 SVG 资源生成 QIcon。

资源路径用 `importlib.resources.files()` 解析,源码期与 wheel 装包后都能找到。
SVG 直接交给 `QSvgRenderer` → `QPixmap`,无中间 PNG。

风格约定:
- **action icons**(`action_icon()` / `tinted_action_icon()`)统一到 Lucide
  (ISC/MIT)— stroke-width=1.75、currentColor、round caps/joins,见 `ATTRIBUTIONS.md`
- **app icon**(`load_app_icon()`)保持原设计(gradient 多色,产品身份),
  视觉上与 action icons 不同是故意的:工具栏走单色线条以让观感一致,
  但 dock / 任务栏需要一个有"重量"的标识

注意:Qt 的 `QSvgRenderer.render(painter)` **不解析** SVG 的 `currentColor`
关键字 —— 它会原样画,导致 SVG 在 painter 上变成 Qt 默认黑色。要让
`currentColor` 真正生效,必须在 renderer 之前把 `currentColor` 字面量替换
成目标 QColor 的 name(rgb)。这就是 `tinted_action_icon()` 干的事。
"""
from __future__ import annotations

from functools import lru_cache
from importlib import resources

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_RESOURCES = "tgmonitor.resources"
_ICONS_DIR = "icons"


def _resource_bytes(rel: str) -> bytes:
    """读 pkg 资源(开发期 + wheel 都行)。"""
    return resources.files(_RESOURCES).joinpath(rel).read_bytes()


@lru_cache(maxsize=8)
def load_app_icon() -> QIcon:
    """应用主图标(macOS dock / Windows 任务栏 / 窗口左上角)。

    app_icon.svg 是多色 gradient,与 action icons 单色风不同;刻意保留差异
    —— 它是产品身份,不应受工具栏一致性的约束。
    """
    return _svg_icon("app_icon.svg", sizes=(16, 24, 32, 48, 64, 128, 256))


@lru_cache(maxsize=16)
def action_icon(name: str) -> QIcon:
    """工具栏 / 列表项 / 状态指示图标(单色,无 tint — 由调用方 QSS 控制)。

    **警告**:Qt `QSvgRenderer` 不解析 `currentColor` — 渲染出来是黑色。
    这个函数保留作为"调用方愿意自己用 QSS 控色"的入口,实际项目内
    大多数调用点应该用 `tinted_action_icon(name, QColor)`,直接把 fg
    色注入 SVG,避免 QLabel 链路上 currentColor 失效。

    `name` 对应 `resources/icons/<name>.svg`。目前注册:
    - 工具栏动作:`refresh` / `export` / `settings`
    - 频道类型:`kind_channel` / `kind_supergroup` / `kind_group`
    - 导航栏:`nav_live` / `nav_dashboard` / `nav_channels` / `nav_settings`

    sizes=(16, 20, 24)三个档位覆盖 macOS Retina(16@2x=32)与 Windows
    标准 (16 / 20 / 24)显示环境。
    """
    return _svg_icon(f"{_ICONS_DIR}/{name}.svg", sizes=(16, 20, 24))


def tinted_action_icon(name: str, color: QColor) -> QIcon:
    """带 fg 色的 action icon。**这是项目内推荐入口**。

    实现:在 SVG 字节上把 `currentColor` 替换成 `color.name()`(e.g. "#b0b5c8")
    再交给 `QSvgRenderer`。替换后 SVG 不再依赖 Qt 的 currentColor 解析,
    在所有 painter / QLabel / QSS 场景下都稳定输出目标色。

    调色版用 lru_cache 缓存:`(name, QColor.name())` 作 key,跨页面复用
    同一份 QIcon 实例。
    """
    return _tinted_action_icon_cached(name, color.name())


@lru_cache(maxsize=64)
def _tinted_action_icon_cached(name: str, color_name: str) -> QIcon:
    return _svg_icon(
        f"{_ICONS_DIR}/{name}.svg",
        sizes=(16, 20, 24),
        tint=QColor(color_name),
    )


def _svg_icon(
    rel: str,
    *,
    sizes: tuple[int, ...],
    tint: QColor | None = None,
) -> QIcon:
    raw = _resource_bytes(rel)
    if tint is not None:
        # `currentColor` 在 SVG spec 里就是关键字;Qt 不解析,直接画黑。
        # 把它替换成显式 hex,name() 给 "#rrggbb" 形式,SVG / 渲染器都吃。
        raw = raw.replace(b"currentColor", tint.name().encode("ascii"))
    renderer = QSvgRenderer(raw)
    icon = QIcon()
    for size in sizes:
        pm = QPixmap(QSize(size, size))
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        renderer.render(p)
        p.end()
        icon.addPixmap(pm)
    return icon
