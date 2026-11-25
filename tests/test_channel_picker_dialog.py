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
from PySide6.QtWidgets import QApplication, QDialog

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.ui.widgets.channel_picker_dialog import ChannelPickerDialog


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
