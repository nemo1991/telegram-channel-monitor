"""2026-09-11 v1.7.4:频道选择 dialog — 取代 `QInputDialog.getInt` 输入 chat_id。

设计要点:
- `ChannelDTO` 列表 + QListWidget(title + username + chat_id 灰色副标)
- 顶部 QLineEdit 搜索过滤(title / username 子串匹配,大小写不敏感)
- 双击 / 「确定」 → 返 `channel.id`(Telegram chat_id)
- 「取消」 / 关闭 → None
- 空列表 → OK 禁用,搜索无结果 → 显示「无匹配」
- 静态 `pick_channel(parent, channels)` — 仿 `QInputDialog.getItem`

平台:Qt6 已跨平台,无 Linux 字体问题。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.dto import ChannelDTO


class ChannelPickerDialog(QDialog):
    """2026-09-11 v1.7.4:频道选择 list — 取代 QInputDialog.getInt。

    Returns:`channel_id: int` 或 None(cancel / close)。
    """

    def __init__(self, channels: list[ChannelDTO], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("选择目标频道…"))
        self.setMinimumSize(420, 320)

        self._channels: list[ChannelDTO] = list(channels)
        self._selected_channel_id: int | None = None

        root = QVBoxLayout(self)

        # 顶部搜索
        search_row = QHBoxLayout()
        search_lbl = QLabel(self.tr("搜索(title / @username):"))
        search_lbl.setObjectName("channelSearchLabel")
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(self.tr("输入关键字过滤…"))
        self._search_input.setObjectName("channelSearchInput")
        self._search_input.textChanged.connect(self._apply_filter)
        search_row.addWidget(search_lbl)
        search_row.addWidget(self._search_input, 1)
        root.addLayout(search_row)

        # 列表
        self._list = QListWidget()
        self._list.setObjectName("channelList")
        self._list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._list.currentItemChanged.connect(self._on_current_changed)
        root.addWidget(self._list, 1)

        # 计数标签
        self._count_label = QLabel("")
        self._count_label.setObjectName("channelCountLabel")
        root.addWidget(self._count_label)

        # OK / Cancel
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)

        # 初始填充
        self._populate(self._channels)
        # 空列表 → OK 禁用
        if not self._channels:
            self._disable_ok()
        else:
            self._list.setCurrentRow(0)

    def _populate(self, channels: list[ChannelDTO]) -> None:
        """填充列表 — 每行:title + (username / chat_id) 灰色副标。"""
        self._list.clear()
        for ch in channels:
            text = self._format_row_text(ch)
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, ch.id)
            # 提示:完整 ID + kind
            item.setToolTip(
                self.tr("chat_id: {cid} · {kind}").format(cid=ch.id, kind=self.tr(ch.kind))
            )
            self._list.addItem(item)
        self._update_count_label(len(channels))

    def _format_row_text(self, ch: ChannelDTO) -> str:
        """单行文本:`<title>` + `<@username / <chat_id>>` 灰色副标。

        副标用半角空格分隔,Qt QListWidget 不支持富文本(用纯文本 + 副标符号)。
        """
        title = ch.title or self.tr("(无标题)")
        if ch.username:
            return f"{title}  @{ch.username}  · {ch.id}"
        return f"{title}  · {ch.id}"

    def _apply_filter(self, keyword: str) -> None:
        """按 title / username 子串过滤(大小写不敏感)。"""
        kw = keyword.strip().lower()
        if not kw:
            filtered = self._channels
        else:
            filtered = [
                ch
                for ch in self._channels
                if kw in (ch.title or "").lower() or kw in (ch.username or "").lower()
            ]
        self._populate(filtered)
        if not filtered:
            self._disable_ok()
        elif self._channels:
            # 任意匹配 → 重置 OK(原本可能因空被禁用)
            self._enable_ok()
        if filtered:
            self._list.setCurrentRow(0)

    def _update_count_label(self, shown: int) -> None:
        total = len(self._channels)
        if shown == total:
            self._count_label.setText(self.tr("共 {n} 个频道").format(n=total))
        else:
            self._count_label.setText(
                self.tr("显示 {shown} / {total} 个频道").format(shown=shown, total=total)
            )

    def _disable_ok(self) -> None:
        btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if btn is not None:
            btn.setEnabled(False)

    def _enable_ok(self) -> None:
        btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if btn is not None:
            btn.setEnabled(True)

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        """双击 = 选 + OK。"""
        cid = item.data(Qt.ItemDataRole.UserRole)
        if cid is None:
            return
        self._selected_channel_id = int(cid)
        self.accept()

    def _on_current_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        """选中变化 — 记录 channel_id。"""
        if current is None:
            return
        cid = current.data(Qt.ItemDataRole.UserRole)
        if cid is not None:
            self._selected_channel_id = int(cid)
            self._enable_ok()

    def accept(self) -> None:
        """覆写:必须选中非空,否则等同 reject。"""
        if self._selected_channel_id is None:
            return
        super().accept()

    def selected_channel_id(self) -> int | None:
        return self._selected_channel_id

    @classmethod
    def pick_channel(cls, parent: QWidget | None, channels: list[ChannelDTO]) -> int | None:
        """2026-09-11 v1.7.4:静态入口 — 仿 `QInputDialog.getItem`。

        Returns:
            `channel_id` 或 None(cancel / close / 空列表)。
        """
        dlg = cls(channels, parent)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return dlg.selected_channel_id()
