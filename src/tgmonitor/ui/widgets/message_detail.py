"""MessageDetail — 单条消息详情面板。

点击 LIVE 视图里的某条消息 → 在右侧显示详情:
  - 完整正文(可滚动)
  - 作者 / 时间 / 频道 / msg_id 元数据
  - 媒体附件列表(类型 + 尺寸 + 文件名)
  - 原始 JSON(可读优先,显示键名)
  - 跳转到底层链接(copy + open)

默认隐藏;无选中消息时显示「点击消息查看详情」提示。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.dto import MessageDTO


def _to_local_str(dt: datetime | None) -> str:
    """naive datetime 视作 UTC 转本地时区字符串。"""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        # 假设是 naive UTC(项目里 _map_message 行为)
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


class _FieldRow(QWidget):
    """一行 「label: value」对齐展示。"""

    def __init__(self, label: str, value: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(8)

        lbl = QLabel(label + ":")
        lbl.setProperty("role", "hint")
        lbl.setFixedWidth(64)
        lbl.setAlignment(Qt.AlignTop | Qt.AlignRight)
        h.addWidget(lbl)

        val = QLabel(value)
        val.setWordWrap(True)
        val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        h.addWidget(val, 1)


class MessageDetail(QScrollArea):
    """详情面板 — 嵌入 LIVE 页的右侧。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("messageDetail")
        self.setFrameShape(QFrame.NoFrame)
        self.setWidgetResizable(True)
        self.setMinimumWidth(280)
        self.setMaximumWidth(420)

        self._current: MessageDTO | None = None
        self._build_empty_state()

    def _build_empty_state(self) -> None:
        """无选中时的占位 UI。"""
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(24, 24, 24, 24)
        v.setSpacing(8)
        v.setAlignment(Qt.AlignCenter)

        icon = QLabel("💬")
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet("font-size: 36px;")
        v.addWidget(icon)

        title = QLabel("消息详情")
        title.setAlignment(Qt.AlignCenter)
        title.setObjectName("pageTitle")
        v.addWidget(title)

        hint = QLabel("点击左侧任意一条消息\n查看完整内容、媒体附件与原始数据")
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        hint.setProperty("role", "hint")
        v.addWidget(hint)

        self.setWidget(wrap)

    def show_message(self, m: MessageDTO | None) -> None:
        """显示一条消息的详情。None = 回到占位。"""
        self._current = m
        if m is None:
            self._build_empty_state()
            return

        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(12)

        # ---- header: 频道 + msg_id ----
        header = QLabel(f"#{m.telegram_msg_id}")
        header.setStyleSheet("font-size: 11px; color: #8a8d92; letter-spacing: 0.5px;")
        v.addWidget(header)

        # ---- 元数据 ----
        meta_group = QFrame()
        meta_group.setObjectName("metaCard")
        meta_layout = QVBoxLayout(meta_group)
        meta_layout.setContentsMargins(12, 8, 12, 8)
        meta_layout.setSpacing(2)

        if m.date:
            meta_layout.addWidget(_FieldRow("时间", _to_local_str(m.date)))
        if m.author:
            meta_layout.addWidget(_FieldRow("作者", m.author))
        meta_layout.addWidget(_FieldRow("频道", f"#{m.channel_id}"))
        if m.views:
            meta_layout.addWidget(_FieldRow("浏览", f"{m.views:,}"))
        if m.forwards:
            meta_layout.addWidget(_FieldRow("转发", f"{m.forwards:,}"))
        if m.reply_to_msg_id:
            meta_layout.addWidget(_FieldRow("回复", f"#{m.reply_to_msg_id}"))
        if m.edited:
            meta_layout.addWidget(_FieldRow("已编辑", "✓"))
        v.addWidget(meta_group)

        # ---- 正文 ----
        if m.text:
            v.addWidget(self._section_label("📝 正文"))
            text_edit = QPlainTextEdit(m.text)
            text_edit.setReadOnly(True)
            text_edit.setFrameShape(QFrame.NoFrame)
            text_edit.setMaximumHeight(220)
            text_edit.setStyleSheet("background: transparent;")
            v.addWidget(text_edit)

        # ---- 媒体 ----
        if m.has_media:
            v.addWidget(self._section_label(f"📎 媒体 ({len(m.media)})"))
            for i, med in enumerate(m.media):
                med_label = QLabel(self._format_media(med, i + 1))
                med_label.setWordWrap(True)
                med_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
                med_label.setStyleSheet(
                    "background:#fafbfc;border:1px solid #e2e4e9;"
                    "border-radius:6px;padding:8px;margin-bottom:4px;"
                )
                v.addWidget(med_label)

        # ---- 原始 JSON ----
        if m.raw:
            v.addWidget(self._section_label("🔍 原始 JSON"))
            raw_str = json.dumps(m.raw, indent=2, ensure_ascii=False, default=str)
            raw_edit = QPlainTextEdit(raw_str)
            raw_edit.setReadOnly(True)
            raw_edit.setFrameShape(QFrame.NoFrame)
            raw_edit.setFont(QFont("Menlo, Consolas, monospace", 10))
            raw_edit.setMaximumHeight(260)
            raw_edit.setStyleSheet("background: #fafbfc; border: 1px solid #e2e4e9; border-radius: 6px;")
            v.addWidget(raw_edit)

        # 关闭按钮(顶部右上角 — 不在主视图,做成行内)
        v.addStretch(1)
        close_row = QHBoxLayout()
        close_row.addStretch(1)
        btn_close = QPushButton("关闭详情")
        btn_close.clicked.connect(lambda: self.show_message(None))
        close_row.addWidget(btn_close)
        v.addLayout(close_row)

        self.setWidget(wrap)

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "font-size: 12px; font-weight: 600; color: #5a5d64; "
            "padding-top: 4px; border-bottom: 1px solid #e2e4e9; padding-bottom: 4px;"
        )
        return lbl

    @staticmethod
    def _format_media(med, idx: int) -> str:
        """格式化单条媒体信息。"""
        lines = [f"{idx}. {med.type.value}"]
        if med.mime_type:
            lines.append(f"   类型: {med.mime_type}")
        if med.file_name:
            lines.append(f"   文件: {med.file_name}")
        if med.file_size:
            size_mb = med.file_size / (1024 * 1024)
            lines.append(f"   大小: {size_mb:.2f} MB ({med.file_size:,} 字节)")
        if med.width and med.height:
            lines.append(f"   尺寸: {med.width} × {med.height}")
        if med.duration:
            lines.append(f"   时长: {med.duration} 秒")
        return "\n".join(lines)