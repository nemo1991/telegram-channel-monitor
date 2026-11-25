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
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.ui.widgets.status_bar import (  # noqa: E402
    StatusBar,
    _ActivityLabel,
    _ErrorBellButton,
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


# 2026-11-17 v2.0.3 D1:原 `test_backend_label_default` /
# `test_last_message_label_{today_format,other_day,none}` 四条断言随
# `_BackendLabel` / `_LastMessageLabel` 组件移除而删除(PR 描述已逐条列理由)。
# 新的单一状态位契约见 `test_ui_simplification_2003.py`。


# ---- _ActivityLabel ----


def test_activity_label_throttle_rapid_calls_dropped(qapp: QApplication) -> None:
    """min_interval_ms 内的连续调用被丢弃,只首次生效。"""
    label = _ActivityLabel()
    label.throttle("message_received", "+1 #A", min_interval_ms=1500)
    label.throttle("message_received", "+1 #B", min_interval_ms=1500)
    label.throttle("message_received", "+1 #C", min_interval_ms=1500)
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


# ======================== StatusBar 主组件 ========================


def _make_status_bar(qapp: QApplication) -> StatusBar:
    """构造 StatusBar — 用 stub app 替代真 AppService。"""
    return StatusBar(_StubApp(), None)


def test_transient_message_aligned_with_stats(qapp, qloop, tmp_path) -> None:
    """登录提示 transient 与 stats 同一基线、不重叠(回归 ready 偏下)。"""
    from tests.fixtures._real_window import build_real_main_window

    win = build_real_main_window(qloop, tmp_path)
    sb = win.status_bar
    sb.set_stats(subscribed=12, messages=345)
    sb.show_message("登录状态: ready", 4000)
    sb.show_activity("已同步 3 个频道")
    qapp.processEvents()

    stats = sb.findChild(_StatsLabel)
    transient = sb._transient
    assert stats is not None
    assert not transient.isHidden()
    # 原缺陷:transient 没进布局,默认几何 (0,0,100,30) → 文字中心低约 3px + 与 stats 重叠
    assert transient.geometry().y() == stats.geometry().y()
    assert transient.geometry().height() == stats.geometry().height()
    assert transient.geometry().left() >= stats.geometry().right(), "transient 不应覆盖 stats 文字"
    win.close()
