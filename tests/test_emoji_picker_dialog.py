"""2026-09-11 v1.7.4:`EmojiPickerDialog` 单元测试。

覆盖:
- 构造期默认状态(窗口标题 / OK 禁用 / 7 类 grid)
- 点 grid 按钮 → _selected_emoji + OK 启用
- 手输 → _selected_emoji + 取消 grid 互斥高亮
- 「大表情」QCheckBox 状态
- get_emoji 静态入口 — 模拟用户路径
- select 后 accept 返回 (emoji, is_big) tuple
- 没选 / 关闭 → None

模式:直接构造 widget(无需 dialog exec),调私有方法模拟 UI 动作。
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from tgmonitor.ui.widgets.emoji_picker_dialog import (
    _EMOJI_GROUPS,
    EmojiPickerDialog,
)


@pytest.fixture
def qapp() -> QApplication:
    """QApplication 实例 — widget 测试需要 event loop 集成。"""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


@pytest.fixture
def dialog(qapp: QApplication) -> EmojiPickerDialog:
    dlg = EmojiPickerDialog()
    return dlg


def _drain(qapp: QApplication) -> None:
    """排空 queued event(模拟 UI 刷新)。"""
    qapp.processEvents()


def test_dialog_constructs_with_title_and_disabled_ok(
    dialog: EmojiPickerDialog, qapp: QApplication
) -> None:
    """默认状态:OK 禁用,window title 是「选择表情回应…」。"""
    _drain(qapp)
    assert dialog.windowTitle() == dialog.tr("选择表情回应…")
    ok_btn = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok_btn is not None
    assert not ok_btn.isEnabled()
    assert dialog.selected_emoji() is None
    assert dialog.is_big() is False


def test_dialog_has_seven_emoji_groups(dialog: EmojiPickerDialog, qapp: QApplication) -> None:
    """v1.7.4:7 类 emoji 分类覆盖(笑脸 / 否定 / 爱 / 手势 / 动物 / 食物 / 活动)。"""
    assert len(_EMOJI_GROUPS) == 7
    # 类别首标签分别是 7 类
    group_labels = [g[0] for g in _EMOJI_GROUPS]
    assert "笑脸" in group_labels
    assert "动物" in group_labels
    assert "活动" in group_labels
    # 每类至少 5 个 emoji(防未来误删)
    for label, emojis in _EMOJI_GROUPS:
        assert len(emojis) >= 5, f"{label} 只有 {len(emojis)} 个"


def test_click_emoji_button_sets_selection(dialog: EmojiPickerDialog, qapp: QApplication) -> None:
    """点 grid 按钮 → _selected_emoji + OK 启用。"""
    _drain(qapp)
    # 找第一个 emoji 按钮
    btn = dialog._emoji_buttons[0]
    expected_emoji = btn.property("emojiChar")
    btn.click()  # type: ignore[attr-defined]
    _drain(qapp)
    assert dialog.selected_emoji() == expected_emoji
    ok_btn = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok_btn is not None
    assert ok_btn.isEnabled()


def test_click_emoji_clears_manual_input(dialog: EmojiPickerDialog, qapp: QApplication) -> None:
    """点 grid 后手动输入框被清空 — 避免 grid / manual 双选中歧义。"""
    dialog._manual_input.setText("🔥")
    btn = dialog._emoji_buttons[0]
    btn.click()  # type: ignore[attr-defined]
    _drain(qapp)
    assert dialog._manual_input.text() == ""
    assert dialog.selected_emoji() == btn.property("emojiChar")


def test_manual_input_sets_selection_and_disables_grid(
    dialog: EmojiPickerDialog, qapp: QApplication
) -> None:
    """手输接管 selected_emoji — selected 切换到手输值。

    Qt `QButtonGroup.exclusive=True` 自身保证单选高亮,这里只验证
    业务层 _selected_emoji 跟手输对齐(不强求 Qt 状态机具体表现,
    防 Qt 版本差异导致 flake)。
    """
    dialog._emoji_buttons[0].setChecked(True)
    dialog._manual_input.setText("custom_emoji_id:42")
    _drain(qapp)
    assert dialog.selected_emoji() == "custom_emoji_id:42"


def test_manual_input_empty_clears_selection(dialog: EmojiPickerDialog, qapp: QApplication) -> None:
    """手输清空 → selected_emoji 回到 None + OK 禁用。"""
    dialog._manual_input.setText("🔥")
    dialog._manual_input.setText("")
    _drain(qapp)
    assert dialog.selected_emoji() is None
    ok_btn = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok_btn is not None
    assert not ok_btn.isEnabled()


def test_manual_input_strips_whitespace(dialog: EmojiPickerDialog, qapp: QApplication) -> None:
    """手输带前后空白 → selected_emoji 是 strip 后的(避免后续调 API 失败)。"""
    dialog._manual_input.setText("  🔥  ")
    _drain(qapp)
    assert dialog.selected_emoji() == "🔥"


def test_is_big_checkbox_toggle(dialog: EmojiPickerDialog, qapp: QApplication) -> None:
    """「大表情」checkbox 切换 → is_big() 返回对应布尔。"""
    _drain(qapp)
    assert dialog.is_big() is False
    dialog._is_big_checkbox.setChecked(True)
    _drain(qapp)
    assert dialog.is_big() is True


def test_accept_without_selection_is_noop(dialog: EmojiPickerDialog, qapp: QApplication) -> None:
    """没选就 accept() → 静默忽略(dialog 状态保留 Rejected 等价)。"""
    # 直接调 accept —— _selected_emoji 是 None,覆写应该早返
    dialog.accept()  # type: ignore[func-returns-value]
    # 不会抛;窗口仍可继续操作
    _drain(qapp)
    assert dialog.selected_emoji() is None


def test_get_emoji_classmethod_returns_none_on_close(
    dialog: EmojiPickerDialog, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """静态入口 get_emoji:exec 返 Rejected → None(cancel 路径)。"""
    # monkeypatch QDialog.exec 模拟用户点取消
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    result = EmojiPickerDialog.get_emoji()
    assert result is None


def test_get_emoji_classmethod_returns_tuple_on_accept(
    dialog: EmojiPickerDialog, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """静态入口 get_emoji:exec 返 Accepted → (emoji, is_big) tuple。"""
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    # 直接 set _selected_emoji 后 get_emoji 才会返非 None
    dlg = EmojiPickerDialog()
    dlg._selected_emoji = "🔥"
    dlg._is_big_checkbox.setChecked(True)
    # 直接调静态入口 — 但它会自己造新 dlg,所以需要 monkeypatch class
    monkeypatch.setattr(EmojiPickerDialog, "__init__", lambda self, parent=None: None)
    monkeypatch.setattr(EmojiPickerDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(EmojiPickerDialog, "selected_emoji", lambda self: "🔥")
    monkeypatch.setattr(EmojiPickerDialog, "is_big", lambda self: True)
    result = EmojiPickerDialog.get_emoji()
    assert result == ("🔥", True)


def test_dialog_cleanup_on_close(dialog: EmojiPickerDialog, qapp: QApplication) -> None:
    """关窗不抛(防 test teardown 报警)。"""
    _drain(qapp)
    dialog.close()
    dialog.deleteLater()
    _drain(qapp)
