"""自定义 StatusBar widget + 7 个子组件 — 单元测。

2026-11-17 v2.0.3 D1/D2:剔除 Backend / LastMessage 两个低价值组件,
登录态与连接态合并为唯一 `_UnifiedStateLabel`(LEFT 3 + RIGHT 4)。
2026-10-01 v1.11.x 状态栏组件化重构后子组件独立可测,不再依赖 MainWindow 桩。覆盖:

- 子组件 setter / 显示文案 / 文案映射
- throttle / timeout / ring buffer cap
- EventBus 直接订阅(AuthErrorOccurred → _ErrorBellButton)
- StatusBar show_message / clear_message 自管 transient
- SettingsChanged → objects_warn 移除

测试策略:用 conftest 提供的 `qapp` session fixture,直接构造 widget 即可。
"""

from __future__ import annotations

import os

# 2026-10-02 v1.12.1:`test_bell_button_auth_error_shows_and_counts` 在 Windows offscreen
# 平台 `qapp.processEvents()` 后偶发 access violation / SIGSEGV(v1.12.0 push 后
# CI 首次爆,与 `test_main_window_delete_feedback` 同根:`asyncio.run` 跑完后 nested Qt
# event loop pump 触 paint event 路径 race。套 `windows_qt_paint_skip`。
# 2026-11-15:去掉强制 `offscreen`,并把 `windows_qt_paint_skip` 从 `sys.platform`
# 三平台无差别跳过改为按 QPA 精确跳过,恢复本地真机覆盖(PR #45 同法)。
import sys

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from tgmonitor.core.events import AuthErrorOccurred, ErrorOccurred  # noqa: E402
from tgmonitor.ui.widgets.status_bar import (  # noqa: E402
    StatusBar,
    _ActivityLabel,
    _conn_state_label,
    _ErrorBellButton,
    _ObjectsWarnLabel,
    _PausedLabel,
    _StatsLabel,
    _UnifiedStateLabel,
)


def _paint_race_skipped() -> bool:
    # `TG_WIN32_PAINT_PROBE=1`(CI windows-paint-probe job 置位)强制不跳过,在 Windows 上
    # 实跑这些测试,验证 6.12 + qloop 治理后 race 是否消失。
    if os.environ.get("TG_WIN32_PAINT_PROBE"):
        return False
    return os.environ.get("QT_QPA_PLATFORM") == "offscreen" or sys.platform == "win32"


windows_qt_paint_skip = pytest.mark.skipif(
    _paint_race_skipped(),
    reason=(
        "Qt offscreen paint path race 在 `asyncio.run` 嵌套 `qapp.processEvents()`"
        "触发:CI 的 Qt offscreen 平台(QT_QPA_PLATFORM=offscreen) + Windows `windows`"
        "QPA 仍偶发。本地 macOS/Linux 真机(cocoa/xcb)不属本 race,仍跑。"
    ),
)

# ======================== LEFT 区子组件 ========================


def test_stats_label_format(qapp: QApplication) -> None:
    """set_stats(n_ch, n_msg) → 「监听:N 消息:M」。"""
    label = _StatsLabel()
    label.set_stats(5, 12)
    text = label.text()
    assert "5" in text
    assert "12" in text
    assert "监听" in text
    label.deleteLater()


# 2026-11-17 v2.0.3 D1:原 `test_backend_label_default` /
# `test_last_message_label_{today_format,other_day,none}` 四条断言随
# `_BackendLabel` / `_LastMessageLabel` 组件移除而删除(PR 描述已逐条列理由)。
# 新的单一状态位契约见 `test_ui_simplification_2003.py`。


# ---- _ActivityLabel ----


def test_activity_label_set_text(qapp: QApplication) -> None:
    label = _ActivityLabel()
    label.show_text("加载已订阅频道…")
    assert label.text() == "加载已订阅频道…"
    label.deleteLater()


def test_activity_label_empty_clears(qapp: QApplication) -> None:
    label = _ActivityLabel()
    label.show_text("登录中: code_required")
    assert label.text() != ""
    label.show_text("")
    assert label.text() == ""
    label.deleteLater()


