# mypy: disable-error-code="attr-defined"
"""ChannelWidget — 监听频道管理(设置页内嵌单实例)。

把"监听谁"这事最简化:
- **左栏「已加入频道」**:从 Telegram 现拉/库中已加入的频道,行尾 `+` = 加入监听
- **右栏「已监听」**:当前监听白名单(真理走 `storage.list_monitored_channels()`),
  行尾 `−` = 移出监听。2026-11-19 起不再有全量同步入口(移除)。

2026-11-19 v2.0.7 FR-06/FR-07:双栏由上下改左右;底部双击提示删除;
行文本不再展示 chat_id(仍存在 item 的 Qt.UserRole 供逻辑使用);
双击入口删除,统一走行尾图标按钮。

事件:`ChannelMonitored / ChannelUnmonitored` 会刷新右栏。
设计原则:开关监听是高频操作,应该是行内显式按钮,不是隐藏手势。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import (
    ChannelMonitored,
    ChannelPhotoChanged,
    ChannelTitleChanged,
    ChannelUnmonitored,
)
from tgmonitor.ui.async_bridge import run_coro
from tgmonitor.ui.icon import tinted_action_icon
from tgmonitor.ui.thumbnail import (
    ThumbnailCache,
    render_pixmap,
)
from tgmonitor.ui.widgets.form_row import empty_hint

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.events import Event, EventBus

log = logging.getLogger(__name__)


# 频道类型 → Lucide 图标名。
# 单色、currentColor 风格,与工具栏 UI 一致;row title 已带 title 文字,
# 单色图标不再承担"颜色编码"语义(megaphone / users / user-round 已经表意)。
_KIND_ICON_NAMES: dict[str, str] = {
    "channel": "kind_channel",
    "supergroup": "kind_supergroup",
    "group": "kind_group",
}

# 2026-09-04 v1.6.4:进程内 LRU 缩略图缓存 — 频道头像 + media 共用。
# `ThumbnailCache` 设计上接受 `(backend, object_key)`;频道头像来源是
# Telegram 本地缓存绝对路径,固定用 backend="local",key=Telegram 推过来的 path。
_CHANNEL_THUMB_BACKEND = "local"


def _thumbnail_cache() -> ThumbnailCache:
    """懒加载模块级 cache 单例。

    测试可通过 monkeypatch `tgmonitor.ui.widgets.channel_widget._channel_thumb_cache`
    注入 fixture;生产启动时延后到首次访问创建(避免 import 时 Qt 未就绪)。
    """
    global _channel_thumb_cache
    cache = globals().get("_channel_thumb_cache")
    if cache is None:
        cache = ThumbnailCache()
        globals()["_channel_thumb_cache"] = cache
    return cache


def _read_photo_bytes(path: str) -> bytes | None:
    """读 Telegram 本地缓存头像 bytes。失败(FileNotFoundError / PermissionError /
    非文件)返 None — 由 caller 走 kind placeholder。

    头像文件通常 ~5-50KB JPEG/PNG,同步读可接受(< 5ms SSD);fail-open 走
    fallback 不影响主流程。
    """
    try:
        return Path(path).read_bytes()
    except (OSError, ValueError):
        return None


def _kind_icon(
    kind: str,
    photo_local_key: str | None = None,
) -> QIcon:
    """频道 list item 图标 — 优先频道头像,失败回 kind emoji 占位。

    2026-09-04 v1.6.4:photo 路径走 `ThumbnailCache` LRU;cache miss 时
    同步读 `photo_local_key`(Telegram 本地缓存绝对路径)。IO 失败或格式
    不可解码 → fallback `_kind_emoji_icon(kind)`。

    未知 kind 一律 fallback 到 group(user-round)。
    """
    if photo_local_key:
        cache = _thumbnail_cache()
        pix = cache.get(_CHANNEL_THUMB_BACKEND, photo_local_key)
        if pix is None:
            data = _read_photo_bytes(photo_local_key)
            if data:
                pix = render_pixmap(data, max_size=48)
                if pix is not None and not pix.isNull():
                    cache.put(_CHANNEL_THUMB_BACKEND, photo_local_key, pix)
        if pix is not None and not pix.isNull():
            return QIcon(pix)
    return _kind_emoji_icon(kind)


def _kind_emoji_icon(kind: str) -> QIcon:
    """频道类型 fallback 图标 —— Lucide 单色(2026-08-27 PR #Q2)。

    fg 固定浅色 #1a1a2e。Qt 的 QSvgRenderer 不解析 currentColor,
    所以走 tinted_action_icon 显式注入 hex,避免图标渲染成黑团。
    """
    fg = "#1a1a2e"
    return tinted_action_icon(
        _KIND_ICON_NAMES.get(kind, _KIND_ICON_NAMES["group"]),
        QColor(fg),
    )


def _channel_display_text(ch: ChannelDTO) -> str:
    """频道列表行文本 — **频道名称为主、username 为辅**。

    格式:`title  @username`(有 username)/ `title`(无 username)。
    2026-11-19 v2.0.7 FR-07:chat_id 不再展示(行尾与 tooltip 都没有),
    但仍存在 item 的 `Qt.UserRole` 中供逻辑使用(同名频道靠它区分)。
    末尾附加 spammer 徽标:`is_verified` → ` ✓`,`is_scam`/`is_fake` → ` [风险]`。
    """
    title = ch.title or "(无标题)"
    base = f"{title}  @{ch.username}" if ch.username else title
    if ch.is_verified:
        base += " ✓"
    if ch.is_scam or ch.is_fake:
        base += " [风险]"
    return base


class ChannelListCard(QWidget):
    """监听管理的左右两栏之一 — 标题 + (可选) action + list + 行尾图标按钮。

    两张卡的同构部件抽在这里,只在 __init__ 参数里区分:
      - title / action 按钮标签 / action tooltip / show_action(右栏无按钮)
      - 是否多选(extended_selection;两栏均 single)
      - 是否带 empty_hint(左栏需要;右栏没"空监听白名单"歧义场景)
      - `row_action_icon`("plus"=加入监听 / "minus"=移出监听)

    2026-11-19 v2.0.7 FR-07:双击入口与底部提示删除,改为每行右侧图标按钮。
    每行用 `setItemWidget` 挂一个水平 QWidget(名称 Label + QToolButton);
    同时仍给 QListWidgetItem.setText,保留行文本契约与过滤逻辑。

    Public API 给 ChannelWidget 用:
      - `set_items(channels, *, count_template)` — 装载频道列表 + 更新标题
      - `clear_items()` — 清空 + 重新显示 empty_hint(如果配置了)
      - `add_item(channel)` / `remove_by_cid(cid)` / `find_cid(cid)` — 增量更新
      - `apply_filter(text, joined_mapping)` — 按 text 过滤可见
      - `set_row_actions_enabled(enabled)` — 门控行尾按钮
      - 属性:`lst`、`btn_action`(`QPushButton | None`)、`count_label`

    Signals:
      - `item_action_clicked(qlonglong)` — 行尾按钮点击,携带 channel_id
      - `action_clicked()` — action 按钮被点(仅 show_action=True 的卡)
    """

    # Telegram chat_id 是 64 位带符号整数(如 -1001375475051),`Signal(int)`
    # 映射到 C++ 32 位 int,emit 会 shiboken Overflow 且 slot 派发失败;
    # 必须用 64 位 C++ 类型 `qlonglong`。
    item_action_clicked = Signal("qlonglong")  # type: ignore[arg-type]  # PySide6 字符串签名,stub 不认
    action_clicked = Signal()

    def __init__(
        self,
        *,
        title: str,
        action_label: str,
        action_tooltip: str = "",
        extended_selection: bool = False,
        empty_hint_spec: tuple[str, str, str] | None = None,
        row_action_icon: str = "",
        show_action: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("channelCard")
        self._title = title
        self._empty_hint_spec = empty_hint_spec
        self._row_action_icon = row_action_icon

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # 标题行 — title label + 弹性间隔 + action button(可关闭)
        head = QHBoxLayout()
        self.count_label = QLabel(title)
        self.count_label.setObjectName("cardSectionTitle")
        head.addWidget(self.count_label)
        head.addStretch(1)
        # 2026-11-19:下栏「已监听」不再有全量同步入口 → show_action=False
        # 时不建按钮。btn_action 类型 `QPushButton | None`。
        self.btn_action: QPushButton | None = None
        if show_action:
            self.btn_action = QPushButton(action_label)
            if action_tooltip:
                self.btn_action.setToolTip(action_tooltip)
            head.addWidget(self.btn_action)
        root.addLayout(head)

        # 列表 + 空态占位 — 2026-11-19 一起放进 QStackedWidget,让「暂无已加入
        # 频道」显示在**卡片框内**（与列表同一区域），而不是原来那样作为列表
        # 下方的独立 widget 显示在框外。QStackedWidget 非当前页 `isHidden()`
        # 为 True，与既有 `_empty_hint.isHidden()` 契约断言语义一致。
        self.lst = QListWidget()
        self.lst.setAlternatingRowColors(True)
        if extended_selection:
            # 多选模式预留（当前两栏均 single；保留参数供未来扩展）
            self.lst.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._stack = QStackedWidget()
        self._stack.addWidget(self.lst)  # index 0 — 有数据
        root.addWidget(self._stack, 1)

        # empty_hint(可选)— 叠在列表区域(index 1),空时切入。
        self._empty_hint: QWidget | None = None
        if empty_hint_spec is not None:
            icon_e, title_e, hint_e = empty_hint_spec
            self._empty_hint = empty_hint(icon_e, title_e, hint_e)
            self._stack.addWidget(self._empty_hint)  # index 1 — 空态

        # v2.0.7 FR-06:底部双击行为提示删除 —— 操作入口已改为行尾图标按钮。

        # 信号
        if self.btn_action is not None:
            self.btn_action.clicked.connect(self.action_clicked)

        # 初始态：列表为空 → 切到空态页（配置了 hint 时）。
        self._refresh_empty_state()

    # ---- 数据装载 ----

    def set_items(self, channels: list[ChannelDTO], *, count_template: str) -> None:
        """装载频道 + 设置标题右侧 count(format 用 `{n}` 占位)。

        `count_template`:例如 `"已加入频道 · {n}"`、`"已监听 · {n}"`、
        `"已监听:{n}"`。模板只接受一个整数占位。

        2026-09-04 v1.6.4:`_kind_icon(kind, photo_local_key)` 优先渲染频道
        头像;`_channel_display_text(ch)` 附加 verified/scam 徽标。
        """
        self._clear_rows()
        for ch in sorted(channels, key=lambda c: (c.title or "").lower()):
            self._add_row(ch)
        self.count_label.setText(count_template.format(n=len(channels)))
        self._refresh_empty_state()

    def clear_items(self, *, count_template: str) -> None:
        """清空 + 显示 empty_hint(如果配置了)。"""
        self._clear_rows()
        self.count_label.setText(count_template.format(n=0))
        self._refresh_empty_state()

    def add_item(self, ch: ChannelDTO) -> None:
        """监听事件时增量追加一条。注意:不复检是否重复(ChannelWidget 已用
        `_monitored_ids` set 防重)。
        """
        self._add_row(ch)
        self._refresh_empty_state()

    def remove_by_cid(self, channel_id: int) -> bool:
        """按 channel_id 移除一行(取消监听事件)。找不到返 False。"""
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            if it is not None and it.data(Qt.UserRole) == channel_id:
                widget = self.lst.itemWidget(it)
                self.lst.takeItem(i)
                if widget is not None:
                    widget.deleteLater()
                self._refresh_empty_state()
                return True
        return False

    def find_cid(self, channel_id: int) -> int:
        """找不到返 -1。"""
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            if it is not None and it.data(Qt.UserRole) == channel_id:
                return i
        return -1

    def apply_filter(self, text: str, mapping: dict[int, ChannelDTO]) -> None:
        """按 text 过滤行的可见性 — mapping 通常是 `ChannelWidget._joined`。
        空 text = 显示全部。
        """
        text = text.strip().lower()
        for i in range(self.lst.count()):
            item = self.lst.item(i)
            if item is None:
                continue
            cid = item.data(Qt.UserRole)
            ch = mapping.get(int(cid)) if cid is not None else None
            hidden = bool(text) and (
                ch is None
                or text not in ((ch.title or "").lower() + " " + (ch.username or "").lower())
            )
            item.setHidden(hidden)
            # setItemWidget 的行不会随 item.setHidden 自动隐藏,须显式同步。
            widget = self.lst.itemWidget(item)
            if widget is not None:
                widget.setHidden(hidden)

    def set_row_actions_enabled(self, enabled: bool) -> None:
        """门控行尾加减按钮(布局层;core require_ready 才是安全边界)。"""
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            widget = self.lst.itemWidget(it) if it is not None else None
            if widget is None:
                continue
            btn = widget.findChild(QToolButton, "channelRowAction")
            if btn is not None:
                btn.setEnabled(enabled)

    # ---- helpers ----

    def _clear_rows(self) -> None:
        """清空列表,释放挂在各 item 上的行 widget(避免 viewport 残留)。"""
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            widget = self.lst.itemWidget(it) if it is not None else None
            if widget is not None:
                widget.deleteLater()
        self.lst.clear()

    def _add_row(self, ch: ChannelDTO) -> None:
        """建一行:item 仍 setText(保留文本契约),widget 提供行尾图标按钮。"""
        text = _channel_display_text(ch)
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, ch.id)
        item.setIcon(_kind_icon(ch.kind, ch.photo_local_key))
        item.setSizeHint(QSize(20, 34))
        self.lst.addItem(item)
        if not self._row_action_icon:
            return

        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(4, 2, 4, 2)
        row_lay.setSpacing(4)
        name = QLabel(text)
        name.setObjectName("channelRowName")
        name.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        row_lay.addWidget(name)

        btn = QToolButton()
        btn.setObjectName("channelRowAction")
        btn.setIcon(tinted_action_icon(self._row_action_icon, QColor("#5b9cf5")))
        btn.setToolTip("加入监听" if self._row_action_icon == "plus" else "移出监听")
        btn.setCursor(Qt.PointingHandCursor)
        cid = int(ch.id)
        # 默认参数捕获 cid:避开 late-binding;int 经 qlonglong 信号保留 64 位。
        btn.clicked.connect(lambda _checked=False, cid=cid: self.item_action_clicked.emit(cid))
        row_lay.addWidget(btn)
        self.lst.setItemWidget(item, row)

    def _set_row_text(self, item: QListWidgetItem, text: str) -> None:
        """item 与挂件内label 同步文本(事件驱动标题变更时两处都要更新)。"""
        item.setText(text)
        widget = self.lst.itemWidget(item)
        if widget is None:
            return
        name = widget.findChild(QLabel, "channelRowName")
        if name is not None:
            name.setText(text)

    def _refresh_empty_state(self) -> None:
        """list count == 0 + 配置了 empty_hint → 切到空态页;否则切回列表页。

        2026-11-19:lst 与 empty_hint 同处 QStackedWidget,空态显示在卡片框内。
        非当前页 `isHidden()` 为 True,保持既有可见性契约。
        """
        if self._empty_hint is not None:
            self._stack.setCurrentIndex(1 if self.lst.count() == 0 else 0)


class ChannelWidget(QWidget):
    """监听频道管理 — 左右两栏 + 搜索过滤。

    左「已加入频道」:行尾 `+` = 加入监听白名单;
    右「已监听」:行尾 `−` = 移出监听。

    内部用两张 `ChannelListCard` 实例(`joined_card` / `subs_card`)
    实际承载 UI,ChannelWidget 自身只负责装配 + 搜索 + EventBus 接线和
    增量子操作。
    """

    # 异步拉频道后 → 主线程刷新 list
    joined_loaded = Signal(list)
    # 2026-11-19:删除全量同步入口后 sync_requested 信号一并移除。

    def __init__(
        self,
        app: AppService,
        loop: asyncio.AbstractEventLoop,
        parent: QWidget | None = None,
    ) -> None:
        """建两张 `ChannelListCard`(joined / subscribed)+ 接 EventBus + 异步拉频道。"""
        super().__init__(parent)
        self.app = app
        self.loop = loop
        self._joined: dict[int, ChannelDTO] = {}
        self._monitored_ids: set[int] = set()
        self._build()
        self._wire_bus()
        # 异步拉频道 → 主线程刷新
        self.joined_loaded.connect(self._apply_joined)
        # 2026-11-19:进入设置先读库展示已加入频道(tg_subscribed=True),
        # 不再空白等用户点「同步」。TG 拉取 + 落库只在用户点「同步」时发生。
        self._bootstrap_joined_from_storage()

    def _bootstrap_joined_from_storage(self) -> None:
        """构造后异步读库填充左栏 — 只读,不连 TG、不改库。"""
        run_coro(
            self.loop,
            self.app.channels.list_tg_subscribed_channels(),
            error_label="bootstrap_joined_channels",
            on_success=lambda chs: self.joined_loaded.emit(list(chs)),
        )

    # ---- UI ----

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        # 顶部搜索框
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索频道名 / username…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        root.addWidget(self.search_edit)

        # v2.0.7 FR-06:左右两栏同处一个 QHBox;搜索框仍独占顶部一行。
        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)

        # 左栏 — 已加入(`ChannelListCard`,已接入 empty_hint #2-6)
        self.joined_card = ChannelListCard(
            title="已加入频道",
            action_label="同步",
            action_tooltip="从 Telegram 拉取当前账号加入的全部频道/群组并保存到本地",
            extended_selection=False,
            empty_hint_spec=(
                "≡",
                "暂无已加入频道",
                "请先到「设置 → 账户」登录 Telegram 账号,\n登录成功后点「同步」拉取频道。",
            ),
            row_action_icon="plus",
        )
        self.joined_card.action_clicked.connect(self._on_refresh)
        self.joined_card.item_action_clicked.connect(self._on_joined_action)
        cards_row.addWidget(self.joined_card, 1)

        # 右栏 — 已监听（2026-11-19:全量同步入口删除，无 action 按钮）
        self.subs_card = ChannelListCard(
            title="已监听",
            action_label="",
            extended_selection=False,
            empty_hint_spec=None,  # 已监听「空」语义不明,不放 empty_hint
            row_action_icon="minus",
            show_action=False,
        )
        self.subs_card.item_action_clicked.connect(self._on_monitored_action)
        cards_row.addWidget(self.subs_card, 1)

        root.addLayout(cards_row, 1)

        # 构造即初始化两栏计数为 0(如「已加入频道 · 0」),与 _refresh_state
        # 解耦 —— 左栏此后只由「刷新」按钮的 set_joined 驱动,右栏由监听
        # 状态驱动。不初始化会停在无计数的标题文本(title 本身)。
        self.joined_card.clear_items(count_template="已加入频道 · {n}")
        self.subs_card.clear_items(count_template="已监听 · {n}")

        # ---- 公开只读句柄 ----
        # 上层(MainWindow / UIAssembler)与契约测试通过这些语义明确的属性访问,
        # 卡片内部实现仍由 ChannelListCard 封装。
        self.joined_list = self.joined_card.lst
        self.monitored_list = self.subs_card.lst
        self.joined_count_label = self.joined_card.count_label
        self.monitored_count_label = self.subs_card.count_label
        self.refresh_button = self.joined_card.btn_action
        self._empty_joined: QWidget | None = self.joined_card._empty_hint

    def _apply_filter(self, text: str) -> None:
        """搜索过滤 — 两张卡各跑一遍 (用 self._joined 字典查 title)。"""
        self.joined_card.apply_filter(text, self._joined)
        self.subs_card.apply_filter(text, self._joined)

    def set_operations_enabled(self, enabled: bool) -> None:
        """v2.0.3 D7:按 core 门控状态适配依赖 TG 的操作入口(体验层)。

        **只做呈现,不重复判定规则** —— 判定在 core `ops_gate.is_ready`,
        本方法由 `AppStateController` 收到 `LoginStateChanged` 后调用。
        真正的执行安全由 core 域服务入口门控保证(UI 禁用只是体验,不是安全边界):
        行尾按钮即便被编程式触发,core 门控仍会一致拒绝。
        """
        tip = "" if enabled else "需先登录 Telegram 才能同步频道"
        if self.refresh_button is not None:
            self.refresh_button.setEnabled(enabled)
            self.refresh_button.setToolTip(tip)
        # 列表禁用会连带禁掉行按钮;再显式遍历加固(过滤隐藏时仍生效)。
        self.joined_card.lst.setEnabled(enabled)
        self.subs_card.lst.setEnabled(enabled)
        self.joined_card.set_row_actions_enabled(enabled)
        self.subs_card.set_row_actions_enabled(enabled)

    # ---- 数据装载 ----

    def set_joined(self, channels: list[ChannelDTO]) -> None:
        """外部装入已加入频道(DTO 列表)— 由 `MainWindow._refresh_state` 调。"""
        self._joined = {c.id: c for c in channels}
        self.joined_card.set_items(channels, count_template="已加入频道 · {n}")
        # 触发过滤(数据变了)
        self._apply_filter(self.search_edit.text())

    def set_monitored(self, channels: list[ChannelDTO]) -> None:
        """外部装入已监听频道(白名单)— 由 `MainWindow._refresh_state` 调。"""
        self._monitored_ids = {c.id for c in channels}
        self.subs_card.set_items(channels, count_template="已监听 · {n}")
        # 触发过滤
        self._apply_filter(self.search_edit.text())

    def merge_joined(self, channels: list[ChannelDTO]) -> None:
        """合并 — 拉刷新时不全清空,只追加新频道(更柔和)。"""
        new = {c.id: c for c in channels}
        new.update(self._joined)
        self.set_joined(list(new.values()))

    # ---- event bus ----

    def _wire_bus(self) -> None:
        bus: EventBus = self.app.bus

        async def _on(e: Event) -> None:
            if isinstance(e, ChannelMonitored) and e.channel is not None:
                self._add_to_monitored_list(e.channel)
            elif isinstance(e, ChannelUnmonitored):
                self._remove_from_monitored_list(e.channel_id)
            elif isinstance(e, ChannelTitleChanged):
                # 2026-09-03 v1.6.0 PR #Q2:实时刷新卡片标题。
                # 不重启应用 = 数据 stale 的问题修掉。
                self._apply_title_changed(e.channel_id, e.new_title)
            elif isinstance(e, ChannelPhotoChanged):
                # 2026-09-03 v1.6.0 PR #Q2:实时刷新头像(local 路径可能不可访问,
                # 由 ChannelListCard 兜底 placeholder)。
                self._apply_photo_changed(e.channel_id, e.local_path)

        bus.subscribe(ChannelMonitored, _on)
        bus.subscribe(ChannelUnmonitored, _on)
        bus.subscribe(ChannelTitleChanged, _on)
        bus.subscribe(ChannelPhotoChanged, _on)

    def _apply_title_changed(self, channel_id: int, new_title: str) -> None:
        """2026-09-03 v1.6.0 PR #Q2:更新 _joined 缓存 + 重建两张卡上对应 item 的
        display 文本。2026-09-04 v1.6.4:用 `_channel_display_text` 保留徽标后缀。
        """
        ch = self._joined.get(channel_id)
        if ch is None:
            return  # 频道不在 joined 卡,可能未刷新过
        ch.title = new_title
        # 重建 item 文本(简单路径 — QListWidgetItem.setText)
        for card in (self.joined_card, self.subs_card):
            for i in range(card.lst.count()):
                it = card.lst.item(i)
                if it and it.data(Qt.UserRole) == channel_id:
                    card._set_row_text(it, _channel_display_text(ch))

    def _apply_photo_changed(self, channel_id: int, local_path: str | None) -> None:
        """2026-09-03 v1.6.0 PR #Q2 stub → 2026-09-04 v1.6.4 实装:
        实时刷新两个卡上对应 item 的图标。

        - `local_path` 非 None → `_kind_icon(kind, local_path)` 走
          `ThumbnailCache` LRU;cache miss 时同步读 Telegram 本地缓存文件
          并 `render_pixmap(bytes, max_size=48)`;失败(IO / 非图格式)→
          fallback `_kind_emoji_icon(kind)`。
        - `local_path` 为 None → 头像被删,退回 emoji 占位。
        """
        ch = self._joined.get(channel_id)
        if ch is None:
            return
        ch.photo_local_key = local_path
        icon = _kind_icon(ch.kind, local_path)
        for card in (self.joined_card, self.subs_card):
            for i in range(card.lst.count()):
                it = card.lst.item(i)
                if it and it.data(Qt.UserRole) == channel_id:
                    it.setIcon(icon)

    def _add_to_monitored_list(self, ch: ChannelDTO) -> None:
        if ch.id in self._monitored_ids:
            return
        # 也写入 joined(以防 joined 还没刷新)
        self._joined[ch.id] = ch
        self._monitored_ids.add(ch.id)
        self.subs_card.add_item(ch)
        self.subs_card.count_label.setText(f"已监听:{len(self._monitored_ids)}")

    def _remove_from_monitored_list(self, channel_id: int) -> None:
        self._monitored_ids.discard(channel_id)
        self.subs_card.remove_by_cid(channel_id)
        self.subs_card.count_label.setText(f"已监听:{len(self._monitored_ids)}")

    # ---- 槽 ----

    def _on_refresh(self) -> None:
        async def _go() -> None:
            # v2.0.3:刷新同时落库校正 TG 订阅状态(写 tg_subscribed + 退订级联停监听)。
            # 未登录/空返回时 service 整轮跳过并返回 [],UI 展示空列表但不动库。
            chs = await self.app.channels.refresh_joined_channels()
            # 用 Signal 而非 QMetaObject.invokeMethod —— 后者把 Python list 经
            # Qt 元对象系统转 C++ 会 "Cannot copy-convert (list) to C++"。
            # Signal.emit 在 qasync 里跨 loop iteration 自然 queued,语义等价。
            self.joined_loaded.emit(chs)

        run_coro(self.loop, _go(), error_label="refresh_joined")

    def _apply_joined(self, chs: list[ChannelDTO]) -> None:
        self.set_joined(chs)

    def _on_joined_action(self, cid: int) -> None:
        """左栏行尾 `+` → 加入监听(core 调用与旧双击槽一致)。"""
        ch = self._joined.get(cid)
        if ch is None:
            return
        if cid in self._monitored_ids:
            return  # 已监听时按钮无效(改在右栏点 `−` 移出)
        run_coro(
            self.loop,
            self.app.channels.monitor_channel(ch),
            error_label="monitor_channel",
        )

    def _on_monitored_action(self, cid: int) -> None:
        """右栏行尾 `−` → 移出监听。"""
        run_coro(
            self.loop,
            self.app.channels.unmonitor_channel(int(cid)),
            error_label="unmonitor_channel",
        )
