"""2026-09-11 v1.7.4:`ChannelPickerDialog` 单元测试。

覆盖:
- 构造期默认状态(标题、OK 启用、列表填充)
- 空 channel 列表 → OK 禁用
- 双击 item → accept + selected_channel_id
- 选中变化 → enable OK + 记录 channel_id
- 搜索过滤(title / username 子串,大小写不敏感)
- pick_channel 静态入口(Accepted → channel_id;Rejected → None)
- 关闭 / cancel → None

模式:直接构造 widget,模拟 _on_current_changed / itemDoubleClicked
信号(避免 modal exec 阻塞测试)。
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.ui.widgets.channel_picker_dialog import ChannelPickerDialog


@pytest.fixture
def qapp() -> QApplication:
    """QApplication 实例 — widget 测试需要 event loop 集成。"""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


@pytest.fixture
def channels() -> list[ChannelDTO]:
    return [
        ChannelDTO(id=100, title="Tech News", username="technews"),
        ChannelDTO(id=200, title="Python 中文", username="python_zh"),
        ChannelDTO(id=300, title="私密日记"),  # 无 username
        ChannelDTO(id=400, title="Rust 编程", username="rustlang"),
    ]


@pytest.fixture
def dialog(qapp: QApplication, channels: list[ChannelDTO]) -> ChannelPickerDialog:
    return ChannelPickerDialog(channels)


def _drain(qapp: QApplication) -> None:
    qapp.processEvents()


def test_dialog_constructs_with_title_and_populated_list(
    dialog: ChannelPickerDialog, qapp: QApplication, channels: list[ChannelDTO]
) -> None:
    """默认:window title = 「选择目标频道…」,列表 4 项,OK 启用。

    默认 currentRow = 0(UX:列表多时,第一个常是热门候选,免一次点击)→
    selected_channel_id 也跟着设为第一个 id。
    """
    _drain(qapp)
    assert dialog.windowTitle() == dialog.tr("选择目标频道…")
    assert dialog._list.count() == len(channels)
    ok_btn = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok_btn is not None
    assert ok_btn.isEnabled()
    assert dialog.selected_channel_id() == channels[0].id


def test_dialog_first_row_is_selected_on_init(
    dialog: ChannelPickerDialog, qapp: QApplication
) -> None:
    """初始:第 0 行自动 currentRow → selected_channel_id = 第一个 id。"""
    _drain(qapp)
    assert dialog._list.currentRow() == 0
    assert dialog.selected_channel_id() == 100


def test_dialog_empty_list_disables_ok(qapp: QApplication) -> None:
    """空 channels 列表 → OK 禁用,selected = None。"""
    dlg = ChannelPickerDialog([])
    _drain(qapp)
    ok_btn = dlg._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok_btn is not None
    assert not ok_btn.isEnabled()
    assert dlg.selected_channel_id() is None


def test_dialog_row_text_includes_title_and_id(
    dialog: ChannelPickerDialog, qapp: QApplication, channels: list[ChannelDTO]
) -> None:
    """行文本:`title + @username + · id` 或 `title + · id`(无 username)。"""
    _drain(qapp)
    # 第 1 行(有 username)
    item0 = dialog._list.item(0)
    assert item0 is not None
    text0 = item0.text()
    assert "Tech News" in text0
    assert "@technews" in text0
    assert "100" in text0
    # 第 3 行(无 username — 第 3 个 channel 是私密日记)
    item2 = dialog._list.item(2)
    assert item2 is not None
    text2 = item2.text()
    assert "私密日记" in text2
    assert "300" in text2
    assert "@" not in text2


def test_dialog_double_click_returns_channel_id(
    dialog: ChannelPickerDialog, qapp: QApplication
) -> None:
    """双击 item → 立即 accept + selected_channel_id = 该 channel.id。"""
    _drain(qapp)
    # 选第 2 行(Python 中文 = 200)
    dialog._list.setCurrentRow(1)
    item = dialog._list.item(1)
    assert item is not None
    # 模拟双击
    dialog._on_item_double_clicked(item)
    _drain(qapp)
    assert dialog.selected_channel_id() == 200


def test_dialog_search_filters_by_title(dialog: ChannelPickerDialog, qapp: QApplication) -> None:
    """按 title 子串过滤 — 「Rust」→ 只 1 行。"""
    dialog._search_input.setText("Rust")
    _drain(qapp)
    assert dialog._list.count() == 1
    item0 = dialog._list.item(0)
    assert item0 is not None
    assert "Rust" in item0.text()


def test_dialog_search_filters_by_username(dialog: ChannelPickerDialog, qapp: QApplication) -> None:
    """按 username 子串过滤 — 「python」→ Python 中文。"""
    dialog._search_input.setText("python")
    _drain(qapp)
    assert dialog._list.count() == 1
    item0 = dialog._list.item(0)
    assert item0 is not None
    assert "Python" in item0.text()


def test_dialog_search_case_insensitive(dialog: ChannelPickerDialog, qapp: QApplication) -> None:
    """搜索大小写不敏感 — 「TECH」匹配 Tech News。"""
    dialog._search_input.setText("TECH")
    _drain(qapp)
    assert dialog._list.count() == 1


def test_dialog_search_no_match_shows_zero_rows(
    dialog: ChannelPickerDialog, qapp: QApplication
) -> None:
    """搜索无匹配 → 0 行 + OK 禁用 + count_label 提示。"""
    dialog._search_input.setText("无此频道")
    _drain(qapp)
    assert dialog._list.count() == 0
    ok_btn = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok_btn is not None
    assert not ok_btn.isEnabled()
    assert "0" in dialog._count_label.text()


def test_dialog_search_clear_restores_all(
    dialog: ChannelPickerDialog, qapp: QApplication, channels: list[ChannelDTO]
) -> None:
    """清空搜索 → 全部恢复 + OK 启用。"""
    dialog._search_input.setText("Rust")
    _drain(qapp)
    assert dialog._list.count() == 1
    dialog._search_input.setText("")
    _drain(qapp)
    assert dialog._list.count() == len(channels)
    ok_btn = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok_btn is not None
    assert ok_btn.isEnabled()


def test_dialog_accept_without_selection_is_noop(
    dialog: ChannelPickerDialog, qapp: QApplication
) -> None:
    """没选就 accept() → 静默忽略(等同 reject)。"""
    dialog._selected_channel_id = None
    dialog.accept()  # type: ignore[func-returns-value]
    _drain(qapp)
    assert dialog.selected_channel_id() is None


def test_pick_channel_classmethod_returns_none_on_reject(
    qapp: QApplication,
    channels: list[ChannelDTO],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pick_channel:cancel → None。"""
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    result = ChannelPickerDialog.pick_channel(None, channels)
    assert result is None


def test_pick_channel_classmethod_returns_channel_id_on_accept(
    qapp: QApplication,
    channels: list[ChannelDTO],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pick_channel:Accepted + selected set → channel.id。"""
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(ChannelPickerDialog, "selected_channel_id", lambda self: 200)
    result = ChannelPickerDialog.pick_channel(None, channels)
    assert result == 200


def test_dialog_cleanup_on_close(dialog: ChannelPickerDialog, qapp: QApplication) -> None:
    """关窗不抛(防 test teardown 报警)。"""
    _drain(qapp)
    dialog.close()
    dialog.deleteLater()
    _drain(qapp)
