"""状态栏左侧活动指示器 `_activity_label` + `_show_activity` helper 测试。

2026-09-25 v1.8.x 新功能:`_activity_label` 放 status bar LEFT(stretch=1),
`_show_activity(text, *, timeout_ms=None)` 是统一入口,各 VM slot
(_on_login_state / _on_sync_progress / _on_export_done / _on_error 等)都调它。
右侧 conn_label / paused_label / bell_btn / objects_warn_label 是持久的
状态指示,本测试只关心活动指示器。

`_throttle_activity(key, text, min_interval_ms)` 节流 helper:MessageReceived
等高频事件用,避免 label 被刷成流水账。

测试策略:仅测 `_show_activity` / `_throttle_activity` 两个 pure helper
(不依赖 status_bar / _conn_label / _bell_btn 等其他 widget)。slot 测试
(如 `_on_login_state` 同时写 status_bar)需要构造完整 MainWindow,与
`test_main_window_statusbar.py` 同模式但更重;留给需要时再加。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QLabel  # noqa: E402

from tgmonitor.ui.main_window import MainWindow  # noqa: E402


class _FakeWindow:
    """最小桩:只提供 `_show_activity` / `_throttle_activity` 需要的字段。

    与 `test_main_window_statusbar.py` 的 _FakeWindow 同模式 — slot 单测
    不构造完整 MainWindow(太重)。
    """

    def __init__(self) -> None:
        self._activity_label = QLabel("")
        self._activity_label.setObjectName("statusActivityLabel")
        self._activity_throttle: dict[str, float] = {}

    # 把 MainWindow 上的 unbound method 绑过来 — slot 不依赖 self 其他字段
    _show_activity = MainWindow._show_activity
    _throttle_activity = MainWindow._throttle_activity


def test_activity_label_initial_empty(qapp) -> None:
    """新窗口 → activity label 文本为空。"""
    win = _FakeWindow()
    assert win._activity_label.text() == ""


def test_show_activity_sets_text(qapp) -> None:
    """_show_activity(text) → label 文本更新。"""
    win = _FakeWindow()
    win._show_activity("加载已订阅频道…")
    assert win._activity_label.text() == "加载已订阅频道…"


def test_show_activity_empty_clears(qapp) -> None:
    """_show_activity("") → 清空。稳态场景(后台 startup 完成后清空)。"""
    win = _FakeWindow()
    win._show_activity("登录中: code_required")
    assert win._activity_label.text() != ""
    win._show_activity("")
    assert win._activity_label.text() == ""


def test_show_activity_persistent_without_timeout(qapp) -> None:
    """timeout_ms=None → 持续显示,不会自动清空。"""
    win = _FakeWindow()
    win._show_activity("同步 #1: 50/100 (history)")
    assert win._activity_label.text() == "同步 #1: 50/100 (history)"
    # 没设 timeout,pump events 也不应清空
    for _ in range(5):
        qapp.processEvents()
    assert win._activity_label.text() == "同步 #1: 50/100 (history)"


def test_show_activity_timeout_schedules_timer(qapp) -> None:
    """timeout_ms>0 → 挂 QTimer.singleShot,且 ms 参数正确。"""
    win = _FakeWindow()
    captured: list[int] = []
    orig_single_shot = QTimer.singleShot

    def fake_single_shot(ms: int, slot) -> None:  # type: ignore[no-untyped-def]
        captured.append(ms)

    QTimer.singleShot = staticmethod(fake_single_shot)  # type: ignore[assignment]
    try:
        win._show_activity("导出完成: /tmp/x.html", timeout_ms=4000)
        assert captured == [4000]
        # label 文本已设
        assert win._activity_label.text() == "导出完成: /tmp/x.html"
    finally:
        QTimer.singleShot = orig_single_shot  # type: ignore[assignment]


def test_show_activity_zero_timeout_no_timer(qapp) -> None:
    """timeout_ms=0 → 不挂 QTimer(等价 None),持续显示。"""
    win = _FakeWindow()
    captured: list[int] = []
    orig_single_shot = QTimer.singleShot

    def fake_single_shot(ms: int, slot) -> None:  # type: ignore[no-untyped-def]
        captured.append(ms)

    QTimer.singleShot = staticmethod(fake_single_shot)  # type: ignore[assignment]
    try:
        win._show_activity("登录中: code_required", timeout_ms=0)
        assert captured == [], f"timeout_ms=0 不应挂 timer,实际捕获 {captured}"
        assert win._activity_label.text() == "登录中: code_required"
    finally:
        QTimer.singleShot = orig_single_shot  # type: ignore[assignment]


# ---- _throttle_activity 节流 ----


def test_throttle_activity_first_call_updates(qapp) -> None:
    """首次调用 → 立即更新 label。"""
    win = _FakeWindow()
    win._throttle_activity("message_received", "+1 #频道A", min_interval_ms=1500)
    assert win._activity_label.text() == "+1 #频道A"


def test_throttle_activity_rapid_calls_dropped(qapp) -> None:
    """min_interval_ms 内的连续调用被丢弃,label 保持首次值不变。"""
    win = _FakeWindow()
    win._throttle_activity("message_received", "+1 #A", min_interval_ms=1500)
    win._throttle_activity("message_received", "+1 #B", min_interval_ms=1500)
    win._throttle_activity("message_received", "+1 #C", min_interval_ms=1500)
    # 三次调用都在节流期内,只有第一次生效
    assert win._activity_label.text() == "+1 #A"


def test_throttle_activity_different_keys_independent(qapp) -> None:
    """不同 key 互不干扰 — 节流按 key 维度。"""
    win = _FakeWindow()
    win._throttle_activity("message_received", "+1 #A", min_interval_ms=1500)
    # 不同 key 不应被第一个 key 的节流阻塞
    win._throttle_activity("media_downloaded", "已下载: a.jpg", min_interval_ms=1500)
    assert win._activity_label.text() == "已下载: a.jpg"


def test_throttle_activity_expires_after_interval(qapp) -> None:
    """min_interval_ms 后,同 key 的下一次调用可生效。

    把 _activity_throttle 的时间戳覆盖到 N ms 之前,模拟"上次调用是 N ms 之前"。
    """
    import time as _time

    win = _FakeWindow()
    # 先设一个 throttle 时间戳到 2000ms 之前
    win._activity_throttle["message_received"] = _time.monotonic() * 1000.0 - 2000
    # 节流已过 → 这次应生效
    win._throttle_activity("message_received", "+1 #A", min_interval_ms=1500)
    assert win._activity_label.text() == "+1 #A"
