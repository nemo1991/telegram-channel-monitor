# mypy: disable-error-code="attr-defined"
"""MessageManagerWidget — v2.0.8 新增「消息」导航页:查看 + 筛选已落库消息。

与大实时流(`MessageView`)不同,本页是**按需查询**的历史消息浏览器:
- 顶部筛选栏:频道 / 关键词 / 日期范围 / 收藏 / 有标签 / 置顶
- 中部表格:频道 · 时间 · 类型 · 摘要 · 标记(一次最多 500 条)
- 下部详情:选中行展示完整正文 + 标签 / 备注 / 媒体文件清单

查询复用 core 已有能力 `app.messages.list_messages(...)` —— search 子串匹配
text + media.file_name,favorite/tag/pinned 为 AND 语义,日期走 storage
date 字段;UI 只负责收集条件、渲染结果,不重复实现过滤逻辑。

跨边界只传 DTO(MessageDTO / ChannelDTO),core 不感知 Qt。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.dto import ChannelDTO, MessageDTO
from tgmonitor.ui.async_bridge import run_coro

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService

log = logging.getLogger(__name__)

# 单次查询上限 —— 消息详情页要显示摘要,500 条已足够人工浏览;
# 真要导全量走 core 的导出/分页能力,不在本页做无限加载。
_QUERY_LIMIT = 500

# 日期控件起始值(用当前日期,勾选后才生效,qss 上以 chk_date 控制显隐态)。
_DATE_PLACEHOLDER_YEAR = 2000


class MessageManagerWidget(QWidget):
    """消息管理页:筛选栏 + 结果表 + 详情。

    Signals:
        messages_loaded: payload = list[MessageDTO]。run_coro 的 on_success
            在测试环境可能落在后台 loop 线程 —— 只 emit signal,由 Qt 主线程
            的槽接手渲染(与 VM 的 message_received 同款跨线程协议)。
        query_failed: payload = 错误文案,同上。
        query_finished: 一次查询完成,payload = 返回条数(状态栏 / 测试用)。
    """

    messages_loaded = Signal(object)
    query_failed = Signal(str)
    query_finished = Signal(int)

    def __init__(
        self,
        app: AppService,
        loop,
        channels_provider: Callable[[], dict[int, ChannelDTO]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("messageManagerWidget")
        self._app = app
        self._loop = loop
        self._channels_provider = channels_provider
        self._rows: list[MessageDTO] = []
        self._channel_titles: dict[int, str] = {}

        self._build_ui()
        self._wire()

    # ---- 静态装配 ----

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        root.addLayout(self._build_filter_bar())

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = self._build_table()
        self.detail = QTextBrowser()
        self.detail.setObjectName("messageManagerDetail")
        self.detail.setPlaceholderText(self.tr("选中一行查看完整消息内容"))
        self.detail.setOpenExternalLinks(False)
        splitter.addWidget(self.table)
        splitter.addWidget(self.detail)
        splitter.setSizes([420, 180])
        root.addWidget(splitter, 1)

        self.lbl_status = QLabel(self.tr("尚未查询"))
        self.lbl_status.setObjectName("messageManagerStatus")
        root.addWidget(self.lbl_status)

    def _build_filter_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self.cmb_channel = QComboBox()
        self.cmb_channel.setMinimumWidth(150)
        self.cmb_channel.addItem(self.tr("全部频道"), None)
        bar.addWidget(self.cmb_channel)

        self.edit_search = QLineEdit()
        self.edit_search.setPlaceholderText(self.tr("搜索正文 / 文件名…"))
        self.edit_search.setClearButtonEnabled(True)
        self.edit_search.setMinimumWidth(180)
        bar.addWidget(self.edit_search, 1)

        self.chk_favorite = QCheckBox(self.tr("收藏"))
        self.chk_tagged = QCheckBox(self.tr("有标签"))
        self.chk_pinned = QCheckBox(self.tr("置顶"))
        bar.addWidget(self.chk_favorite)
        bar.addWidget(self.chk_tagged)
        bar.addWidget(self.chk_pinned)

        # 日期范围:默认不启用(两个 QDateEdit 禁用),勾选后参与筛选。
        self.chk_date = QCheckBox(self.tr("日期"))
        bar.addWidget(self.chk_date)
        self.date_from = QDateEdit()
        self.date_to = QDateEdit()
        for w in (self.date_from, self.date_to):
            w.setCalendarPopup(True)
            w.setDisplayFormat("yyyy-MM-dd")
            w.setEnabled(False)
            bar.addWidget(w)
        self.date_from.setDate(QDate(_DATE_PLACEHOLDER_YEAR, 1, 1))
        self.date_to.setDate(QDate.currentDate())

        self.btn_query = QPushButton(self.tr("查询"))
        self.btn_query.setObjectName("primaryBtn")
        bar.addWidget(self.btn_query)

        self.btn_reset = QPushButton(self.tr("重置"))
        bar.addWidget(self.btn_reset)
        return bar

    def _build_table(self) -> QTableWidget:
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(
            [
                self.tr("频道"),
                self.tr("时间"),
                self.tr("类型"),
                self.tr("摘要"),
                self.tr("标记"),
            ]
        )
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        header = table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        return table

    def _wire(self) -> None:
        self.btn_query.clicked.connect(self.refresh)
        self.btn_reset.clicked.connect(self.reset_filters)
        self.edit_search.returnPressed.connect(self.refresh)
        self.chk_date.toggled.connect(self.date_from.setEnabled)
        self.chk_date.toggled.connect(self.date_to.setEnabled)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        # 跨线程安全:run_coro 的回调只 emit,渲染在主线程槽内做
        self.messages_loaded.connect(self._apply_messages)
        self.query_failed.connect(self._on_query_failed)

    # ---- 对外入口 ----

    def refresh(self) -> None:
        """重新拉取消息 —— 导航切到本页 / 点「查询」时调用。"""
        self._sync_channel_combo()
        self.lbl_status.setText(self.tr("查询中…"))
        self.btn_query.setEnabled(False)

        channel_data = self.cmb_channel.currentData()
        channel_ids = [int(channel_data)] if channel_data is not None else None
        date_from, date_to = self._selected_dates()
        search = self.edit_search.text().strip()

        async def _go() -> list[MessageDTO]:
            return await self._app.messages.list_messages(
                channel_ids=channel_ids,
                date_from=date_from,
                date_to=date_to,
                limit=_QUERY_LIMIT,
                search=search,
                include_unmonitored=True,
                favorite_only=self.chk_favorite.isChecked(),
                tag_only=self.chk_tagged.isChecked(),
                pinned_only=self.chk_pinned.isChecked(),
            )

        def _done(msgs: list[MessageDTO]) -> None:
            # 只 emit —— Qt 会把 signal 排到 widget 所在主线程再执行槽。
            self.messages_loaded.emit(msgs)

        def _failed(exc: BaseException) -> None:
            log.warning("message manager query failed: %s", exc)
            self.query_failed.emit(self.tr(f"查询失败: {exc}"))

        run_coro(
            self._loop,
            _go(),
            on_success=_done,
            on_error=_failed,
            error_label="message_manager_query",
        )

    def reset_filters(self) -> None:
        """清空全部筛选并重新查询。"""
        self.cmb_channel.setCurrentIndex(0)
        self.edit_search.clear()
        self.chk_favorite.setChecked(False)
        self.chk_tagged.setChecked(False)
        self.chk_pinned.setChecked(False)
        self.chk_date.setChecked(False)
        self.refresh()

    # ---- 内部 ----

    def _sync_channel_combo(self) -> None:
        """从 VM 的 monitored_channels 刷新下拉项,保留当前选择。"""
        provider = self._channels_provider
        channels: dict[int, ChannelDTO] = provider() if provider is not None else {}
        self._channel_titles = {cid: ch.title for cid, ch in channels.items()}
        current = self.cmb_channel.currentData()
        self.cmb_channel.blockSignals(True)
        try:
            self.cmb_channel.clear()
            self.cmb_channel.addItem(self.tr("全部频道"), None)
            for cid in sorted(channels, key=lambda c: channels[c].title):
                self.cmb_channel.addItem(channels[cid].title, cid)
            if current is not None:
                idx = self.cmb_channel.findData(current)
                if idx >= 0:
                    self.cmb_channel.setCurrentIndex(idx)
        finally:
            self.cmb_channel.blockSignals(False)

    def _selected_dates(self) -> tuple[datetime | None, datetime | None]:
        if not self.chk_date.isChecked():
            return None, None
        d1 = self.date_from.date()
        d2 = self.date_to.date()
        # QDate.toPyDate() 在 PySide6 stub 里返回 object,直接用 year/month/day
        # 构造 naive datetime —— storage 的 _dt_to_db 对 naive 原样编码 ISO。
        start = datetime(d1.year(), d1.month(), d1.day(), 0, 0, 0)
        end = datetime(d2.year(), d2.month(), d2.day(), 23, 59, 59, 999999)
        return start, end

    def _on_query_failed(self, msg: str) -> None:
        self.btn_query.setEnabled(True)
        self.lbl_status.setText(msg)

    def _apply_messages(self, msgs: list[MessageDTO]) -> None:
        self._rows = list(msgs)
        self.table.setRowCount(len(self._rows))
        for row, msg in enumerate(self._rows):
            values = (
                self._channel_titles.get(msg.channel_id, str(msg.channel_id)),
                msg.date.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
                if msg.date.tzinfo is not None
                else msg.date.strftime("%Y-%m-%d %H:%M"),
                self._message_kind(msg),
                " ".join(msg.text.split())[:160],
                self._message_flags(msg),
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, col, item)
        self.table.clearSelection()
        self.detail.clear()
        self.btn_query.setEnabled(True)
        suffix = self.tr(f" · 上限 {_QUERY_LIMIT} 条") if len(self._rows) >= _QUERY_LIMIT else ""
        self.lbl_status.setText(self.tr(f"共 {len(self._rows)} 条消息{suffix}"))
        self.query_finished.emit(len(self._rows))

    def _on_selection_changed(self) -> None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return
        idx = rows[0].row()
        if 0 <= idx < len(self._rows):
            self.detail.setPlainText(self._format_detail(self._rows[idx]))

    @staticmethod
    def _message_kind(msg: MessageDTO) -> str:
        if not msg.media:
            return "文本"
        kinds = {m.type.value for m in msg.media}
        return ", ".join(sorted(kinds))

    @staticmethod
    def _message_flags(msg: MessageDTO) -> str:
        flags: list[str] = []
        if msg.is_favorite:
            flags.append("★")
        if msg.tags:
            flags.append(f"#{len(msg.tags)}")
        if msg.is_pinned:
            flags.append("置顶")
        return " ".join(flags)

    def _format_detail(self, msg: MessageDTO) -> str:
        channel = self._channel_titles.get(msg.channel_id, str(msg.channel_id))
        lines = [
            f"{self.tr('频道')}: {channel}",
            f"{self.tr('时间')}: {msg.date.isoformat(sep=' ', timespec='seconds')}",
            f"{self.tr('消息 ID')}: {msg.telegram_msg_id}",
            f"{self.tr('作者')}: {msg.author or '-'}",
            "",
            msg.text or self.tr("(无正文)"),
        ]
        if msg.tags:
            lines.extend(["", f"{self.tr('标签')}: {', '.join(msg.tags)}"])
        if msg.notes:
            lines.extend(["", f"{self.tr('备注')}: {msg.notes}"])
        if msg.media:
            lines.extend(["", self.tr("媒体:")])
            lines.extend(f"  - {m.file_name or m.type.value}" for m in msg.media)
        return "\n".join(lines)
