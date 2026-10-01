"""状态栏左侧活动指示器测试。

2026-09-25 v1.8.x 新功能:`_activity_label` 放 status bar LEFT(stretch=1),
`_show_activity(text, *, timeout_ms=None)` 是统一入口,各 VM slot
(_on_login_state / _on_sync_progress / _on_export_done / _on_error 等)都调它。
右侧 conn_label / paused_label / bell_btn / objects_warn_label 是持久的
状态指示,本测试只关心活动指示器。

`_throttle_activity(key, text, min_interval_ms)` 节流 helper:MessageReceived
等高频事件用,避免 label 被刷成流水账。

2026-10-01 v1.11.x 状态栏组件化重构:`_show_activity` / `_throttle_activity`
已迁移到 `StatusBar._ActivityLabel`,MainWindow 不再持有 `_activity_label` /
`_activity_throttle` 字段。本测试改为直接构造 `_ActivityLabel` 测纯单元,
不依赖 MainWindow 桩 — 更纯粹的单元测试。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tgmonitor.ui.widgets.status_bar import _ActivityLabel  # noqa: E402


def test_activity_label_initial_empty(qapp) -> None:
    """新 widget → 文本为空。"""
    label = _ActivityLabel()
    assert label.text() == ""


def test_show_activity_sets_text(qapp) -> None:
    """show_text(text) → label 文本更新。"""
    label = _ActivityLabel()
    label.show_text("加载已订阅频道…")
    assert label.text() == "加载已订阅频道…"


def test_show_activity_empty_clears(qapp) -> None:
    """show_text("") → 清空。稳态场景(后台 startup 完成后清空)。"""
    label = _ActivityLabel()
    label.show_text("登录中: code_required")
    assert label.text() != ""
    label.show_text("")
    assert label.text() == ""


def test_show_activity_persistent_without_timeout(qapp) -> None:
    """timeout_ms=None → 持续显示,不会自动清空。"""
    label = _ActivityLabel()
    label.show_text("同步 #1: 50/100 (history)")
    assert label.text() == "同步 #1: 50/100 (history)"
    for _ in range(5):
        qapp.processEvents()
    assert label.text() == "同步 #1: 50/100 (history)"


def test_show_activity_timeout_schedules_timer(qapp) -> None:
    """timeout_ms>0 → 启 _timer(实例 QTimer),到点自动清空。"""
    label = _ActivityLabel()
    label.show_text("导出完成: /tmp/x.html", timeout_ms=4000)
    assert label._timer.isActive() is True
    assert label._timer.interval() == 4000
    assert label.text() == "导出完成: /tmp/x.html"


def test_show_activity_zero_timeout_no_timer(qapp) -> None:
    """timeout_ms=0 → 不启 _timer,持续显示。"""
    label = _ActivityLabel()
    label.show_text("登录中: code_required", timeout_ms=0)
    assert label._timer.isActive() is False
    assert label.text() == "登录中: code_required"


# ---- throttle 节流 ----


def test_throttle_activity_first_call_updates(qapp) -> None:
    """首次调用 → 立即更新 label。"""
    label = _ActivityLabel()
    label.throttle("message_received", "+1 #频道A", min_interval_ms=1500)
    assert label.text() == "+1 #频道A"


def test_throttle_activity_rapid_calls_dropped(qapp) -> None:
    """min_interval_ms 内的连续调用被丢弃,label 保持首次值不变。"""
    label = _ActivityLabel()
    label.throttle("message_received", "+1 #A", min_interval_ms=1500)
    label.throttle("message_received", "+1 #B", min_interval_ms=1500)
    label.throttle("message_received", "+1 #C", min_interval_ms=1500)
    assert label.text() == "+1 #A"


def test_throttle_activity_different_keys_independent(qapp) -> None:
    """不同 key 互不干扰 — 节流按 key 维度。"""
    label = _ActivityLabel()
    label.throttle("message_received", "+1 #A", min_interval_ms=1500)
    label.throttle("media_downloaded", "已下载: a.jpg", min_interval_ms=1500)
    assert label.text() == "已下载: a.jpg"


def test_throttle_activity_expires_after_interval(qapp) -> None:
    """min_interval_ms 后,同 key 的下一次调用可生效。

    把 _throttle 的时间戳覆盖到 N ms 之前,模拟"上次调用是 N ms 之前"。
    """
    import time as _time

    label = _ActivityLabel()
    label._throttle["message_received"] = _time.monotonic() * 1000.0 - 2000
    label.throttle("message_received", "+1 #A", min_interval_ms=1500)
    assert label.text() == "+1 #A"
