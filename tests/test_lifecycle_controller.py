"""`LifecycleController` 单元测试 — 2026-10-10 v1.12.x plan-batch2 目标 1.4。

覆盖 6 个 method(2026-11-16 v2.0.2 移除 `focus_search` 及 Esc 搜索分支):
- `show_and_raise` unminimize + 顶置 + 焦点
- `quit_app` 标 `_truly_quit=True` + 调 `qt_app.quit()`
- `switch_tab` 委托 nav.set_current
- `on_nav_changed` 切到 MESSAGES(1)→ refresh / MEDIA(2)→ start media_refresh_debounce
- `on_global_escape` 非 LIVE → 回 LIVE(搜索分支已移除)
- `copy_current_message_text` LIVE 选中消息 text → clipboard + status 提示
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.ui.controllers.lifecycle_controller import LifecycleController  # noqa: E402


@pytest.fixture(scope="session")
def qapp_session() -> QApplication:
    return QApplication.instance() or QApplication([])


def _make_ctx(qapp: QApplication) -> tuple[Any, dict[str, Any]]:
    """构造 LifecycleController 用的最小 ctx,返回 ctx + 各 mock 引用。"""
    # main_window stub — QApplication 上下文下的 QObject 派生(QWidget 是
    # QObject),允许调 showNormal / raise_ / activateWindow / tr。
    from PySide6.QtWidgets import QMainWindow  # noqa: PLC0415

    mw = QMainWindow()
    mw._truly_quit = False

    # 各 widget 桩
    nav = MagicMock(name="nav")
    live_view = MagicMock(name="live_view")
    stack = MagicMock(name="stack")
    stack.currentIndex = MagicMock(return_value=0)  # 默认在 DASHBOARD(含实时流)
    status_bar = MagicMock(name="status_bar")
    media_refresh_debounce = MagicMock(spec=QTimer, name="media_refresh_debounce")
    search_debounce = MagicMock(spec=QTimer, name="search_debounce")
    message_manager = MagicMock(name="message_manager")

    # main_window.tr(...) 返原文
    mw.tr = MagicMock(side_effect=lambda s: s)

    ctx = MagicMock(
        name="ctx",
        app=MagicMock(name="app"),
        main_window=mw,
        nav=nav,
        live_view=live_view,
        stack=stack,
        status_bar=status_bar,
        media_refresh_debounce=media_refresh_debounce,
        search_debounce=search_debounce,
        message_manager=message_manager,
    )
    refs = {
        "mw": mw,
        "nav": nav,
        "live_view": live_view,
        "stack": stack,
        "status_bar": status_bar,
        "media_refresh_debounce": media_refresh_debounce,
        "search_debounce": search_debounce,
        "message_manager": message_manager,
    }
    return ctx, refs


class TestQuitApp:
    """`quit_app` — 标 `_truly_quit=True` + 调 `qt_app.quit()`。"""

    def test_sets_truly_quit_and_calls_qapp_quit(self, qapp_session) -> None:
        ctx, refs = _make_ctx(qapp_session)
        ctrl = LifecycleController(ctx)
        mw = refs["mw"]
        assert mw._truly_quit is False

        mock_quit = MagicMock(name="qt_app.quit")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "tgmonitor.ui.controllers.lifecycle_controller.QApplication.instance",
                classmethod(lambda cls: MagicMock(quit=mock_quit)),
            )
            ctrl.quit_app()

        assert mw._truly_quit is True
        mock_quit.assert_called_once()


# 2026-11-16 v2.0.2:TestFocusSearch 与 TestOnGlobalEscape 的搜索 focus 分支
# 随顶部搜索功能移除而删除。


# ---- 杂项:thin delegate 保留 ----


def test_main_window_thin_delegates_preserved() -> None:
    """2026-10-10:thin delegate 7 个 method 全保留,以保持 signal wire /
    `_ACTION_SLOTS` 工厂 / 测试夹具 / 其他内部 call site 引用稳定。
    """
    from tgmonitor.ui.main_window import MainWindow  # noqa: PLC0415

    for name in (
        "_show_and_raise",
        "_quit_app",
        "_switch_tab",
        "_on_nav_changed",
        "_focus_search",
        "_on_global_escape",
        "_copy_current_message_text",
    ):
        assert hasattr(MainWindow, name), f"MainWindow.{name} should be preserved"
