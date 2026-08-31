# mypy: disable-error-code="attr-defined"
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

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QMouseEvent
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

from tgmonitor.core.dto import MediaDownloadStatus, MediaType, MessageDTO, ReactionDTO

# 2026-08-31 v1.5.0 PR #A8:Lightbox 预览白名单 — 与 MediaManagerWidget 同源。
# 媒体卡片可点 = 可点缩略图弹大图;非图 / 未下载 走系统查看器,保持原行为。
_LIGHTBOX_PREVIEWABLE_TYPES: frozenset[MediaType] = frozenset(
    {MediaType.PHOTO, MediaType.STICKER, MediaType.ANIMATION}
)


def _to_local_str(dt: datetime | None) -> str:
    """naive datetime 视作 UTC 转本地时区字符串。"""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        # 假设是 naive UTC(项目里 _map_message 行为)
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _format_reactions(reactions: list[ReactionDTO]) -> str:
    """PR #10:reactions 列表 → 单行展示 `😀 5  👍 3  ...`。

    自己投的用方括号包起来,例如 `[❤️ 1] 😀 5`;空 list 返 None(让 caller
    跳过本行)。
    """
    if not reactions:
        return ""
    parts: list[str] = []
    for r in reactions:
        # count=0 跳过(TDLib 偶尔推送空 reaction 占位)
        if r.count <= 0:
            continue
        body = f"{r.emoji} {r.count}"
        if r.is_chosen:
            parts.append(f"[{body}]")
        else:
            parts.append(body)
    return "  ".join(parts)


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

    # 2026-08-31 v1.5.0 PR #A8:Lightbox 内嵌预览 — 点媒体卡(PHOTO/STICKER/ANIMATION
    # 且 DONE 状态)→ 异步加载原图 bytes → 弹 LightboxDialog。MainWindow 接到信号
    # 后处理流程同 `_on_media_preview`(复用 main_window.py 内的加载器)。
    preview_requested = Signal(int, int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        """建空状态占位面板(无选中消息时显示)。"""
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
        # 用 objectName 而不是 setStyleSheet:空状态 icon 走全局 QSS
        # (`QLabel#emptyHintIcon { font-size: 36px; }`),保持主题切换一致
        # + 也被 `form_row.empty_hint` 复用(同一 selector)。
        icon.setObjectName("emptyHintIcon")
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
        # 颜色 / font-size / letter-spacing 都走全局 QSS (`#detailHeader`)
        header.setObjectName("detailHeader")
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
        # 2026-08-27 v1.4.0 PR #10:reactions 列表(emoji + count + 自己投了高亮)
        if m.reactions:
            rx_label = self._format_reactions(m.reactions)
            if rx_label:
                meta_layout.addWidget(_FieldRow("反应", rx_label))
        v.addWidget(meta_group)

        # ---- 正文 ----
        if m.text:
            v.addWidget(self._section_label("📝 正文"))
            text_edit = QPlainTextEdit(m.text)
            text_edit.setReadOnly(True)
            text_edit.setFrameShape(QFrame.NoFrame)
            text_edit.setMaximumHeight(220)
            # `transparent` 走 `#detailTextEdit` QSS
            text_edit.setObjectName("detailTextEdit")
            v.addWidget(text_edit)

        # ---- 媒体 ----
        if m.has_media:
            v.addWidget(self._section_label(f"📎 媒体 ({len(m.media)})"))
            for i, med in enumerate(m.media):
                med_label = QLabel(self._format_media(med, i + 1))
                med_label.setWordWrap(True)
                med_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
                # 媒体卡背景 / 边框 / 圆角 / padding 都走 `#mediaItem`
                med_label.setObjectName("mediaItem")
                # 2026-08-31 v1.5.0 PR #A8:Lightbox 可点 — 图片类(photo/animation/sticker)
                # 且已下载完成 → 设 PointingHandCursor + monkeypatch mousePressEvent;
                # 非图 / 未下载 保持默认箭头 + 不响应(走系统查看器 fallback)。
                if (
                    med.type in _LIGHTBOX_PREVIEWABLE_TYPES
                    and med.download_status == MediaDownloadStatus.DONE
                ):
                    med_label.setCursor(Qt.PointingHandCursor)
                    med_label.setToolTip("点击查看大图")
                    med_label.mousePressEvent = self._make_media_click_handler(  # type: ignore[method-assign, assignment]
                        m.channel_id, m.telegram_msg_id, i
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
            # raw JSON box 背景 / 边框 / 圆角 → `#rawJsonEdit`
            raw_edit.setObjectName("rawJsonEdit")
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

    def refresh_if_showing(self, channel_id: int, telegram_msg_id: int) -> None:
        """下载状态回写后,若详情面板正显示该消息则重建(状态行更新)。"""
        cur = self._current
        if (
            cur is not None
            and cur.channel_id == channel_id
            and cur.telegram_msg_id == telegram_msg_id
        ):
            self.show_message(cur)

    def _section_label(self, text: str) -> QLabel:
        """Section header — 正文 / 媒体 / 原始 JSON 小节标题。

        走全局 QSS `#detailSectionLabel`(浅色 / 暗色 各一份)。
        """
        lbl = QLabel(text)
        lbl.setObjectName("detailSectionLabel")
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
        # 下载状态(异步下载队列回写;PENDING 不显示,避免旧数据噪音)
        if med.download_status == MediaDownloadStatus.DONE:
            lines.append("   状态: 已下载 ✓")
        elif med.download_status == MediaDownloadStatus.DOWNLOADING:
            lines.append("   状态: 下载中… ⏳")
        elif med.download_status == MediaDownloadStatus.FAILED:
            lines.append("   状态: 下载失败 ❌")
            if med.download_error:
                lines.append(f"   原因: {med.download_error}")
        return "\n".join(lines)

    def _make_media_click_handler(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> object:
        """2026-08-31 v1.5.0 PR #A8:媒体卡 click → 发 preview_requested。

        闭包绑死 (channel_id, telegram_msg_id, media_idx),monkeypatch 到
        `med_label.mousePressEvent`(直接覆盖 Qt 实例方法;MediaManagerWidget
        同样的 MVP 取舍,详见那里注释)。
        """
        outer = self

        def _handler(event: QMouseEvent) -> None:
            if event is None:
                return
            if event.button() != Qt.LeftButton:
                return
            outer.preview_requested.emit(channel_id, telegram_msg_id, media_idx)

        return _handler
