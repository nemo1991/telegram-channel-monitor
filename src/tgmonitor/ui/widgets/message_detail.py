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
from PySide6.QtGui import QFont, QGuiApplication, QMouseEvent
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

from tgmonitor.core.dto import MediaDownloadStatus, MessageDTO, ReactionDTO

# 2026-09-04 v1.6.7:Lightbox 预览白名单统一从 MediaManagerWidget 导入,
# 不再本地拷贝 — 加 VIDEO/VIDEO_NOTE 一处生效。
from tgmonitor.ui.widgets.media_manager_widget import LIGHTBOX_PREVIEWABLE_TYPES
from tgmonitor.ui.widgets.reaction_format import format_reactions_short


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

    2026-09-11 v1.7.4:实现下沉到 `format_reactions_short`(同目录 `reaction_format.py`),
    LIVE 行 + 详情面板共用一份代码 — 避免两处分叉。
    详情面板全量展示(max_show=999),自投的用 `[]` 包。
    """
    return format_reactions_short(reactions, max_show=999, mark_chosen=True)


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

    # 2026-09-08 v1.7.0:详情顶部按钮 — 2026-11-16 v2.0.2 只读化后仅剩复制文本
    # (导出 / 删除随 LIVE 页只读化移除,core 层能力保留)
    copy_text_requested = Signal(str)  # 文本内容

    def __init__(self, parent: QWidget | None = None) -> None:
        """建空状态占位面板(无选中消息时显示)。"""
        super().__init__(parent)
        self.setObjectName("messageDetail")
        self.setFrameShape(QFrame.NoFrame)
        self.setWidgetResizable(True)
        self.setMinimumWidth(280)
        # 2026-09-14 v1.7.5 PR #4 (P0-H):max 删 — QSplitter 接管,用户可
        # 自由拖到 1/2 屏幕宽度(原 280-420 锁死,加锁 splitter 会被强制回弹)。

        self._current: MessageDTO | None = None
        # 2026-11-16:频道名映射,与 LIVE 列表共用同一份数据源
        # (MainWindow._refresh_state / MessageStreamController.on_message_received
        # 同步注入)。详情面板不再硬编码 `#channel_id`。
        self._channel_titles: dict[int, str] = {}
        # 2026-09-14 v1.7.5 PR #4 (P0-H):删 setMaximumWidth(420) —
        # 让 QSplitter 接管,用户拖拽自由(原 280-420 锁死)。保留 min=280。
        self._build_empty_state()

    def set_channel_titles(self, titles: dict[int, str]) -> None:
        """同步频道名映射(名称优先、无名称回退 #id)。

        与 `MessageView.set_channel_titles` 同签名,由 MainWindow /
        MessageStreamController 在同一处同时喂两个 widget,避免列表显示
        名称、详情又变回 id 的割裂。
        """
        self._channel_titles = dict(titles)

    def _channel_label(self, channel_id: int) -> str:
        """详情面板频道展示:有 title 用 `[title]`,无则 `[#id]` 兜底。"""
        title = self._channel_titles.get(channel_id)
        return f"[{title}]" if title else f"[#{channel_id}]"

    def _build_empty_state(self) -> None:
        """无选中时的占位 UI。"""
        # 用户可见文案统一走 tr() 包装。
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(24, 24, 24, 24)
        v.setSpacing(8)
        v.setAlignment(Qt.AlignCenter)

        icon = QLabel("💬")
        icon.setAlignment(Qt.AlignCenter)
        # 用 objectName 而不是 setStyleSheet:空状态 icon 走全局 QSS
        # (`QLabel#emptyHintIcon { font-size: 36px; }`),保持全局样式一致
        # + 也被 `form_row.empty_hint` 复用(同一 selector)。
        icon.setObjectName("emptyHintIcon")
        v.addWidget(icon)

        title = QLabel(self.tr("消息详情"))
        title.setAlignment(Qt.AlignCenter)
        title.setObjectName("pageTitle")
        v.addWidget(title)

        hint = QLabel(self.tr("点击左侧任意一条消息\n查看完整内容、媒体附件与原始数据"))
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        hint.setProperty("role", "hint")
        v.addWidget(hint)

        self.setWidget(wrap)

    def show_message(self, m: MessageDTO | None) -> None:
        """显示一条消息的详情。None = 回到占位。

        所有用户可见 label 走 tr() 包装。
        2026-09-14 v1.7.5 PR #4 (P2 bonus):同 cid+mid 已显示 → 跳过
        rebuild,保留 QScrollArea 滚动位置(用户切到别的消息再切回时,
        不再被甩回顶部)。
        """
        prev = self._current
        self._current = m
        if m is None:
            self._build_empty_state()
            return
        # 2026-09-14 v1.7.5 PR #4:同 cid+mid 已显示 → 保留 widget,只刷 _current
        if (
            prev is not None
            and self.widget() is not None
            and prev.channel_id == m.channel_id
            and prev.telegram_msg_id == m.telegram_msg_id
        ):
            # 已显示同一条 → 跳过 rebuild(滚动位置保留)
            return

        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(12)

        # ---- header: msg_id(左) + 复制文本(右) ----
        # 2026-11-18 v2.0.5:复制文本按钮从底部操作行上提到 header,与 #msg_id
        # 同一行平行;去掉底部独立的关闭行(详见本方法末尾)。
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header = QLabel(f"#{m.telegram_msg_id}")
        # 颜色 / font-size / letter-spacing 都走全局 QSS (`#detailHeader`)
        header.setObjectName("detailHeader")
        header_row.addWidget(header)
        header_row.addStretch(1)

        self._btn_copy_text = QPushButton(self.tr("📋 复制文本"))
        self._btn_copy_text.setObjectName("detailActionBtn")
        self._btn_copy_text.setEnabled(bool(m.text))  # 空文本无意义,灰掉
        self._btn_copy_text.clicked.connect(self._on_copy_text)
        header_row.addWidget(self._btn_copy_text)
        v.addLayout(header_row)

        # ---- 元数据 ----
        meta_group = QFrame()
        meta_group.setObjectName("metaCard")
        meta_layout = QVBoxLayout(meta_group)
        meta_layout.setContentsMargins(12, 8, 12, 8)
        meta_layout.setSpacing(2)

        if m.date:
            meta_layout.addWidget(_FieldRow(self.tr("时间"), _to_local_str(m.date)))
        if m.author:
            meta_layout.addWidget(_FieldRow(self.tr("作者"), m.author))
        meta_layout.addWidget(_FieldRow(self.tr("频道"), self._channel_label(m.channel_id)))
        # 2026-11-16:移除「浏览」字段 — 只读看板里浏览数无实际意义。
        if m.forwards:
            meta_layout.addWidget(_FieldRow(self.tr("转发"), f"{m.forwards:,}"))
        if m.reply_to_msg_id:
            meta_layout.addWidget(_FieldRow(self.tr("回复"), f"#{m.reply_to_msg_id}"))
        if m.edited:
            meta_layout.addWidget(_FieldRow(self.tr("已编辑"), "✓"))
        # 2026-08-27 v1.4.0 PR #10:reactions 列表(emoji + count + 自己投了高亮)
        if m.reactions:
            rx_label = self._format_reactions(m.reactions)
            if rx_label:
                meta_layout.addWidget(_FieldRow(self.tr("反应"), rx_label))
        v.addWidget(meta_group)

        # ---- 正文 ----
        # 2026-11-18 v2.0.5:去掉「📝 正文」小标题 —— 纯文本框自身已是正文,
        # 上方再挂一个标题浪费纵向空间(消息流要"省空间"的整体方向)。
        if m.text:
            text_edit = QPlainTextEdit(m.text)
            text_edit.setReadOnly(True)
            text_edit.setFrameShape(QFrame.NoFrame)
            text_edit.setMaximumHeight(220)
            # `transparent` 走 `#detailTextEdit` QSS
            text_edit.setObjectName("detailTextEdit")
            v.addWidget(text_edit)

        # ---- 媒体 ----
        if m.has_media:
            v.addWidget(self._section_label(self.tr(f"📎 媒体 ({len(m.media)})")))
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
                    med.type in LIGHTBOX_PREVIEWABLE_TYPES
                    and med.download_status == MediaDownloadStatus.DONE
                ):
                    med_label.setCursor(Qt.PointingHandCursor)
                    med_label.setToolTip(self.tr("点击查看大图"))
                    med_label.mousePressEvent = self._make_media_click_handler(  # type: ignore[method-assign, assignment]
                        m.channel_id, m.telegram_msg_id, i
                    )
                v.addWidget(med_label)

        # ---- 原始 JSON ----
        if m.raw:
            v.addWidget(self._section_label(self.tr("🔍 原始 JSON")))
            raw_str = json.dumps(m.raw, indent=2, ensure_ascii=False, default=str)
            raw_edit = QPlainTextEdit(raw_str)
            raw_edit.setReadOnly(True)
            raw_edit.setFrameShape(QFrame.NoFrame)
            raw_edit.setFont(QFont("Menlo, Consolas, monospace", 10))
            raw_edit.setMaximumHeight(260)
            # raw JSON box 背景 / 边框 / 圆角 → `#rawJsonEdit`
            raw_edit.setObjectName("rawJsonEdit")
            v.addWidget(raw_edit)

        # 2026-11-18 v2.0.5:底部操作行(复制文本 + 关闭详情)整体移除 ——
        # 复制文本已上提到 header 与 #msg_id 平行;关闭详情入口去掉(只读看板
        # 里点其它消息即切换,无需额外关闭按钮),省出的纵向空间给正文/媒体。
        # 保留 v.addStretch 让内容不足时贴顶,不撑满整块。
        v.addStretch(1)

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
        """Section header — 正文 / 媒体 / 原始 JSON 小节标题。"""
        lbl = QLabel(text)
        lbl.setObjectName("detailSectionLabel")
        return lbl

    # 2026-09-07 v1.6.8:从 @staticmethod 改 instance method,让 self.tr() 可用。
    def _format_media(self, med, idx: int) -> str:
        """格式化单条媒体信息。"""
        lines = [f"{idx}. {med.type.value}"]
        if med.mime_type:
            lines.append(self.tr(f"   类型: {med.mime_type}"))
        if med.file_name:
            lines.append(self.tr(f"   文件: {med.file_name}"))
        if med.file_size:
            size_mb = med.file_size / (1024 * 1024)
            lines.append(self.tr(f"   大小: {size_mb:.2f} MB ({med.file_size:,} 字节)"))
        if med.width and med.height:
            lines.append(self.tr(f"   尺寸: {med.width} × {med.height}"))
        if med.duration:
            lines.append(self.tr(f"   时长: {med.duration} 秒"))
        # 下载状态(异步下载队列回写;PENDING 不显示,避免旧数据噪音)
        if med.download_status == MediaDownloadStatus.DONE:
            lines.append(self.tr("   状态: 已下载 ✓"))
        elif med.download_status == MediaDownloadStatus.DOWNLOADING:
            lines.append(self.tr("   状态: 下载中… ⏳"))
        elif med.download_status == MediaDownloadStatus.FAILED:
            lines.append(self.tr("   状态: 下载失败 ❌"))
            if med.download_error:
                lines.append(self.tr(f"   原因: {med.download_error}"))
        return "\n".join(lines)

    # ---- 详情按钮 handler(2026-11-16 v2.0.2 只读化后仅剩复制文本) ----

    def _on_copy_text(self) -> None:
        """当前消息详情 → 复制文本到系统剪贴板。emit 文本给上层做 toast / 日志。

        空文本早 return(虽然按钮已 disabled,但双保险)。
        """
        if self._current is None or not self._current.text:
            return
        QGuiApplication.clipboard().setText(self._current.text)
        self.copy_text_requested.emit(self._current.text)

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
