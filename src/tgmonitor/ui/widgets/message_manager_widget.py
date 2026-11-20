# mypy: disable-error-code="attr-defined"
"""MessageManagerWidget — v2.0.8 新增「消息」导航页:查看 + 筛选已落库消息。

与大实时流(`MessageView`)不同,本页是**按需查询**的历史消息浏览器:
- 顶部筛选栏:频道 / 关键词 / 日期范围(预设 + 自定义)
- 中部表格:频道 · 时间 · 类型 · 摘要(全量落库消息,本地分页)
- 底部:上一页 / 页码 / 下一页 + 详情区(完整正文 + 标签 / 备注 / 媒体)

v2.0.9 二次修正:
  - 移除收藏 / 有标签 / 置顶 3 个筛选 —— 用户判定无实际意义,core 侧能力保留;
  - 日期由「勾选框 + 两个裸 QDateEdit」改为预设下拉(全部/今天/近7天/近30天/
    自定义),选中「自定义」才显示两个日期框;
  - 删除无实际意义的「标记」列;查询/重置按钮统一尺寸;
  - 去掉 500 条上限(改为全量查询 + 本地分页),可翻看全部消息。

查询复用 core 已有能力 `app.messages.list_messages(...)` —— UI 只负责收集
条件、渲染结果,不重复实现过滤逻辑。跨边界只传 DTO,core 不感知 Qt。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
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

# 本地分页大小 —— core 一次性返回全部候选消息,UI 按页渲染。
_PAGE_SIZE = 50

# 日期预设(data 存字符串,「自定义」时才启用两个 QDateEdit)。
_DATE_PRESETS = (
    ("全部时间", "all"),
    ("今天", "today"),
    ("近 7 天", "7d"),
    ("近 30 天", "30d"),
    ("自定义…", "custom"),
)


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
        self._rows: list[MessageDTO] = []  # 当前页要渲染的行
        self._all_rows: list[MessageDTO] = []  # 全量查询结果(分页源)
        self._page = 0  # 0-based
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

        # 分页栏 + 状态 —— 与媒体管理页交互一致:上一页 / 页码 / 下一页。
        pager = QHBoxLayout()
        pager.setSpacing(8)
        self.btn_prev = QPushButton(self.tr("‹ 上一页"))
        self.btn_next = QPushButton(self.tr("下一页 ›"))
        self.lbl_page = QLabel("1 / 1")
        self.lbl_page.setObjectName("messageManagerPage")
        pager.addStretch(1)
        pager.addWidget(self.btn_prev)
        pager.addWidget(self.lbl_page)
        pager.addWidget(self.btn_next)
        root.addLayout(pager)

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

        # v2.0.9:日期预设下拉 —— 常用区间一键选,只有「自定义…」才露日期框。
        self.cmb_date = QComboBox()
        self.cmb_date.setObjectName("messageManagerDate")
        for label, data in _DATE_PRESETS:
            self.cmb_date.addItem(self.tr(label), data)
        bar.addWidget(self.cmb_date)

        self.date_from = QDateEdit()
        self.date_to = QDateEdit()
        for w in (self.date_from, self.date_to):
            w.setCalendarPopup(True)
            w.setDisplayFormat("yyyy-MM-dd")
            w.hide()  # 默认隐藏,cmb_date 选「自定义…」才显示
            bar.addWidget(w)
        self.date_from.setDate(QDate.currentDate().addDays(-30))
        self.date_to.setDate(QDate.currentDate())

        # v2.0.9:查询/重置统一步调 —— 同样 76×30、同样的通用按钮样式。
        self.btn_query = QPushButton(self.tr("查询"))
        self.btn_query.setObjectName("primaryBtn")
        self.btn_reset = QPushButton(self.tr("重置"))
        # 34px 高容纳 primaryBtn 的 8px 上下 padding(13px 字体),避免文字被压。
        for btn in (self.btn_query, self.btn_reset):
            btn.setFixedSize(76, 34)
            bar.addWidget(btn)
        return bar

    def _build_table(self) -> QTableWidget:
        table = QTableWidget(0, 4)  # v2.0.9:删「标记」列
        table.setHorizontalHeaderLabels(
            [
                self.tr("频道"),
                self.tr("时间"),
                self.tr("类型"),
                self.tr("摘要"),
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
        return table

    def _wire(self) -> None:
        self.btn_query.clicked.connect(self.refresh)
        self.btn_reset.clicked.connect(self.reset_filters)
        self.edit_search.returnPressed.connect(self.refresh)
        self.cmb_date.currentIndexChanged.connect(self._on_date_preset_changed)
        self.btn_prev.clicked.connect(self._on_page_prev)
        self.btn_next.clicked.connect(self._on_page_next)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        # 跨线程安全:run_coro 的回调只 emit,渲染在主线程槽内做
        self.messages_loaded.connect(self._apply_messages)
        self.query_failed.connect(self._on_query_failed)

    # ---- 对外入口 ----

    def refresh(self) -> None:
        """重新拉取全部匹配消息 —— 导航切到本页 / 点「查询」时调用。"""
        self._sync_channel_combo()
        self.lbl_status.setText(self.tr("查询中…"))
        self.btn_query.setEnabled(False)

        channel_data = self.cmb_channel.currentData()
        channel_ids = [int(channel_data)] if channel_data is not None else None
        date_from, date_to = self._selected_dates()
        search = self.edit_search.text().strip()

        async def _go() -> list[MessageDTO]:
            # v2.0.9:limit=None 取全部候选,UI 本地分页 —— 可翻看全部消息。
            return await self._app.messages.list_messages(
                channel_ids=channel_ids,
                date_from=date_from,
                date_to=date_to,
                limit=None,
                search=search,
                include_unmonitored=True,
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
        self.cmb_date.setCurrentIndex(0)  # 全部时间
        self._page = 0
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

    def _on_date_preset_changed(self) -> None:
        """日期预设切换 —— 只有「自定义…」才显示两个日期框。"""
        custom = self.cmb_date.currentData() == "custom"
        self.date_from.setVisible(custom)
        self.date_to.setVisible(custom)
        self._page = 0

    def _selected_dates(self) -> tuple[datetime | None, datetime | None]:
        """日期预设 → (起, 止) naive datetime,None 表示不过滤。"""
        preset = self.cmb_date.currentData()
        if preset == "all" or preset is None:
            return None, None
        today = datetime.now()
        if preset == "today":
            start = today.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1) - timedelta(microseconds=1)
            return start, end
        if preset in ("7d", "30d"):
            days = 7 if preset == "7d" else 30
            start = (today - timedelta(days=days - 1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            end = today.replace(hour=23, minute=59, second=59, microsecond=999999)
            return start, end
        # 自定义:QDate 的 year/month/day 构造 naive datetime —— storage
        # 的 _dt_to_db 对 naive 原样编码 ISO。
        d1 = self.date_from.date()
        d2 = self.date_to.date()
        start = datetime(d1.year(), d1.month(), d1.day(), 0, 0, 0)
        end = datetime(d2.year(), d2.month(), d2.day(), 23, 59, 59, 999999)
        return start, end

    # ---- 分页 ----

    def _total_pages(self) -> int:
        return max(1, (len(self._all_rows) + _PAGE_SIZE - 1) // _PAGE_SIZE)

    def _render_page(self) -> None:
        """按 _page 把 _all_rows 的当前页切片渲染到表格。"""
        total_pages = self._total_pages()
        if self._page >= total_pages:
            self._page = max(0, total_pages - 1)
        start = self._page * _PAGE_SIZE
        self._rows = self._all_rows[start : start + _PAGE_SIZE]

        self.table.setRowCount(len(self._rows))
        for row, msg in enumerate(self._rows):
            values = (
                self._channel_titles.get(msg.channel_id, str(msg.channel_id)),
                msg.date.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
                if msg.date.tzinfo is not None
                else msg.date.strftime("%Y-%m-%d %H:%M"),
                self._message_kind(msg),
                " ".join(msg.text.split())[:160],
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, col, item)
        self.table.clearSelection()
        self.detail.clear()

        self.lbl_page.setText(f"{self._page + 1} / {total_pages}")
        self.btn_prev.setEnabled(self._page > 0)
        self.btn_next.setEnabled(self._page + 1 < total_pages)
        total = len(self._all_rows)
        self.lbl_status.setText(self.tr(f"共 {total} 条消息 · 每页 {_PAGE_SIZE} 条"))
        self.query_finished.emit(len(self._rows))

    def _on_page_prev(self) -> None:
        if self._page > 0:
            self._page -= 1
            self._render_page()

    def _on_page_next(self) -> None:
        if self._page + 1 < self._total_pages():
            self._page += 1
            self._render_page()

    def _on_query_failed(self, msg: str) -> None:
        self.btn_query.setEnabled(True)
        self.lbl_status.setText(msg)

    def _apply_messages(self, msgs: list[MessageDTO]) -> None:
        """查询完成(主线程槽)—— 存全量结果,重置到第 1 页渲染。"""
        self._all_rows = list(msgs)
        self._page = 0
        self.btn_query.setEnabled(True)
        self._render_page()

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
