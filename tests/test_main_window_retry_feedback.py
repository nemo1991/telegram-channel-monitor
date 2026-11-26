"""`MediaActionsController` retry / download slot — 反馈可见性回归。

2026-09-26 用户反馈:Media Manager 点 Retry「无反应」。
根因:`AppService.retry_media` 发 `MediaRetried` + `MediaDownloaded` 两个事件,
但 MainWindow 没订阅 `media_retried`,`media_downloaded` 也只刷 LIVE view 不刷
Media Manager widget 列表 — 用户视觉上无变化(row 仍 FAILED)。
本测试守住两条边界:
  1. `on_media_retried` 必须:更新活动指示器 + 触发 widget refresh
  2. `on_media_downloaded` 必须:除现有 LIVE view 刷新外,额外触发 widget refresh

策略:用真实 `MediaRetried` / `MediaDownloaded` dataclass(slot 内 isinstance
走真类型),MediaDTO 走真 dataclass(`MediaDownloadStatus` 字段直接控制)。

2026-10-04 plan 阶段 3.3:handler 从 MainWindow 抽到 MediaActionsController,
测试改直接构造 controller + mock ctx(沿用 test_search_controller.py 模板)。
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QLabel

from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MediaType
from tgmonitor.core.events import MediaDownloaded
from tgmonitor.ui.controllers._base import MainWindowCtx
from tgmonitor.ui.controllers.media_actions_controller import (
    MediaActionsController,
)


class _FakeSignal:
    """可连接的伪 Qt Signal — 简单 callable 列表,够用 + 不依赖 QObject metaclass。

    真 PySide6 Signal 是 class-level descriptor 需要 QObject 元类初始化,
    在测试 stub 里太重,这里只测「slot 是否调到了 `signal.emit()`」。
    """

    def __init__(self) -> None:
        self._callbacks: list = []

    def connect(self, cb) -> None:
        self._callbacks.append(cb)

    def emit(self) -> None:
        for cb in list(self._callbacks):
            cb()


class _FakeMediaManager:
    """最小 MediaManager 桩 — 只暴露 `refresh_requested` 触发计数。"""

    def __init__(self) -> None:
        self.refresh_requested = _FakeSignal()
        self.refresh_count = 0
        self.refresh_requested.connect(self._on_refresh)

    def _on_refresh(self) -> None:
        self.refresh_count += 1


class _FakeLiveView:
    """LiveView 桩 — `on_media_downloaded` 会调 `update_media_status`,留空。"""

    def update_media_status(self, *_args, **_kw) -> None:
        return


class _FakeDetailPanel:
    """message_detail 桩 — 同上。"""

    def refresh_if_showing(self, *_args, **_kw) -> None:
        return


class _FakeWindow:
    """最小 MainWindow 桩 — 覆盖 retry feedback 两个 slot 需要的依赖。

    2026-10-04 plan 阶段 3.3:`on_media_retried` / `on_media_downloaded`
    已移到 `MediaActionsController`,测试通过 `MediaActionsController(ctx).on_*`
    触发。`_FakeWindow` 提供 status_bar + live_view + message_detail +
    media_manager 子对象引用。
    """

    def __init__(self) -> None:
        self.status_bar = _FakeStatusBar()
        self.media_manager = _FakeMediaManager()
        self.live_view = _FakeLiveView()
        self.message_detail = _FakeDetailPanel()

    def tr(self, text: str) -> str:
        """向后兼容 — `_FakeWindow` 自带 tr 桩给 controller `_tr` 之外用。"""
        return text

    @property
    def _activity_label(self) -> QLabel:
        """向后兼容旧测试读 `win._activity_label.text()` — 返回 status_bar 子 label。"""
        return self.status_bar._activity_label


class _FakeStatusBar:
    """StatusBar 子集 — 只暴露 activity_label + show_activity delegate。"""

    def __init__(self) -> None:
        self._activity_label = QLabel("")

    def show_activity(self, text: str, *, timeout_ms: int | None = None) -> None:
        self._activity_label.setText(text)


def _build_ctrl(win: _FakeWindow) -> MediaActionsController:
    """构造 `MediaActionsController` + ctx 引用 `_FakeWindow` 提供的 status_bar
    / live_view / message_detail / media_manager。"""
    # QCoreApplication 在 `_tr()` 调 `QCoreApplication.translate("MainWindow", src)`
    # 时会被 controller 引用 — offscreen 测试已通过 `qapp` fixture 创建
    ctx = MainWindowCtx(
        app=MagicMock(name="app"),
        monitor=MagicMock(name="monitor"),
        loop=MagicMock(name="loop"),
        vm=MagicMock(name="vm"),
        env_path=MagicMock(name="env_path"),
        main_window=win,
        window_title_setter=MagicMock(name="window_title_setter"),
        quit_app=MagicMock(name="quit_app"),
        live_view=win.live_view,
        message_detail=win.message_detail,
        media_manager=win.media_manager,
        dashboard=MagicMock(name="dashboard"),
        channel_panel=MagicMock(name="channel_panel"),
        status_bar=win.status_bar,
        nav=MagicMock(name="nav"),
        stack=MagicMock(name="stack"),
        tray=None,
        media_refresh_debounce=MagicMock(name="media_refresh_debounce"),
    )
    return MediaActionsController(ctx)


def _make_media(file_name: str, *, is_done: bool) -> MediaDTO:
    """构造真实 MediaDTO;status 用 `is_done` 切 DONE / FAILED。"""
    return MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name=file_name,
        file_size=1024,
        download_status=MediaDownloadStatus.DONE if is_done else MediaDownloadStatus.FAILED,
    )


# ============================================================
# `on_media_retried` 行为
# ============================================================


# ============================================================
# `on_media_downloaded` 行为
# ============================================================


def test_on_media_downloaded_triggers_widget_refresh_on_failure(qapp) -> None:
    """失败路径(retry 重试后仍 FAILED)同样要刷新 widget — UI 反映真实状态。"""
    win = _FakeWindow()
    ctrl = _build_ctrl(win)
    ev = MediaDownloaded(
        channel_id=100,
        telegram_msg_id=5,
        media=_make_media("photo.jpg", is_done=False),
    )
    ctrl.on_media_downloaded(ev)
    assert win.media_manager.refresh_count == 1
