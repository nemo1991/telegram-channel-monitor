"""MainWindow MediaDeleted slot — 删除反馈可见性回归。

2026-09-28 用户反馈:Media Manager 点「删除」无反应 — row 不消失、状态栏
无任何提示,体感「点了没反应」。根因:`vm.media_deleted` 事件无 UI 订阅,
单条删除走完 `MediaDeleted` 事件没人接;批量删除靠末尾手动调
`_on_media_refresh()`,但 `vm.delete_media_batch` 是 fire-and-forget,refresh
在 `_go` 跑完前就发了 → 拿到的还是旧 storage。

本测试守住四条边界:
  1. `_on_media_deleted` 必须:更新活动指示器 + 触发 widget refresh
  2. 100ms debounce timer 合并:连续 N 条 MediaDeleted → 单次 widget refresh
  3. 非 MediaDeleted 类型(slot 边界)直接 return,不抛、不刷
  4. activity 文案走 throttle,批量 100 条不会刷成流水账
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402

from tgmonitor.core.events import MediaDeleted, MessageDeleted  # noqa: E402
from tgmonitor.ui.main_window import MainWindow  # noqa: E402


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


class _FakeWindow:
    """最小 MainWindow 桩 — 覆盖 _on_media_deleted slot 需要的依赖。

    QTimer parent=self — 测试只构造桩,QApplication 由 conftest 兜底。
    """

    def __init__(self) -> None:
        self._activity_label_text = ""

        class _Lbl:
            def setText(self, text: str) -> None:
                self.owner._activity_label_text = text

            def text(self) -> str:  # 用于断言
                return self.owner._activity_label_text

            def __init__(self, owner: "_FakeWindow") -> None:
                self.owner = owner

        self._activity_label = _Lbl(self)
        self._activity_throttle: dict[str, float] = {}
        self.media_manager = _FakeMediaManager()

        # 与 _on_media_deleted / _flush_media_refresh_pending 配的真 QTimer
        # (debounce 行为依赖 Qt 事件循环,offscreen 平台照常工作)
        self._media_refresh_debounce = QTimer()
        self._media_refresh_debounce.setSingleShot(True)
        self._media_refresh_debounce.setInterval(100)
        self._media_refresh_debounce.timeout.connect(self._flush_media_refresh_pending)

    def tr(self, text: str) -> str:
        """slot 内 `self.tr("…")` 调用 — fake 不挂 QObject,走 identity。"""
        return text

    # 绑 MainWindow 上的 unbound method
    _show_activity = MainWindow._show_activity
    _throttle_activity = MainWindow._throttle_activity
    _flush_media_refresh_pending = MainWindow._flush_media_refresh_pending


# ============================================================
# `_on_media_deleted` 行为
# ============================================================


def test_on_media_deleted_triggers_widget_refresh(qapp) -> None:
    """`_on_media_deleted` 必须经 debounce timer 触发 widget refresh。

    用户报告:点 Delete 后 row 不消失。这是 v1.9.1 修(delete-no-feedback)
    的根因边界。
    """
    win = _FakeWindow()
    e = MediaDeleted(channel_id=100, telegram_msg_id=5, media_idx=0)
    MainWindow._on_media_deleted(win, e)

    # debounce timer 启动但还没到点 → refresh 未触发
    assert win.media_manager.refresh_requested.emit_count == 0
    # 等 110ms 让 debounce 到点
    QTimer.singleShot(150, lambda: None)  # dummy 让事件循环跑
    # 主动等 debounce 到点
    from PySide6.QtCore import QEventLoop

    loop = QEventLoop()
    QTimer.singleShot(150, loop.quit)
    loop.exec()

    assert win.media_manager.refresh_requested.emit_count == 1


def test_on_media_deleted_shows_activity_toast(qapp) -> None:
    """`_on_media_deleted` 必须更新活动指示器 — 给用户视觉反馈。"""
    win = _FakeWindow()
    MainWindow._on_media_deleted(win, MediaDeleted())
    assert "已删除" in win._activity_label.text()


def test_on_media_deleted_non_mediadeleted_type_is_ignored(qapp) -> None:
    """`_on_media_deleted` 收到非 `MediaDeleted` 实例 → 直接 return。

    守住 slot 边界:EventBus 也会推 `MessageDeleted`(整条消息删除,不是
    单条 media),两者不应混淆。本 slot 只关心 per-media 删除。
    """
    win = _FakeWindow()
    MainWindow._on_media_deleted(win, MessageDeleted(channel_id=100, telegram_msg_id=5))
    # 不更新 activity、不启 debounce
    assert win._activity_label.text() == ""
    assert win.media_manager.refresh_requested.emit_count == 0


def test_on_media_deleted_batch_debounces_to_single_refresh(qapp) -> None:
    """批量 N 条连续 MediaDeleted → debounce timer 合并为单次 widget refresh。

    验证 100ms debounce 工作:连续 5 条 delete 之间 timer 不应到点(假设
    测试在 50ms 内全发完),最终等 100ms 后只看到 1 次 refresh emit。
    """
    from PySide6.QtCore import QEventLoop

    win = _FakeWindow()
    # 5 条连续 delete — 每次都 restart debounce timer
    for i in range(5):
        MainWindow._on_media_deleted(
            win,
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


def test_on_media_deleted_throttles_activity_messages(qapp) -> None:
    """批量 100 条连续 delete → 活动文案不刷成「已删除 媒体 × 100」。

    `_throttle_activity("media_delete", …)` min_interval_ms=500,
    100 条连续 delete(总耗时 < 500ms)只会更新一次 activity label。
    """
    from PySide6.QtCore import QEventLoop

    win = _FakeWindow()
    first_label_text = ""
    for i in range(100):
        MainWindow._on_media_deleted(
            win,
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