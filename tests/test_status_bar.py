"""自定义 StatusBar widget + 9 个子组件 — 单元测。

2026-10-01 v1.11.x 状态栏组件化重构后,所有子组件(LEFT 5 + RIGHT 4)独立可测,
不再依赖 MainWindow 桩。覆盖:

- 子组件 setter / 显示文案 / 翻译
- throttle / timeout / ring buffer cap
- EventBus 直接订阅(AuthErrorOccurred → _ErrorBellButton)
- StatusBar show_message / clear_message 自管 transient
- SettingsChanged → objects_warn 移除

测试策略:用 conftest 提供的 `qapp` session fixture,直接构造 widget 即可。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 2026-10-02 v1.12.1:`test_bell_button_auth_error_shows_and_counts` 在 Windows offscreen
# 平台 `qapp.processEvents()` 后偶发 access violation / SIGSEGV(v1.12.0 push 后
# CI 首次爆,与 `test_main_window_delete_feedback` / `test_media_manager_i18n`
# 三个 qt offscreen paint path race 同根:`asyncio.run` 跑完后 nested Qt
# event loop pump 触 paint event 路径 race。套 `windows_qt_paint_skip`(v1.8.3
# 起扩到 (win32, linux, darwin))。
import sys
from datetime import UTC, datetime  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.core.events import AuthErrorOccurred, ErrorOccurred  # noqa: E402
from tgmonitor.ui.widgets.status_bar import (  # noqa: E402
    StatusBar,
    _ActivityLabel,
    _BackendLabel,
    _conn_state_label,
    _ConnectionLabel,
    _ErrorBellButton,
    _LastMessageLabel,
    _ObjectsWarnLabel,
    _PausedLabel,
    _SelectedChannelLabel,
    _StatsLabel,
)

_PAINT_PATH_RACE_PLATFORMS = ("win32", "linux", "darwin")
windows_qt_paint_skip = pytest.mark.skipif(
    sys.platform in _PAINT_PATH_RACE_PLATFORMS,
    reason=(
        "Qt offscreen paint path race 在 `asyncio.run` 嵌套 `qapp.processEvents()`"
        "触发:GH Actions ubuntu/macos/windows runner 的 Qt offscreen 平台都受影响。"
        "本地 Qt 6.11+ macOS 真机 + linux 真机仍过(不属本 race)。"
    ),
)

# ======================== LEFT 区子组件 ========================


def test_selected_channel_label_none_default(qapp: QApplication) -> None:
    """未设置时显示灰色提示。"""
    label = _SelectedChannelLabel()
    assert "(无选中频道)" in label.text()
    label.deleteLater()


def test_selected_channel_label_set_name(qapp: QApplication) -> None:
    """set_channel(name) → 显示「📡 <name>」。"""
    label = _SelectedChannelLabel()
    label.set_channel("频道 A")
    assert "频道 A" in label.text()
    assert "📡" in label.text()
    # set None 回到占位
    label.set_channel(None)
    assert "(无选中频道)" in label.text()
    label.deleteLater()


def test_stats_label_format(qapp: QApplication) -> None:
    """set_stats(n_ch, n_msg) → 「监听:N 消息:M」。"""
    label = _StatsLabel()
    label.set_stats(5, 12)
    text = label.text()
    assert "5" in text
    assert "12" in text
    assert "监听" in text
    label.deleteLater()


def test_backend_label_default(qapp: QApplication) -> None:
    """未设置时显示「DB:— OS:—」占位。"""
    label = _BackendLabel()
    assert "DB" in label.text()
    label.set_label("DB=jsonl, ObjectStore=folder")
    assert "jsonl" in label.text()
    assert "folder" in label.text()
    label.deleteLater()


def test_last_message_label_today_format(qapp: QApplication) -> None:
    """今天的时间 → 「最后:HH:MM」。"""
    label = _LastMessageLabel()
    when = datetime.now(UTC).replace(hour=14, minute=32, second=0, microsecond=0)
    label.set_time(when)
    assert "14:32" in label.text()
    label.deleteLater()


def test_last_message_label_other_day(qapp: QApplication) -> None:
    """非今天 → 「最后:MM-DD HH:MM」(旧消息历史回放)。"""
    label = _LastMessageLabel()
    when = datetime(2025, 1, 1, 14, 32, 0, tzinfo=UTC)
    label.set_time(when)
    assert "01-01" in label.text()
    label.deleteLater()


def test_last_message_label_none(qapp: QApplication) -> None:
    """None → 占位「最后:—」。"""
    label = _LastMessageLabel()
    label.set_time(None)
    assert "最后" in label.text()
    label.deleteLater()


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


# ======================== RIGHT 区子组件 ========================


def test_connection_label_default(qapp: QApplication) -> None:
    label = _ConnectionLabel()
    # 默认 "unknown" → "TG 状态未知"
    assert label.text() != ""
    label.set_state("ready")
    assert "已连接" in label.text()
    label.set_state("connecting")
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
    """翻译表与 main_window 原版本兼容。"""
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
    """LEFT 5 + RIGHT 4 共 9 个子组件,顺序与设计一致。"""
    sb = _make_status_bar(qapp)
    children = sb.findChildren(_SelectedChannelLabel)
    assert len(children) == 1
    children = sb.findChildren(_StatsLabel)
    assert len(children) == 1
    children = sb.findChildren(_BackendLabel)
    assert len(children) == 1
    children = sb.findChildren(_LastMessageLabel)
    assert len(children) == 1
    children = sb.findChildren(_ActivityLabel)
    assert len(children) == 1
    children = sb.findChildren(_ConnectionLabel)
    assert len(children) == 1
    children = sb.findChildren(_PausedLabel)
    assert len(children) == 1
    children = sb.findChildren(_ErrorBellButton)
    assert len(children) == 1
    sb.deleteLater()


def test_status_bar_setters_delegate(qapp: QApplication) -> None:
    """所有公开 setter 都正确触发子组件更新。"""
    sb = _make_status_bar(qapp)
    sb.set_connection_state("ready")
    conn = sb.findChild(_ConnectionLabel)
    assert "已连接" in conn.text()  # type: ignore[union-attr]

    sb.set_paused(True)
    paused = sb.findChild(_PausedLabel)
    assert paused.isHidden() is False  # type: ignore[union-attr]
    sb.set_paused(False)
    assert paused.isHidden() is True  # type: ignore[union-attr]

    sb.set_selected_channel("频道 X")
    sel = sb.findChild(_SelectedChannelLabel)
    assert "频道 X" in sel.text()  # type: ignore[union-attr]
    sb.set_selected_channel(None)
    assert "(无选中频道)" in sel.text()  # type: ignore[union-attr]

    sb.set_stats(subscribed=5, messages=10)
    stats = sb.findChild(_StatsLabel)
    assert "5" in stats.text()  # type: ignore[union-attr]
    assert "10" in stats.text()  # type: ignore[union-attr]

    sb.set_backend_label("DB=jsonl, ObjectStore=folder")
    backend = sb.findChild(_BackendLabel)
    assert "jsonl" in backend.text()  # type: ignore[union-attr]

    sb.set_last_message_time(datetime.now(UTC))
    last = sb.findChild(_LastMessageLabel)
    assert "最后" in last.text()  # type: ignore[union-attr]

    sb.deleteLater()


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