@windows_qt_paint_skip
def test_activity_label_timeout_clears(qapp: QApplication) -> None:
    """timeout_ms > 0 → 到点自动清空。

    直接调 `_timer.timeout` 模拟点触发,而不是依赖真实时间等待(offscreen 平台
    QTimer 偶发不 fire;跑路径敏感 code 应绕开)。
    """
    label = _ActivityLabel()
    label.show_text("导出完成: /tmp/x.html", timeout_ms=10)
    # 模拟 timer 到点(emit timeout signal,清空 text)
    label._timer.timeout.emit()
    qapp.processEvents()
    assert label.text() == ""
    label.deleteLater()


def test_activity_label_timeout_zero_persistent(qapp: QApplication) -> None:
    """timeout_ms=0 → 不挂 timer,持续显示。

    直接断言 `_timer.isActive() is False` + `text()`;不再依赖 `_time.sleep`,
    因为 offscreen 平台 timer 抖动 + 真机/无头环境 QTimer.isActive 可能 race。
    """
    label = _ActivityLabel()
    label.show_text("持续活动", timeout_ms=0)
    assert label._timer.isActive() is False
    assert label.text() == "持续活动"
    label.deleteLater()


def test_activity_label_throttle_first_call_updates(qapp: QApplication) -> None:
    label = _ActivityLabel()
    label.throttle("message_received", "+1 #A", min_interval_ms=1500)
    assert label.text() == "+1 #A"
    label.deleteLater()


def test_activity_label_throttle_rapid_calls_dropped(qapp: QApplication) -> None:
    """min_interval_ms 内的连续调用被丢弃,只首次生效。"""
    label = _ActivityLabel()
    label.throttle("message_received", "+1 #A", min_interval_ms=1500)
    label.throttle("message_received", "+1 #B", min_interval_ms=1500)
    label.throttle("message_received", "+1 #C", min_interval_ms=1500)
    assert label.text() == "+1 #A"
    label.deleteLater()


def test_activity_label_throttle_different_keys_independent(qapp: QApplication) -> None:
    label = _ActivityLabel()
    label.throttle("message_received", "+1 #A", min_interval_ms=1500)
    label.throttle("media_downloaded", "已下载: a.jpg", min_interval_ms=1500)
    assert label.text() == "已下载: a.jpg"
    label.deleteLater()


def test_activity_label_persistent_without_timeout(qapp: QApplication) -> None:
    """timeout_ms=None(默认)→ 持续显示,多次 processEvents 后不被清空。

    从 test_main_window_activity_label.py 迁入(v2.0.9 删重复文件前保留独有断言)。
    """
    label = _ActivityLabel()
    label.show_text("同步 #1: 50/100 (history)")
    for _ in range(5):
        qapp.processEvents()
    assert label.text() == "同步 #1: 50/100 (history)"
    label.deleteLater()


def test_activity_label_throttle_expires_after_interval(qapp: QApplication) -> None:
    """min_interval_ms 已过 → 同 key 的下一次调用重新生效。

    从 test_main_window_activity_label.py 迁入(v2.0.9 删重复文件前保留独有断言)。
    把时间戳覆盖到 N ms 之前,模拟"上次调用是很久以前",不依赖真实等待。
    """
    import time as _time

    label = _ActivityLabel()
    label._throttle["message_received"] = _time.monotonic() * 1000.0 - 2000
    label.throttle("message_received", "+1 #A", min_interval_ms=1500)
    assert label.text() == "+1 #A"
    label.deleteLater()


# ======================== RIGHT 区子组件 ========================


def test_unified_state_label_priority(qapp: QApplication) -> None:
    """D2:登录态优先;仅 ready 时呈现 5 态连接态。"""
    label = _UnifiedStateLabel()
    # 初始 login_state=uninit → 显示登录态,不是连接态
    assert "启动中" in label.text()
    label.set_connection_state("ready")
    assert "启动中" in label.text()
    assert "已连接" not in label.text()
    label.set_login_state("ready")
    assert "已连接" in label.text()
    label.set_connection_state("connecting")
    assert "连接中" in label.text()
    label.deleteLater()


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("waiting_for_network", "TG 等待网络"),
        ("connecting", "TG 连接中…"),
        ("updating", "TG 同步中…"),
        ("ready", "TG 已连接"),
        ("unknown", "TG 状态未知"),
        ("some_new_state", "TG some_new_state"),
    ],
)
def test_conn_state_label_mapping(qapp: QApplication, state: str, expected: str) -> None:
    """状态文案映射与 main_window 原版本兼容。"""
    label = _conn_state_label(state)
    assert label == expected


