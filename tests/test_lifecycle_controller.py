"""`LifecycleController` 单元测试 — 2026-10-10 v1.12.x plan-batch2 目标 1.4。

覆盖 7 个 method:
- `show_and_raise` unminimize + 顶置 + 焦点
- `quit_app` 标 `_truly_quit=True` + 调 `qt_app.quit()`
- `switch_tab` 委托 nav.set_current
- `on_nav_changed` 切到 MEDIA 页(3)→ start media_refresh_debounce
- `focus_search` 切 LIVE + focus + selectAll
- `on_global_escape` 搜索 focus → 清空 + 失焦;非 LIVE → 回 LIVE
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
    header = MagicMock(name="header")
    search_edit = MagicMock(name="search_edit")
    search_edit.hasFocus = MagicMock(return_value=False)
    search_bar = MagicMock(name="search_bar")
    search_bar.edit = search_edit
    header.search_bar = search_bar

    nav = MagicMock(name="nav")
    live_view = MagicMock(name="live_view")
    stack = MagicMock(name="stack")
    stack.currentIndex = MagicMock(return_value=0)  # 默认在 LIVE 页
    status_bar = MagicMock(name="status_bar")
    media_refresh_debounce = MagicMock(spec=QTimer, name="media_refresh_debounce")
    search_debounce = MagicMock(spec=QTimer, name="search_debounce")

    # main_window.tr(...) 返原文
    mw.tr = MagicMock(side_effect=lambda s: s)

    ctx = MagicMock(
        name="ctx",
        app=MagicMock(name="app"),
        main_window=mw,
        header=header,
        nav=nav,
        live_view=live_view,
        stack=stack,
        status_bar=status_bar,
        media_refresh_debounce=media_refresh_debounce,
        search_debounce=search_debounce,
    )
    refs = {
        "mw": mw,
        "header": header,
        "search_edit": search_edit,
        "search_bar": search_bar,
        "nav": nav,
        "live_view": live_view,
        "stack": stack,
        "status_bar": status_bar,
        "media_refresh_debounce": media_refresh_debounce,
        "search_debounce": search_debounce,
    }
    return ctx, refs


class TestShowAndRaise:
    """`show_and_raise` — unminimize + 顶置 + 焦点。"""

    def test_calls_qwidget_methods(self, qapp_session) -> None:
        ctx, refs = _make_ctx(qapp_session)
        ctrl = LifecycleController(ctx)
        mw = refs["mw"]
        # 给三个方法加 spy
        mw.showNormal = MagicMock(name="showNormal")
        mw.raise_ = MagicMock(name="raise_")
        mw.activateWindow = MagicMock(name="activateWindow")

        ctrl.show_and_raise()

        mw.showNormal.assert_called_once()
        mw.raise_.assert_called_once()
        mw.activateWindow.assert_called_once()


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


class TestSwitchTab:
    """`switch_tab` — 委托 nav.set_current。"""

    def test_calls_nav_set_current(self, qapp_session) -> None:
        ctx, refs = _make_ctx(qapp_session)
        ctrl = LifecycleController(ctx)

        ctrl.switch_tab(3)

        refs["nav"].set_current.assert_called_once_with(3)


class TestOnNavChanged:
    """`on_nav_changed` — 切到 MEDIA 页(3)→ start media_refresh_debounce。"""

    def test_media_tab_starts_debounce(self, qapp_session) -> None:
        ctx, refs = _make_ctx(qapp_session)
        ctrl = LifecycleController(ctx)

        ctrl.on_nav_changed(3)

        refs["media_refresh_debounce"].start.assert_called_once()

    def test_non_media_tab_does_not_start_debounce(self, qapp_session) -> None:
        ctx, refs = _make_ctx(qapp_session)
        ctrl = LifecycleController(ctx)

        ctrl.on_nav_changed(0)  # LIVE
        ctrl.on_nav_changed(1)  # DASHBOARD
        ctrl.on_nav_changed(2)  # CHANNELS
        ctrl.on_nav_changed(4)  # SETTINGS

        refs["media_refresh_debounce"].start.assert_not_called()


class TestFocusSearch:
    """`focus_search` — 切 LIVE + focus + selectAll。"""

    def test_switches_to_live_and_focuses_edit(self, qapp_session) -> None:
        ctx, refs = _make_ctx(qapp_session)
        ctrl = LifecycleController(ctx)

        ctrl.focus_search()

        # switch_tab(0) — LIVE
        refs["nav"].set_current.assert_called_once_with(0)
        refs["search_edit"].setFocus.assert_called_once()
        refs["search_edit"].selectAll.assert_called_once()


class TestOnGlobalEscape:
    """`on_global_escape` — 搜索 focus / 非 LIVE 双分支。"""

    def test_search_focused_clears_and_returns(self, qapp_session) -> None:
        ctx, refs = _make_ctx(qapp_session)
        refs["search_edit"].hasFocus = MagicMock(return_value=True)
        refs["live_view"].set_messages = MagicMock()
        ctrl = LifecycleController(ctx)

        ctrl.on_global_escape()

        # stop debounce + clear search bar + clearFocus + set_messages([])
        refs["search_debounce"].stop.assert_called_once()
        refs["search_bar"].clear.assert_called_once()
        refs["search_edit"].clearFocus.assert_called_once()
        refs["live_view"].set_messages.assert_called_once_with([])

    def test_search_not_focused_and_already_live_is_noop(self, qapp_session) -> None:
        ctx, refs = _make_ctx(qapp_session)
        refs["search_edit"].hasFocus = MagicMock(return_value=False)
        refs["stack"].currentIndex = MagicMock(return_value=0)  # LIVE
        ctrl = LifecycleController(ctx)

        ctrl.on_global_escape()

        # 没切 tab,没清搜索
        refs["nav"].set_current.assert_not_called()
        refs["search_bar"].clear.assert_not_called()
        refs["live_view"].set_messages.assert_not_called()

    def test_search_not_focused_and_not_live_switches_to_live(self, qapp_session) -> None:
        ctx, refs = _make_ctx(qapp_session)
        refs["search_edit"].hasFocus = MagicMock(return_value=False)
        refs["stack"].currentIndex = MagicMock(return_value=2)  # CHANNELS
        ctrl = LifecycleController(ctx)

        ctrl.on_global_escape()

        # 切回 LIVE (idx 0)
        refs["nav"].set_current.assert_called_once_with(0)


class TestCopyCurrentMessageText:
    """`copy_current_message_text` — LIVE 选中消息 text → clipboard + 状态提示。"""

    def test_not_live_page_is_noop(self, qapp_session) -> None:
        ctx, refs = _make_ctx(qapp_session)
        refs["stack"].currentIndex = MagicMock(return_value=1)  # DASHBOARD
        refs["live_view"].current_message = MagicMock()
        ctrl = LifecycleController(ctx)

        ctrl.copy_current_message_text()

        refs["live_view"].current_message.assert_not_called()

    def test_no_selection_is_noop(self, qapp_session) -> None:
        ctx, refs = _make_ctx(qapp_session)
        refs["live_view"].current_message = MagicMock(return_value=None)
        ctrl = LifecycleController(ctx)

        ctrl.copy_current_message_text()

        # 不复制,不弹提示
        refs["status_bar"].show_message.assert_not_called()

    def test_empty_text_shows_no_text_message(self, qapp_session) -> None:
        from dataclasses import dataclass

        @dataclass
        class _Msg:
            text: str = ""

        ctx, refs = _make_ctx(qapp_session)
        refs["live_view"].current_message = MagicMock(return_value=_Msg(text=""))
        ctrl = LifecycleController(ctx)

        ctrl.copy_current_message_text()

        # 弹「当前消息无文本」提示
        refs["status_bar"].show_message.assert_called_once()
        assert "当前消息无文本" in refs["status_bar"].show_message.call_args.args[0]

    def test_valid_text_copies_and_shows_count(self, qapp_session) -> None:
        from dataclasses import dataclass

        @dataclass
        class _Msg:
            text: str = "hello world"

        ctx, refs = _make_ctx(qapp_session)
        refs["live_view"].current_message = MagicMock(return_value=_Msg(text="hello world"))
        # QApplication.clipboard() stub
        from PySide6.QtWidgets import QApplication as _QApp  # noqa: PLC0415

        mock_clipboard = MagicMock()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                f"{_QApp.__module__}.{_QApp.__name__}.clipboard",
                staticmethod(lambda: mock_clipboard),
            )
            ctrl = LifecycleController(ctx)
            ctrl.copy_current_message_text()

        # clipboard.setText 被调,text 长度正确
        mock_clipboard.setText.assert_called_once_with("hello world")
        # 弹「已复制 N 字」提示
        refs["status_bar"].show_message.assert_called_once()
        assert "已复制 11 字" in refs["status_bar"].show_message.call_args.args[0]


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
