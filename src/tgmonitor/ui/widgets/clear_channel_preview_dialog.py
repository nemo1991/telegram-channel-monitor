"""ClearChannelPreviewDialog — 2026-08-25 v1.3.0 PR #8。

Clear Channel 操作的 dry-run 二次确认对话框:
1. 展示 `DeleteChannelPreview`(message_count / media_count / potential_orphan_bytes)
2. 警告「不可撤销」
3. 用户必须勾上「我已了解以上操作不可撤销」才能 enable OK 按钮
4. Cancel → Dialog.Rejected;OK → Dialog.Accepted

`main_window._on_media_clear_channel` 调 Accept/Reject 决定是否真的走
`vm.delete_by_channel`。
"""

# mypy: disable-error-code="attr-defined"

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.dto import DeleteChannelPreview


def _format_bytes(n: int) -> str:
    """人类可读字节数;`n==0` 走「0 B」。"""
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(n)
    i = 0
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.1f}{units[i]}" if i > 0 else f"{int(size)}B"


class ClearChannelPreviewDialog(QDialog):
    """2026-08-25 v1.3.0 PR #8:Clear Channel 二次确认 dialog。

    显示预览(message_count / media_count / potential_orphan_bytes),必勾
    ack checkbox 才 enable OK。
    """

    def __init__(
        self,
        preview: DeleteChannelPreview,
        channel_title: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._preview = preview
        self._channel_title = channel_title
        self.setWindowTitle("Clear Channel — 二次确认")
        self.setModal(True)
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # 标题
        title_label = QLabel(f"🗑 清空频道 {self._channel_title or f'#{self._preview.channel_id}'}")
        title_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(title_label)

        # 警告
        warn = QLabel("⚠ 此操作不可撤销。确认前请仔细检查以下数据:")
        layout.addWidget(warn)

        # 数据项
        layout.addWidget(QLabel(f"  • 消息数: {self._preview.message_count}"))
        layout.addWidget(QLabel(f"  • 媒体数: {self._preview.media_count}"))
        layout.addWidget(
            QLabel(f"  • 预计释放对象存储: {_format_bytes(self._preview.potential_orphan_bytes)}"),
        )
        layout.addWidget(
            QLabel(
                "    (跨频道共享的对象存储 bytes 不计入 — refcount > 1 的 key 不会被清理)",
            ),
        )

        # 必勾确认
        self.chk_ack = QCheckBox("我已了解以上操作不可撤销")
        layout.addWidget(self.chk_ack)

        layout.addStretch(1)

        # 按钮
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("确认清空")
        bb.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.chk_ack.toggled.connect(
            lambda checked: bb.button(QDialogButtonBox.StandardButton.Ok).setEnabled(checked)
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)