def test_paused_label_default_hidden(qapp: QApplication) -> None:
    label = _PausedLabel()
    assert label.isHidden() is True
    label.set_visible(True)
    assert label.isHidden() is False
    label.set_visible(False)
    assert label.isHidden() is True
    label.deleteLater()


def test_objects_warn_label_styled(qapp: QApplication) -> None:
    """对象存储不可用 label 红字 + tooltip 存在。"""
    label = _ObjectsWarnLabel("S3 connect refused")
    assert "S3" in label.text() or "对象存储" in label.text()
    assert "对象存储" in label.text()
    assert label.toolTip() != ""
    label.deleteLater()


# ======================== _ErrorBellButton ========================


class _StubApp:
    """最小 AppService 桩 — 只暴露 bus.subscribe(publish 不需要)。"""

    def __init__(self) -> None:
        from tgmonitor.core.events import EventBus

        self.bus = EventBus()


def _make_bell(qapp: QApplication) -> _ErrorBellButton:
    """构造 _ErrorBellButton + 注入 stub app(避免依赖完整 AppService)。"""
    return _ErrorBellButton(_StubApp())


@pytest.fixture(autouse=True)
def _patch_qmessagebox(monkeypatch: pytest.MonkeyPatch) -> None:
    """autouse:QMessageBox.warning 走 mock,offscreen 平台不阻塞。"""
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: QMessageBox.Ok)


def test_bell_button_default_hidden(qapp: QApplication) -> None:
    bell = _make_bell(qapp)
    assert bell.isHidden() is True
    assert bell.get_log() == []
    bell.deleteLater()


@windows_qt_paint_skip
def test_bell_button_auth_error_shows_and_counts(qapp: QApplication) -> None:
    """AuthErrorOccurred → 铃铛显示 + 计数 + ring buffer 写入。"""
    bell = _make_bell(qapp)

    async def _go() -> None:
        await bell._on_bus_auth_error(AuthErrorOccurred(source="code", message="bad"))

    import asyncio

    asyncio.run(_go())
    qapp.processEvents()
    assert bell.isHidden() is False
    assert "1" in bell.text()
    assert len(bell.get_log()) == 1
    when, source, msg = bell.get_log()[0]
    assert source == "code"
    assert msg == "bad"
    assert when.tzinfo is not None
    bell.deleteLater()


@windows_qt_paint_skip
def test_bell_button_ring_buffer_cap_100(qapp: QApplication) -> None:
    """超过 100 条 → 只留最近 100。"""
    bell = _make_bell(qapp)

    async def _go() -> None:
        for i in range(105):
            await bell._on_bus_auth_error(AuthErrorOccurred(source="code", message=f"err {i}"))

    import asyncio

    asyncio.run(_go())
    qapp.processEvents()
    assert len(bell.get_log()) == 100
    assert "err 5" in bell.get_log()[0][2]
    assert "err 104" in bell.get_log()[-1][2]
    bell.deleteLater()


@windows_qt_paint_skip
def test_bell_button_non_auth_ignored(qapp: QApplication) -> None:
    """非 AuthErrorOccurred → 不动铃铛。"""
    bell = _make_bell(qapp)

    async def _go() -> None:
        await bell._on_bus_auth_error(ErrorOccurred(source="general", message="x"))

    import asyncio

    asyncio.run(_go())
    qapp.processEvents()
    assert bell.isHidden() is True
    assert bell.get_log() == []
    bell.deleteLater()


@windows_qt_paint_skip
def test_bell_button_clear_resets(qapp: QApplication) -> None:
    bell = _make_bell(qapp)

    async def _go() -> None:
        await bell._on_bus_auth_error(AuthErrorOccurred(source="code", message="x"))

    import asyncio

    asyncio.run(_go())
    qapp.processEvents()
    assert bell.isHidden() is False
    bell.clear()
    assert bell.isHidden() is True
    assert bell.get_log() == []
    bell.deleteLater()


# ======================== StatusBar 主组件 ========================


def _make_status_bar(qapp: QApplication) -> StatusBar:
    """构造 StatusBar — 用 stub app 替代真 AppService。"""
    return StatusBar(_StubApp(), None)


def test_status_bar_construction_no_objects_error(qapp: QApplication) -> None:
    """无 objects_error → 不挂 _ObjectsWarnLabel。"""
    sb = _make_status_bar(qapp)
    assert sb.findChild(_ObjectsWarnLabel) is None
    sb.deleteLater()


