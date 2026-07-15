"""图标加载 — 从 SVG 资源生成 QIcon。

资源路径用 `importlib.resources.files()` 解析,源码期与 wheel 装包后都能找到。
SVG 直接交给 `QSvgRenderer` → `QPixmap`,无中间 PNG。
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
    """应用主图标(macOS dock / Windows 任务栏 / 窗口左上角)。"""
    return _svg_icon("app_icon.svg", sizes=(16, 24, 32, 48, 64, 128, 256))


@lru_cache(maxsize=8)
def action_icon(name: str) -> QIcon:
    """工具栏动作图标(单色,用 currentColor 上色)。

    `name` 对应 `resources/icons/<name>.svg`。
    """
    return _svg_icon(f"{_ICONS_DIR}/{name}.svg", sizes=(16, 24))


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
