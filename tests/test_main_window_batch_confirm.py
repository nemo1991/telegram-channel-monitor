"""PR #3 批量操作二次确认测试 — 2026-09-13 v1.7.5 (P0-C)。

覆盖场景:
- `_on_live_pin` 答 No → 不调 `_run_live_pin`(钉选是写服务端,不可撤销)
- `_on_live_pin` 答 Yes → 调 `_run_live_pin(items)`
- `_on_live_react` 答 No → 不调 `_run_live_react`
- `_on_live_react` 答 Yes → 调 `_run_live_react(items, emoji, is_big)`
- `clear_selection` 仅在答 Yes 后调
- i18n 文案覆盖(zh_CN / en_US 都非空)
"""

from __future__ import annotations

import os
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402

from tgmonitor.i18n import install_translator  # noqa: E402
from tgmonitor.ui.main_window import MainWindow  # noqa: E402

# ---- fixtures ----


class _FakeMainWindow:
    """最小桩:_on_live_pin / _on_live_react 需要的方法 + state。"""

    def __init__(self) -> None:
        self._pin_called_with: list | None = None
        self._react_called_with: tuple[list, str, bool] | None = None
        self._clear_selection_count = 0
        self._user_answers_yes = True
        # live_view 桩:有 clear_selection 方法
        self.live_view = self

    # QObject.tr() 桩 — 直接返源串(lupdate/lrelease 抽的 key 已含 placeholder)
    def tr(self, source: str) -> str:  # noqa: ANN001, ANN201
        return source

    # 桩 QMessageBox.warning(简单用 flag 控制答案)
    def warning(self, *args, **kwargs):  # noqa: ANN001, ANN201
        from PySide6.QtWidgets import QMessageBox

        return QMessageBox.Yes if self._user_answers_yes else QMessageBox.No

    # 桩 live_view.clear_selection(由 main_window 调 self.live_view.clear_selection())
    def clear_selection(self) -> None:
        self._clear_selection_count += 1

    # 桩 emoji picker(react 不弹真实 dialog)
    def _emoji_pick_returns(self) -> tuple[str, bool] | None:
        return ("👍", False)

    # 桩 _run_live_*
    def _run_live_pin(self, items: list) -> None:
        self._pin_called_with = items

    def _run_live_react(self, items: list, emoji: str, is_big: bool = False) -> None:
        self._react_called_with = (items, emoji, is_big)


def _patch_pickers_and_dialog(monkeypatch: pytest.MonkeyPatch, win: _FakeMainWindow) -> None:
    """绕过真实 QMessageBox / EmojiPickerDialog。"""

    # QMessageBox.warning → win.warning(由 _user_answers_yes 控)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", win.warning)
    # EmojiPickerDialog.get_emoji → win._emoji_pick_returns()
    monkeypatch.setattr(
        "tgmonitor.ui.widgets.emoji_picker_dialog.EmojiPickerDialog.get_emoji",
        lambda parent: win._emoji_pick_returns(),
    )


def test_pin_yes_calls_run_live_pin(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """点 toolbar「📌 钉选」→ QMessageBox 答 Yes → 调 _run_live_pin。"""
    win = _FakeMainWindow()
    win._user_answers_yes = True
    _patch_pickers_and_dialog(monkeypatch, win)

    items = [(1, 100), (2, 200), (3, 300)]
    MainWindow._on_live_pin(cast(MainWindow, win), items)

    assert win._pin_called_with == items, "Yes 应调 _run_live_pin"
    assert win._clear_selection_count == 1, "Yes 之后应 clear_selection"


def test_pin_no_does_not_call_run_live_pin(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """QMessageBox 答 No → 不调 _run_live_pin,也不 clear_selection。"""
    win = _FakeMainWindow()
    win._user_answers_yes = False
    _patch_pickers_and_dialog(monkeypatch, win)

    items = [(1, 100), (2, 200)]
    MainWindow._on_live_pin(cast(MainWindow, win), items)

    assert win._pin_called_with is None, "No 不能调 _run_live_pin"
    assert win._clear_selection_count == 0, "No 不 clear_selection"


def test_pin_empty_items_no_dialog(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """空选区 → 不弹 dialog,也不调 _run_live_pin。"""
    win = _FakeMainWindow()
    win._user_answers_yes = True
    _patch_pickers_and_dialog(monkeypatch, win)

    MainWindow._on_live_pin(cast(MainWindow, win), [])
    assert win._pin_called_with is None
    # clear_selection 也不调(空选区清无意义)


def test_react_yes_calls_run_live_react(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """emoji 选完后答 Yes → 调 _run_live_react(items, emoji, is_big)。"""
    win = _FakeMainWindow()
    win._user_answers_yes = True
    _patch_pickers_and_dialog(monkeypatch, win)

    items = [(10, 1000), (20, 2000)]
    MainWindow._on_live_react(cast(MainWindow, win), items)

    assert win._react_called_with is not None
    items_arg, emoji, is_big = win._react_called_with
    assert items_arg == items
    assert emoji == "👍"
    assert is_big is False
    assert win._clear_selection_count == 1


def test_react_no_does_not_call_run_live_react(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """emoji 选完后答 No → 不调 _run_live_react。"""
    win = _FakeMainWindow()
    win._user_answers_yes = False
    _patch_pickers_and_dialog(monkeypatch, win)

    items = [(10, 1000)]
    MainWindow._on_live_react(cast(MainWindow, win), items)

    assert win._react_called_with is None
    assert win._clear_selection_count == 0


def test_react_picker_cancel_no_dialog(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """emoji picker 选 None(取消)→ 不弹 QMessageBox,不调 _run_live_react。"""
    win = _FakeMainWindow()

    def _pick_none(parent):  # noqa: ANN001
        return None

    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", win.warning)
    monkeypatch.setattr(
        "tgmonitor.ui.widgets.emoji_picker_dialog.EmojiPickerDialog.get_emoji",
        _pick_none,
    )

    MainWindow._on_live_react(cast(MainWindow, win), [(1, 100)])
    assert win._react_called_with is None
    assert win._clear_selection_count == 0


def test_react_empty_items_no_dialog(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """空选区 → 不弹 emoji picker。"""
    win = _FakeMainWindow()
    win._user_answers_yes = True

    picker_called = {"n": 0}

    def _pick_called(parent):  # noqa: ANN001
        picker_called["n"] += 1
        return ("👍", False)

    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", win.warning)
    monkeypatch.setattr(
        "tgmonitor.ui.widgets.emoji_picker_dialog.EmojiPickerDialog.get_emoji",
        _pick_called,
    )

    MainWindow._on_live_react(cast(MainWindow, win), [])
    assert win._react_called_with is None
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