def test_status_bar_construction_with_objects_error(qapp: QApplication) -> None:
    """有 objects_error → 挂 _ObjectsWarnLabel。"""
    sb = StatusBar(_StubApp(), None, objects_error="S3 connect refused")
    warn = sb.findChild(_ObjectsWarnLabel)
    assert warn is not None
    assert "对象存储" in warn.text()
    sb.deleteLater()


def test_status_bar_layout_left_right_order(qapp: QApplication) -> None:
    """D1:LEFT 2 + RIGHT 4 共 6 个子组件,顺序与设计一致。"""
    sb = _make_status_bar(qapp)
    children = sb.findChildren(_StatsLabel)
    assert len(children) == 1
    children = sb.findChildren(_ActivityLabel)
    assert len(children) == 1
    children = sb.findChildren(_UnifiedStateLabel)
    assert len(children) == 1
    children = sb.findChildren(_PausedLabel)
    assert len(children) == 1
    children = sb.findChildren(_ErrorBellButton)
    assert len(children) == 1
    sb.deleteLater()


def test_status_bar_setters_delegate(qapp: QApplication) -> None:
    """所有公开 setter 都正确触发子组件更新。"""
    sb = _make_status_bar(qapp)
    sb.set_login_state("ready")
    sb.set_connection_state("ready")
    conn = sb.findChild(_UnifiedStateLabel)
    assert "已连接" in conn.text()  # type: ignore[union-attr]

    sb.set_paused(True)
    paused = sb.findChild(_PausedLabel)
    assert paused.isHidden() is False  # type: ignore[union-attr]
    sb.set_paused(False)
    assert paused.isHidden() is True  # type: ignore[union-attr]

    sb.set_stats(subscribed=5, messages=10)
    stats = sb.findChild(_StatsLabel)
    assert "5" in stats.text()  # type: ignore[union-attr]
    assert "10" in stats.text()  # type: ignore[union-attr]

    sb.deleteLater()


@windows_qt_paint_skip
def test_status_bar_show_message_and_clear(qapp: QApplication) -> None:
    """show_message / clear_message 自管 transient label。"""
    sb = _make_status_bar(qapp)
    sb.show_message("拉取频道列表…", 2000)
    qapp.processEvents()
    transient = sb._transient
    assert transient.text() == "拉取频道列表…"
    sb.clear_message()
    assert transient.text() == ""
    sb.deleteLater()


@windows_qt_paint_skip
def test_status_bar_on_settings_changed_removes_objects_warn(
    qapp: QApplication,
) -> None:
    """on_settings_changed → 移除 _ObjectsWarnLabel。"""
    sb = StatusBar(_StubApp(), None, objects_error="S3 connect refused")
    assert sb.findChild(_ObjectsWarnLabel) is not None
    sb.on_settings_changed()
    qapp.processEvents()
    assert sb.findChild(_ObjectsWarnLabel) is None
    # 第二次调用(已无 warn)幂等
    sb.on_settings_changed()
    qapp.processEvents()
    assert sb.findChild(_ObjectsWarnLabel) is None
    sb.deleteLater()


@windows_qt_paint_skip
def test_status_bar_clear_error_log_via_bell(qapp: QApplication) -> None:
    """clear_error_log 委托 _ErrorBellButton.clear。"""
    sb = _make_status_bar(qapp)
    bell = sb.findChild(_ErrorBellButton)

    async def _go() -> None:
        await bell._on_bus_auth_error(  # type: ignore[union-attr]
            AuthErrorOccurred(source="code", message="x")
        )

    import asyncio

    asyncio.run(_go())
    qapp.processEvents()
    assert bell.isHidden() is False  # type: ignore[union-attr]
    sb.clear_error_log()
    assert bell.isHidden() is True  # type: ignore[union-attr]
    assert sb.get_error_log() == []
    sb.deleteLater()


def test_no_selected_channel_placeholder(qapp: QApplication) -> None:
    """状态栏不再常驻「(无选中频道)」死占位(全仓无调用方,整条链路已删)。"""
    sb = _make_status_bar(qapp)
    assert not hasattr(sb, "set_selected_channel")
    assert not any("无选中频道" in lbl.text() for lbl in sb.findChildren(QLabel))
    sb.deleteLater()
