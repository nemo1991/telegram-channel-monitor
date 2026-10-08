"""PR #3 批量操作二次确认测试 — 2026-09-13 v1.7.5 (P0-C)。

覆盖场景:
- `_on_live_pin` 答 No → 不调 `_run_live_pin`(钉选是写服务端,不可撤销)
- `_on_live_pin` 答 Yes → 调 `_run_live_pin(items)`
- `_on_live_react` 答 No → 不调 `_run_live_react`
- `_on_live_react` 答 Yes → 调 `_run_live_react(items, emoji, is_big)`
- `clear_selection` 仅在答 Yes 后调
- i18n 文案覆盖(zh_CN / en_US 都非空)

2026-10-04 plan 阶段 3.3:`_on_live_pin` / `_on_live_react` 抽到
`LiveSelectionController`,测试改直接构造 controller 实例 + mock ctx(同
test_search_controller.py 模板)。
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402

from tgmonitor.i18n import install_translator  # noqa: E402
from tgmonitor.ui.controllers._base import MainWindowCtx  # noqa: E402
from tgmonitor.ui.controllers.live_selection_controller import LiveSelectionController  # noqa: E402

# ---- fixtures ----


class _FakeState:
    """最小桩:state + 桩 method,模拟 `ctx.main_window` + `ctx.live_view`。

    `run_coro` 短路后,`ctx.app.pin_messages` / `ctx.app.add_reaction` 是 AsyncMock,
    测试用 `assert_called_once_with(items)` 验证 controller 调用的参数。
    """

    def __init__(self) -> None:
        self._clear_selection_count = 0
        self._user_answers_yes = True

    # QObject.tr() 桩 — 直接返源串(lupdate/lrelease 抽的 key 已含 placeholder)
    def tr(self, source: str) -> str:  # noqa: ANN001, ANN201
        return source

    # 桩 QMessageBox.warning(简单用 flag 控制答案)
    def warning(self, *args, **kwargs):  # noqa: ANN001, ANN201
        from PySide6.QtWidgets import QMessageBox

        return QMessageBox.Yes if self._user_answers_yes else QMessageBox.No

    # 桩 live_view.clear_selection(由 controller 调 self._ctx.live_view.clear_selection())
    def clear_selection(self) -> None:
        self._clear_selection_count += 1

    # 桩 emoji picker(react 不弹真实 dialog)
    def _emoji_pick_returns(self) -> tuple[str, bool] | None:
        return ("👍", False)


def _build_ctrl(state: _FakeState) -> tuple[LiveSelectionController, MainWindowCtx]:
    """构造 `LiveSelectionController` + mock ctx,`state` 同时充当
    `ctx.main_window` 和 `ctx.live_view` — controller 只调 .warning() /
    .clear_selection() / ._run_live_*,其余方法不依赖。

    返回 `(ctrl, ctx)`:测试用 `ctx.app.pin_messages` 等 AsyncMock
    `assert_called_once_with(items)`。
    """
    app_mock = MagicMock(name="app")
    # 让 pin_messages / add_reaction 返回真 coroutine — AsyncMock 走
    # `await ctx.app.X(...)` 不会 TypeError;MagicMock 返回值不是 coroutine,
    # run_coro / asyncio.run_coroutine_threadsafe 会拒收。
    app_mock.pin_messages = AsyncMock(name="app.pin_messages", return_value=None)
    app_mock.add_reaction = AsyncMock(name="app.add_reaction", return_value=None)
    ctx = MainWindowCtx(
        app=app_mock,
        monitor=MagicMock(name="monitor"),
        loop=MagicMock(name="loop"),
        vm=MagicMock(name="vm"),
        env_path=MagicMock(name="env_path"),
        main_window=state,
        live_view=state,
        message_detail=MagicMock(name="message_detail"),
        media_manager=MagicMock(name="media_manager"),
        selection_toolbar=MagicMock(name="selection_toolbar"),
        dashboard=MagicMock(name="dashboard"),
        channel_panel=MagicMock(name="channel_panel"),
        header=MagicMock(name="header"),
        status_bar=MagicMock(name="status_bar"),
        nav=MagicMock(name="nav"),
        stack=MagicMock(name="stack"),
        tray=None,
        search_debounce=MagicMock(name="search_debounce"),
        media_refresh_debounce=MagicMock(name="media_refresh_debounce"),
    )
    return LiveSelectionController(ctx), ctx


def _patch_pickers_and_dialog(monkeypatch: pytest.MonkeyPatch, win: _FakeState) -> None:
    """绕过真实 QMessageBox / EmojiPickerDialog / BatchProgressDialog / run_coro。

    这些测试只验证「答 Yes/No 后是否走 _run_live_*」,不验证真正的 pin/react
    副作用,所以把 _run_live_* 的副作用都短路掉。
    """

    # QMessageBox.warning → win.warning(由 _user_answers_yes 控)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", win.warning)
    # EmojiPickerDialog.get_emoji → win._emoji_pick_returns()
    monkeypatch.setattr(
        "tgmonitor.ui.widgets.emoji_picker_dialog.EmojiPickerDialog.get_emoji",
        lambda parent: win._emoji_pick_returns(),
    )
    # BatchProgressDialog 不弹真实 UI(controller 的 _show_batch_progress_dialog
    # 走它;_FakeState 不是真实 QWidget,BatchProgressDialog.__init__ 需 parent)
    monkeypatch.setattr(
        "tgmonitor.ui.widgets.batch_progress_dialog.BatchProgressDialog",
        lambda *args, **kwargs: MagicMock(name="BatchProgressDialog"),
    )

    # run_coro 不调 asyncio.run_coroutine_threadsafe — 直接关掉副作用,
    # controller 调 ctx.app.pin_messages(items)(AsyncMock,真 coroutine)→ 进
    # run_coro 然后到此 no-op;coro.close() 避免「never awaited」warning。
    def _noop_run_coro(loop, coro, **kwargs):  # noqa: ANN001
        import inspect

        if inspect.iscoroutine(coro):
            coro.close()
        return None

    monkeypatch.setattr("tgmonitor.ui.async_bridge.run_coro", _noop_run_coro)


def test_pin_yes_calls_run_live_pin(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """点 toolbar「📌 钉选」→ QMessageBox 答 Yes → 调 _run_live_pin。"""
    state = _FakeState()
    state._user_answers_yes = True
    _patch_pickers_and_dialog(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    items = [(1, 100), (2, 200), (3, 300)]
    ctrl._on_live_pin(items)

    ctx.app.pin_messages.assert_called_once_with(items)
    assert state._clear_selection_count == 1, "Yes 之后应 clear_selection"


def test_pin_no_does_not_call_run_live_pin(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """QMessageBox 答 No → 不调 _run_live_pin,也不 clear_selection。"""
    state = _FakeState()
    state._user_answers_yes = False
    _patch_pickers_and_dialog(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    items = [(1, 100), (2, 200)]
    ctrl._on_live_pin(items)

    ctx.app.pin_messages.assert_not_called()
    assert state._clear_selection_count == 0, "No 不 clear_selection"


def test_pin_empty_items_no_dialog(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """空选区 → 不弹 dialog,也不调 _run_live_pin。"""
    state = _FakeState()
    state._user_answers_yes = True
    _patch_pickers_and_dialog(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    ctrl._on_live_pin([])
    ctx.app.pin_messages.assert_not_called()
    # clear_selection 也不调(空选区清无意义)


def test_react_yes_calls_run_live_react(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """emoji 选完后答 Yes → 调 _run_live_react(items, emoji, is_big)。"""
    state = _FakeState()
    state._user_answers_yes = True
    _patch_pickers_and_dialog(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    items = [(10, 1000), (20, 2000)]
    ctrl._on_live_react(items)

    ctx.app.add_reaction.assert_called_once_with(items, "👍", is_big=False)
    assert state._clear_selection_count == 1


def test_react_no_does_not_call_run_live_react(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """emoji 选完后答 No → 不调 _run_live_react。"""
    state = _FakeState()
    state._user_answers_yes = False
    _patch_pickers_and_dialog(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    items = [(10, 1000)]
    ctrl._on_live_react(items)

    ctx.app.add_reaction.assert_not_called()
    assert state._clear_selection_count == 0


def test_react_picker_cancel_no_dialog(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """emoji picker 选 None(取消)→ 不弹 QMessageBox,不调 _run_live_react。"""
    state = _FakeState()

    def _pick_none(parent):  # noqa: ANN001
        return None

    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", state.warning)
    monkeypatch.setattr(
        "tgmonitor.ui.widgets.emoji_picker_dialog.EmojiPickerDialog.get_emoji",
        _pick_none,
    )
    ctrl, ctx = _build_ctrl(state)

    ctrl._on_live_react([(1, 100)])
    ctx.app.add_reaction.assert_not_called()
    assert state._clear_selection_count == 0


def test_react_empty_items_no_dialog(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """空选区 → 不弹 emoji picker。"""
    state = _FakeState()
    state._user_answers_yes = True

    picker_called = {"n": 0}

    def _pick_called(parent):  # noqa: ANN001
        picker_called["n"] += 1
        return ("👍", False)

    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", state.warning)
    monkeypatch.setattr(
        "tgmonitor.ui.widgets.emoji_picker_dialog.EmojiPickerDialog.get_emoji",
        _pick_called,
    )
    ctrl, ctx = _build_ctrl(state)

    ctrl._on_live_react([])
    ctx.app.add_reaction.assert_not_called()
    assert picker_called["n"] == 0, "空选区不应弹 emoji picker"


# ---- i18n 覆盖 ----


def test_pin_confirm_translations_both_locales(qapp) -> None:
    """2026-09-13 v1.7.5 PR #3:`钉选确认` 文案 zh_CN / en_US 双语覆盖。"""
    # zh_CN
    install_translator(qapp, locale="zh_CN")
    assert QCoreApplication.translate("MainWindow", "钉选确认") == "钉选确认"
    assert (
        QCoreApplication.translate(
            "MainWindow",
            "确定钉选选中的 %d 条消息?\n操作不可撤销。",
        )
        == "确定钉选选中的 %d 条消息?\n操作不可撤销。"
    )
    # en_US
    install_translator(qapp, locale="en_US")
    assert QCoreApplication.translate("MainWindow", "钉选确认") == "Pin confirmation"
    assert (
        QCoreApplication.translate(
            "MainWindow",
            "确定钉选选中的 %d 条消息?\n操作不可撤销。",
        )
        == "Pin the %d selected messages?\nThis action cannot be undone."
    )


def test_react_confirm_translations_both_locales(qapp) -> None:
    """2026-09-13 v1.7.5 PR #3:`回应确认` 文案 zh_CN / en_US 双语覆盖。"""
    install_translator(qapp, locale="zh_CN")
    assert QCoreApplication.translate("MainWindow", "回应确认") == "回应确认"
    zh_msg = QCoreApplication.translate(
        "MainWindow",
        "确定对选中的 %d 条消息打 %s 反应?\n操作不可撤销。",
    )
    assert "确定对选中的" in zh_msg and "反应" in zh_msg

    install_translator(qapp, locale="en_US")
    assert QCoreApplication.translate("MainWindow", "回应确认") == "React confirmation"
    en_msg = QCoreApplication.translate(
        "MainWindow",
        "确定对选中的 %d 条消息打 %s 反应?\n操作不可撤销。",
    )
    assert "reaction" in en_msg and "selected messages" in en_msg
