"""图标加载 — 从 SVG 资源生成 QIcon。

资源路径用 `importlib.resources.files()` 解析,源码期与 wheel 装包后都能找到。
SVG 直接交给 `QSvgRenderer` → `QPixmap`,无中间 PNG。

风格约定:
- **action icons**(`action_icon()`)统一到 Lucide (ISC/MIT)— stroke-width=1.75、
  currentColor、round caps/joins,见 `ATTRIBUTIONS.md`
- **app icon**(`load_app_icon()`)保持原设计(gradient 多色,产品身份),
  视觉上与 action icons 不同是故意的:工具栏走单色线条以让观感一致,
  但 dock / 任务栏需要一个有"重量"的标识
"""
from __future__ import annotations

from functools import lru_cache
from importlib import resources

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
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


@lru_cache(maxsize=8)
def action_icon(name: str) -> QIcon:
    """工具栏 / 列表项 / 状态指示图标(单色,follow currentColor)。

    `name` 对应 `resources/icons/<name>.svg`。目前注册:
    - 工具栏动作:`refresh` / `export` / `settings`
    - 频道类型(`ChannelWidget._kind_icon` 用):
      `kind_channel`(megaphone)/ `kind_supergroup`(users)/ `kind_group`(user-round)

    sizes=(16, 20, 24)三个档位覆盖 macOS Retina(16@2x=32)与 Windows
    标准 (16 / 20 / 24)显示环境。
    """
    return _svg_icon(f"{_ICONS_DIR}/{name}.svg", sizes=(16, 20, 24))


def _svg_icon(rel: str, *, sizes: tuple[int, ...]) -> QIcon:
    raw = _resource_bytes(rel)
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
