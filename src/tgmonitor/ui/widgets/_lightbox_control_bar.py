# mypy: disable-error-code="attr-defined"
"""Lightbox 底部控制条构建 / 淡入淡出 — 2026-11-25 P2-N5b。

从 `lightbox_dialog.py`(992 行)抽出控制条的 8 个构建/展示方法,用
free function + host 透传模式(同 telethon handler / PG 分域 module):

- 宿主 `LightboxDialog` 保留 `_build_control_bar` / `_show_bar` /
  `_fade_out_bar` 等方法名,方法体改为 1 行薄委托
- 所有控件仍挂在宿主上(`host._control_bar` / `host._btn_*` /
  `host._bar_opacity_effect` / `host._hide_timer`)—— 测试直接访问这些
  私有属性,几何与信号连线逐字不变
- 本模块不 import 宿主类(用 `host: Any`),无循环依赖

行为完全不变:stylesheet、按钮顺序、tooltip、timer 间隔、opacity 通道逐字搬移。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QPushButton,
)


def build_control_bar(host: Any) -> None:
    """构建底部工具栏 + 加进宿主 layout(align bottom-center)。"""
    init_control_bar_frame(host)
    init_control_buttons(host)
    wire_control_buttons(host)
    init_bar_opacity_and_timer(host)
    # stretch=0 不抢画布空间;_canvas(stretch=1)撑满中间区域
    outer = host.layout()
    assert outer is not None
    outer.addWidget(host._control_bar)
    outer.setAlignment(host._control_bar, Qt.AlignBottom | Qt.AlignHCenter)


def init_control_bar_frame(host: Any) -> None:
    """QFrame + stylesheet + QHBoxLayout 容器,半透黑底白字风格。"""
    host._control_bar = QFrame(host)
    host._control_bar.setStyleSheet(
        "QFrame { background-color: rgba(0, 0, 0, 180); border-radius: 6px; }"
        "QPushButton { color: white; background: transparent; border: none;"
        " padding: 6px 12px; font-size: 16px; }"
        "QPushButton:hover { background-color: rgba(255, 255, 255, 30); }"
        "QPushButton:disabled { color: rgba(255, 255, 255, 80); }"
    )
    bar = QHBoxLayout(host._control_bar)
    bar.setContentsMargins(12, 6, 12, 6)
    bar.setSpacing(4)


def init_control_buttons(host: Any) -> None:
    """建 9 个按钮(prev/next/zoom_out/zoom_in/rotate/play_pause/slideshow/save/close)+ 布局。"""
    host._btn_prev = make_btn(host, "‹", host.tr("上一张"))
    host._btn_next = make_btn(host, "›", host.tr("下一张"))
    host._btn_zoom_out = make_btn(host, "−", host.tr("缩小"))
    host._btn_zoom_in = make_btn(host, "＋", host.tr("放大"))
    host._btn_rotate = make_btn(host, "⟳", host.tr("旋转 90°"))
    # 2026-09-09 v1.7.2:▶/⏸ 按钮 — gif / video 播放切换。
    host._btn_play_pause = make_btn(host, "▶", host.tr("播放/暂停"))
    # 2026-09-09 v1.7.2:⏯ 幻灯片切换(checkable)。
    host._btn_slideshow = make_btn(host, "⏯", host.tr("幻灯片"))
    host._btn_slideshow.setCheckable(True)
    host._btn_save = make_btn(host, "⤓", host.tr("另存为…"))
    host._btn_close = make_btn(host, "关闭", host.tr("关闭"))

    bar = host._control_bar.layout()
    assert isinstance(bar, QHBoxLayout)
    bar.addWidget(host._btn_prev)
    bar.addWidget(host._btn_next)
    bar.addSpacing(16)
    bar.addWidget(host._btn_zoom_out)
    bar.addWidget(host._btn_zoom_in)
    bar.addSpacing(16)
    bar.addWidget(host._btn_rotate)
    bar.addWidget(host._btn_play_pause)
    bar.addWidget(host._btn_slideshow)
    bar.addSpacing(16)
    bar.addWidget(host._btn_save)
    bar.addWidget(host._btn_close)


def wire_control_buttons(host: Any) -> None:
    """9 个按钮 clicked/toggled 信号接 handler。"""
    host._btn_prev.clicked.connect(lambda: host._step_index(-1))
    host._btn_next.clicked.connect(lambda: host._step_index(+1))
    host._btn_zoom_out.clicked.connect(host._zoom_out)
    host._btn_zoom_in.clicked.connect(host._zoom_in)
    host._btn_rotate.clicked.connect(host._rotate_90)
    host._btn_play_pause.clicked.connect(host._toggle_play_pause)
    host._btn_slideshow.toggled.connect(host._toggle_slideshow)
    host._btn_save.clicked.connect(host._save_current)
    host._btn_close.clicked.connect(host.accept)


def init_bar_opacity_and_timer(host: Any) -> None:
    """QGraphicsOpacityEffect + 2s 淡出 QTimer(避开 QFrame setWindowOpacity 无效坑)。"""
    host._control_bar.hide()
    host._bar_opacity_effect = QGraphicsOpacityEffect(host._control_bar)
    host._bar_opacity_effect.setOpacity(0.0)
    host._control_bar.setGraphicsEffect(host._bar_opacity_effect)
    host._control_bar.adjustSize()

    host._hide_timer = QTimer(host)
    host._hide_timer.setSingleShot(True)
    host._hide_timer.setInterval(2000)
    host._hide_timer.timeout.connect(host._fade_out_bar)


def make_btn(host: Any, text: str, tooltip: str) -> QPushButton:
    """单按钮工厂 — NoFocus 防抢键盘焦点,左右方向键仍走 keyPressEvent 翻页。"""
    btn = QPushButton(text, host._control_bar)
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setFocusPolicy(Qt.NoFocus)
    btn.setFixedHeight(32)
    return btn


def show_bar(host: Any) -> None:
    """鼠标动 → bar 渐显 + 重启 2s timer。"""
    if host._control_bar is None:
        return
    host._control_bar.show()
    host._bar_opacity_effect.setOpacity(1.0)
    host._hide_timer.start()


def fade_out_bar(host: Any) -> None:
    """2s 不动 → bar 渐隐(走 graphics effect 通道,全平台一致)。"""
    if host._control_bar is None:
        return
    host._bar_opacity_effect.setOpacity(0.0)
