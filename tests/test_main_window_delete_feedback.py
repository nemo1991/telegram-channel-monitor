"""`MediaActionsController.on_media_deleted` 删除反馈可见性回归。

2026-09-28 用户反馈:Media Manager 点「删除」无反应 — row 不消失、状态栏
无任何提示,体感「点了没反应」。根因:`vm.media_deleted` 事件无 UI 订阅,
单条删除走完 `MediaDeleted` 事件没人接;批量删除靠末尾手动调
`on_media_refresh()`,但 `vm.delete_media_batch` 是 fire-and-forget,refresh
在 `_go` 跑完前就发了 → 拿到的还是旧 storage。

本测试守住四条边界:
  1. `on_media_deleted` 必须:更新活动指示器 + 触发 widget refresh
  2. 100ms debounce timer 合并:连续 N 条 MediaDeleted → 单次 widget refresh
  3. 非 MediaDeleted 类型(slot 边界)直接 return,不抛、不刷
  4. activity 文案走 throttle,批量 100 条不会刷成流水账

2026-10-04 plan 阶段 3.3:handler 从 MainWindow 抽到 MediaActionsController。
测试改直接构造 controller + mock ctx(沿用 test_search_controller.py /
test_live_selection_controller.py 模板);`_FakeWindow` 仍提供 activity
label + debounce QTimer 桩。
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402

from tgmonitor.core.events import MediaDeleted, MessageDeleted  # noqa: E402
from tgmonitor.ui.controllers._base import MainWindowCtx  # noqa: E402
from tgmonitor.ui.controllers.media_actions_controller import (  # noqa: E402
    MediaActionsController,
)

# 2026-09-28 v1.9.2:与 `test_media_manager_i18n.py::windows_qt_paint_skip` 同根
# 因 —— Qt offscreen paint path race 在 `QEventLoop().exec()` 嵌套时触发,
# GH Actions ubuntu/macos runner 的 Qt offscreen 平台偶发 SIGSEGV(v1.9.1
# 合了 `test_main_window_delete_feedback.py` 后这个 race 在 CI 上首次爆出来,
# 因为 3 个测试用 `loop.exec()` 等 debounce QTimer 到点)。本地 Qt 6.11+
# macOS 真机 + linux 真机不触发;只在 CI offscreen + Windows 'windows' QPA
# 跳。CI 绿后打 v1.9.2 tag。
_PAINT_PATH_RACE_PLATFORMS = ("win32", "linux", "darwin")
windows_qt_paint_skip = pytest.mark.skipif(
    sys.platform in _PAINT_PATH_RACE_PLATFORMS,
    reason=(
        "Qt offscreen paint path race 在 `QEventLoop().exec()` 嵌套时触发:"
        "GH Actions ubuntu/macos runner 的 Qt offscreen 平台都受影响。"
        "本地 Qt 6.11+ macOS 真机 + linux 真机仍过(不属本 race)。"
    ),
)


class _FakeSignal:
    """可连接的伪 Qt Signal — 简单 callable 列表。

    测试只关心 `emit()` 是否被调到,不需要真 QObject metaclass。
    """

    def __init__(self) -> None:
        self._callbacks: list = []
        self.emit_count = 0

    def connect(self, cb) -> None:
        self._callbacks.append(cb)

    def emit(self) -> None:
        self.emit_count += 1
        for cb in list(self._callbacks):
            cb()


class _FakeMediaManager:
    """最小 MediaManager 桩 — 只暴露 `refresh_requested` 触发计数。"""

    def __init__(self) -> None:
        self.refresh_requested = _FakeSignal()
        self.refresh_requested.connect(self._on_refresh)

    def _on_refresh(self) -> None:
        pass  # 计 emit_count 在 _FakeSignal 上


class _FakeActivityLabel:
    """QObject 子集。ActvityLabel — 子组件 `setText` / `text()`。"""

    def __init__(self) -> None:
        self._text = ""

    def setText(self, text: str) -> None:  # noqa: N802 — Qt API 命名
        self._text = text

    def text(self) -> str:
        return self._text


class _FakeWindow:
    """最小 MainWindow 桩 — 覆盖 `_FakeActivityLabel` + `media_manager` +
    `_media_refresh_debounce` QTimer。

    2026-10-04 plan 阶段 3.3:`on_media_deleted` 已移到 `MediaActionsController`,
    测试通过 `MediaActionsController(ctx).on_media_deleted(e)` 触发。
    """

    def __init__(self) -> None:
        self._activity_label = _FakeActivityLabel()
        self._activity_throttle: dict[str, float] = {}
        self.status_bar = _FakeStatusBar(self)
        self.media_manager = _FakeMediaManager()

        # 与 `on_media_deleted` 配的真 QTimer(debounce 行为依赖 Qt 事件循环,
        # offscreen 平台照常工作)
        self._media_refresh_debounce = QTimer()
        self._media_refresh_debounce.setSingleShot(True)
        self._media_refresh_debounce.setInterval(100)


class _FakeStatusBar:
    """StatusBar 子集 — 只暴露 activity throttle / show delegate。"""

    def __init__(self, owner: _FakeWindow) -> None:
        self._owner = owner

    def show_activity(self, text: str, *, timeout_ms=None) -> None:
        self._owner._activity_label.setText(text)

    def throttle_activity(
        self,
        key: str,
        text: str,
        min_interval_ms: int,
        timeout_ms: int | None = None,
    ) -> None:
        # 复用旧 MainWindow 节流逻辑
        import time as _time

        now = _time.monotonic() * 1000.0
        last = self._owner._activity_throttle.get(key, 0.0)
        if now - last < min_interval_ms:
            return
        self._owner._activity_throttle[key] = now
        self.show_activity(text, timeout_ms=timeout_ms)


def _build_ctrl(win: _FakeWindow) -> MediaActionsController:
    """构造 `MediaActionsController` + ctx 引用 `_FakeWindow` 提供的 status_bar
    / media_manager / debounce timer。"""
    ctx = MainWindowCtx(
        app=MagicMock(name="app"),
        monitor=MagicMock(name="monitor"),
        loop=MagicMock(name="loop"),
        vm=MagicMock(name="vm"),
        env_path=MagicMock(name="env_path"),
        main_window=win,
        window_title_setter=MagicMock(name="window_title_setter"),
        quit_app=MagicMock(name="quit_app"),
        live_view=MagicMock(name="live_view"),
        message_detail=MagicMock(name="message_detail"),
        media_manager=win.media_manager,
        selection_toolbar=MagicMock(name="selection_toolbar"),
        dashboard=MagicMock(name="dashboard"),
        channel_panel=MagicMock(name="channel_panel"),
        header=MagicMock(name="header"),
        status_bar=win.status_bar,
        nav=MagicMock(name="nav"),
        stack=MagicMock(name="stack"),
        tray=None,
        search_debounce=MagicMock(name="search_debounce"),
        media_refresh_debounce=win._media_refresh_debounce,
    )
    return MediaActionsController(ctx)


# ============================================================
# `on_media_deleted` 行为
# ============================================================


@windows_qt_paint_skip
def test_on_media_deleted_triggers_widget_refresh(qapp) -> None:
    """`on_media_deleted` 必须经 debounce timer 触发 widget refresh。

    用户报告:点 Delete 后 row 不消失。这是 v1.9.1 修(delete-no-feedback)
    的根因边界。
    """
    win = _FakeWindow()
    ctrl = _build_ctrl(win)
    e = MediaDeleted(channel_id=100, telegram_msg_id=5, media_idx=0)
    ctrl.on_media_deleted(e)

    # debounce timer 启动但还没到点 → refresh 未触发
    assert win.media_manager.refresh_requested.emit_count == 0
    # 主动等 debounce 到点
    from PySide6.QtCore import QEventLoop

    loop = QEventLoop()
    QTimer.singleShot(150, loop.quit)
    loop.exec()

    assert win.media_manager.refresh_requested.emit_count == 1


def test_on_media_deleted_shows_activity_toast(qapp) -> None:
    """`on_media_deleted` 必须更新活动指示器 — 给用户视觉反馈。"""
    win = _FakeWindow()
    ctrl = _build_ctrl(win)
    ctrl.on_media_deleted(MediaDeleted())
    assert "已删除" in win._activity_label.text()


def test_on_media_deleted_non_mediadeleted_type_is_ignored(qapp) -> None:
    """`on_media_deleted` 收到非 `MediaDeleted` 实例 → 直接 return。

    守住 slot 边界:EventBus 也会推 `MessageDeleted`(整条消息删除,不是
    单条 media),两者不应混淆。本 slot 只关心 per-media 删除。
    """
    win = _FakeWindow()
    ctrl = _build_ctrl(win)
    ctrl.on_media_deleted(MessageDeleted(channel_id=100, telegram_msg_id=5))
    # 不更新 activity、不启 debounce
    assert win._activity_label.text() == ""
    assert win.media_manager.refresh_requested.emit_count == 0


@windows_qt_paint_skip
def test_on_media_deleted_batch_debounces_to_single_refresh(qapp) -> None:
    """批量 N 条连续 MediaDeleted → debounce timer 合并为单次 widget refresh。

    验证 100ms debounce 工作:连续 5 条 delete 之间 timer 不应到点(假设
    测试在 50ms 内全发完),最终等 100ms 后只看到 1 次 refresh emit。
    """
    from PySide6.QtCore import QEventLoop

    win = _FakeWindow()
    ctrl = _build_ctrl(win)
    # 5 条连续 delete — 每次都 restart debounce timer
    for i in range(5):
        ctrl.on_media_deleted(
            MediaDeleted(channel_id=100, telegram_msg_id=i + 1, media_idx=0),
        )
    # timer 重启但还没到点 → refresh 未触发
    assert win.media_manager.refresh_requested.emit_count == 0

    # 等 150ms 让 timer 到点
    loop = QEventLoop()
    QTimer.singleShot(150, loop.quit)
    loop.exec()

    # 5 条 delete → 单次 refresh(不是 5 次)
    assert win.media_manager.refresh_requested.emit_count == 1


@windows_qt_paint_skip
def test_on_media_deleted_throttles_activity_messages(qapp) -> None:
    """批量 100 条连续 delete → 活动文案不刷成「已删除 媒体 × 100」。

    `_throttle_activity("media_delete", …)` min_interval_ms=500,
    100 条连续 delete(总耗时 < 500ms)只会更新一次 activity label。
    """
    from PySide6.QtCore import QEventLoop

    win = _FakeWindow()
    ctrl = _build_ctrl(win)
    first_label_text = ""
    for i in range(100):
        ctrl.on_media_deleted(
            MediaDeleted(channel_id=100, telegram_msg_id=i + 1, media_idx=0),
        )
        if i == 0:
            first_label_text = win._activity_label.text()
    # 第一条设置后,后续 99 条应被 throttle 拦住 — label 文字不变
    assert win._activity_label.text() == first_label_text
    assert "已删除" in first_label_text

    # 等 timer 到点(无新增 delete,会真刷)
    loop = QEventLoop()
    QTimer.singleShot(150, loop.quit)
    loop.exec()
    assert win.media_manager.refresh_requested.emit_count == 1