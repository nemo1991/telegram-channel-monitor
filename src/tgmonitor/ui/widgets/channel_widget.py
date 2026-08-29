# mypy: disable-error-code="attr-defined"
"""ChannelWidget — 主窗口侧栏下半部。

把"监听谁"这事最简化:
- **上半栏「全部(已加入)」**:从 Telegram 现拉的全部频道/群组,**双击 = 订阅**
- **下半栏「已监听」**:`AppService._subscribed` 当前白名单,**多选 + 全量同步…**

事件:`ChannelSubscribed / ChannelUnsubscribed` 会刷新下半栏。
设计原则:订阅是高频操作,不应该藏在工具栏「刷新频道」里然后一揽子全量订。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import ChannelSubscribed, ChannelUnsubscribed
from tgmonitor.ui._async import run_coro
from tgmonitor.ui.icon import tinted_action_icon
from tgmonitor.ui.theme import Theme, ThemeManager
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


def _kind_icon(kind: str) -> QIcon:
    """频道类型图标 —— Lucide 单色,见 `ATTRIBUTIONS.md` 与 `ui/icon.py`。

    fg 跟当前主题:QListWidget 在 light 主题用 #1a1a2e,dark 主题用 #f0f1fa。
    Qt 的 QSvgRenderer 不解析 currentColor,所以走 tinted_action_icon
    显式注入 hex,避免图标在 list row 上渲染成黑团。

    未知 kind 一律 fallback 到 group(user-round)。
    """
    fg = "#f0f1fa" if ThemeManager.current() == Theme.DARK else "#1a1a2e"
    return tinted_action_icon(
        _KIND_ICON_NAMES.get(kind, _KIND_ICON_NAMES["group"]),
        QColor(fg),
    )


class _ChannelListCard(QWidget):
    """频道管理页的双栏之一 — 标题 + action button + list + 底部 hint。

    把"已加入"和"已监听"两张卡的同构部件抽出来(原本是 ChannelWidget._build
    里两段各 ~30 行的复制粘贴),只在 __init__ 参数里区分:
      - title / action 按钮标签 / action tooltip
      - 是否多选(已监听需要,已加入 single)
      - 是否带 empty_hint(已加入需要;已监听没"空监听白名单"歧义场景)
      - bottom_hint(双击行为提示)

    Public API 给 ChannelWidget 用:
      - `set_items(channels, *, count_template)` — 装载频道列表 + 更新标题
      - `clear_items()` — 清空 + 重新显示 empty_hint(如果配置了)
      - `add_item(channel)` / `remove_by_cid(cid)` / `find_cid(cid)` —
        增量更新(订阅/退订事件驱动)
      - `apply_filter(text, joined_mapping)` — 按 text 过滤可见
      - `selected_cids()` — 多选模式:取当前选中行的 id 列表
      - 属性:`lst`(QListWidget)、`btn_action`、`count_label`

    Signals:
      - `item_double_clicked(qlonglong)` — channel_id
      - `action_clicked()` — action 按钮被点
    """

    # Telegram chat_id 是 64 位带符号整数(如 -1001375475051),`Signal(int)`
    # 映射到 C++ 32 位 int,emit 会 shiboken Overflow 且 slot 派发失败;
    # 必须用 64 位 C++ 类型 `qlonglong`。
    item_double_clicked = Signal("qlonglong")  # type: ignore[arg-type]  # PySide6 字符串签名,stub 不认
    action_clicked = Signal()

    def __init__(
        self,
        *,
        title: str,
        action_label: str,
        action_tooltip: str = "",
        extended_selection: bool = False,
        empty_hint_spec: tuple[str, str, str] | None = None,
        bottom_hint: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("channelCard")
        self._title = title
        self._empty_hint_spec = empty_hint_spec

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # 标题行 — title label + 弹性间隔 + action button
        head = QHBoxLayout()
        self.count_label = QLabel(title)
        self.count_label.setObjectName("cardSectionTitle")
        head.addWidget(self.count_label)
        head.addStretch(1)
        self.btn_action = QPushButton(action_label)
        if action_tooltip:
            self.btn_action.setToolTip(action_tooltip)
        head.addWidget(self.btn_action)
        root.addLayout(head)

        # 列表
        self.lst = QListWidget()
        self.lst.setAlternatingRowColors(True)
        if extended_selection:
            # 多选 — Ctrl/Shift 多选 + 全选(Ctrl+A)用于批量 sync
            self.lst.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.lst.itemDoubleClicked.connect(self._on_lst_double_clicked)
        root.addWidget(self.lst)

        # empty_hint(可选)— 类型 `QWidget | None`,mypy 在 `_refresh_empty_state`
        # 内调 `setVisible` 不再报 object 没 attr;默认 None 表示不显示 hint。
        self._empty_hint: QWidget | None = None
        if empty_hint_spec is not None:
            icon_e, title_e, hint_e = empty_hint_spec
            self._empty_hint = empty_hint(icon_e, title_e, hint_e)
            root.addWidget(self._empty_hint)

        # 底部 hint label(行为提示)
        if bottom_hint:
            lbl = QLabel(bottom_hint)
            lbl.setProperty("role", "hint")
            root.addWidget(lbl)

        # 信号
        self.btn_action.clicked.connect(self.action_clicked)

    # ---- 数据装载 ----

    def set_items(self, channels: list[ChannelDTO], *, count_template: str) -> None:
        """装载频道 + 设置标题右侧 count(format 用 `{n}` 占位)。

        `count_template`:例如 `"已加入频道 · {n}"`、`"已监听 · {n}"`、
        `"已监听:{n}"`。模板只接受一个整数占位。
        """
        self.lst.clear()
        for ch in sorted(channels, key=lambda c: (c.title or "").lower()):
            item = QListWidgetItem(ch.display)
            item.setData(Qt.UserRole, ch.id)
            item.setIcon(_kind_icon(ch.kind))
            self.lst.addItem(item)
        self.count_label.setText(count_template.format(n=len(channels)))
        self._refresh_empty_state()

    def clear_items(self, *, count_template: str) -> None:
        """清空 + 显示 empty_hint(如果配置了)。"""
        self.lst.clear()
        self.count_label.setText(count_template.format(n=0))
        self._refresh_empty_state()

    def add_item(self, ch: ChannelDTO) -> None:
        """订阅事件时增量追加一条。注意:不复检是否重复(ChannelWidget 已用
        `_subscribed_ids` set 防重)。"""
        item = QListWidgetItem(ch.display)
        item.setData(Qt.UserRole, ch.id)
        item.setIcon(_kind_icon(ch.kind))
        self.lst.addItem(item)
        self._refresh_empty_state()

    def remove_by_cid(self, channel_id: int) -> bool:
        """按 channel_id 移除一行(退订事件)。找不到返 False。"""
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            if it is not None and it.data(Qt.UserRole) == channel_id:
                self.lst.takeItem(i)
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
            if not text:
                item.setHidden(False)
                continue
            if ch is None:
                item.setHidden(True)
                continue
            hay = (ch.title or "").lower() + " " + (ch.username or "").lower()
            item.setHidden(text not in hay)

    def selected_cids(self) -> list[int]:
        """多选模式:取当前选中行的 channel_ids(用于全量同步)。"""
        return [
            int(self.lst.item(i).data(Qt.UserRole))
            for i in range(self.lst.count())
            if self.lst.item(i) is not None and self.lst.item(i).isSelected()
        ]

    def all_cids(self) -> list[int]:
        """所有行的 cid(无选项全量同步时 fallback)。"""
        out: list[int] = []
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            if it is not None:
                cid = it.data(Qt.UserRole)
                if cid is not None:
                    out.append(int(cid))
        return out

    # ---- helpers ----

    def _refresh_empty_state(self) -> None:
        """list count == 0 + 配置了 empty_hint → 显示;否则隐藏。"""
        if self._empty_hint is not None:
            self._empty_hint.setVisible(self.lst.count() == 0)

    def _on_lst_double_clicked(self, item: QListWidgetItem) -> None:
        cid = item.data(Qt.UserRole)
        if cid is not None:
            self.item_double_clicked.emit(int(cid))


class ChannelWidget(QWidget):
    """频道管理面板 — 已加入 + 已监听 双栏 + 搜索过滤。

    已加入:从 Telegram 现拉的全部频道/群组,**双击 = 订阅**
    已监听:`AppService._subscribed` 当前白名单,**双击 = 退订;多选 + 全量同步**

    内部用两张 `_ChannelListCard` 实例(`joined_card` / `subs_card`)
    实际承载 UI,ChannelWidget 自身只负责装配 + 搜索 + EventBus 接线和
    增量子操作。
    """

    # 异步拉频道后 → 主线程刷新 list
    joined_loaded = Signal(list)
    # 用户在"已监听"栏多选 + 点"全量同步…" → 触发 sync dialog
    sync_requested = Signal(list)  # list[int] channel_ids

    def __init__(
        self,
        app: AppService,
        loop: asyncio.AbstractEventLoop,
        parent: QWidget | None = None,
    ) -> None:
        """建两张 `_ChannelListCard`(joined / subscribed)+ 接 EventBus + 异步拉频道。"""
        super().__init__(parent)
        self.app = app
        self.loop = loop
        self._joined: dict[int, ChannelDTO] = {}
        self._subscribed_ids: set[int] = set()
        self._build()
        self._wire_bus()
        # 异步拉频道 → 主线程刷新
        self.joined_loaded.connect(self._apply_joined)

    # ---- UI ----

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        # 顶部搜索框
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔍 搜索频道名 / username…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        root.addWidget(self.search_edit)

        # 上栏 — 已加入(`_ChannelListCard`,已接入 empty_hint #2-6)
        self.joined_card = _ChannelListCard(
            title="已加入频道",
            action_label="刷新",
            action_tooltip="从 Telegram 拉取当前账号加入的全部频道/群组",
            extended_selection=False,
            empty_hint_spec=(
                "📋",
                "暂无已加入频道",
                "请先在「设置 → 账户」登录 Telegram 账号,\n登录成功后点「刷新」自动拉取。",
            ),
            bottom_hint="💡 双击一行 → 加入监听白名单",
        )
        self.joined_card.action_clicked.connect(self._on_refresh)
        self.joined_card.item_double_clicked.connect(self._on_joined_double_click)
        root.addWidget(self.joined_card, 3)

        # 下栏 — 已监听
        self.subs_card = _ChannelListCard(
            title="已监听",
            action_label="全量同步…",
            action_tooltip="多选 + 全量拉取元数据 + 历史消息(可调频率防封号)",
            extended_selection=True,
            empty_hint_spec=None,  # 已监听「空」语义不明,不放 empty_hint
            bottom_hint="💡 双击一行 → 移出监听;Ctrl/Shift 多选 → 全量同步…",
        )
        self.subs_card.action_clicked.connect(self._on_sync_clicked)
        self.subs_card.item_double_clicked.connect(self._on_subscribed_double_click)
        root.addWidget(self.subs_card, 2)

        # ---- 兼容老 attribute 名(测试 + MainWindow 仍走旧 API)----
        # 旧代码依赖 `self.lst_joined` / `lst_subscribed` / `lbl_*_count` /
        # `btn_refresh` / `btn_sync`,把桥接留在 ChannelWidget 层 —
        # 测试 / 上层代码不再穿透到 card 层,_ChannelListCard 是私有实现细节。
        self.lst_joined = self.joined_card.lst
        self.lst_subscribed = self.subs_card.lst
        self.lbl_joined_count = self.joined_card.count_label
        self.lbl_subs_count = self.subs_card.count_label
        self.btn_refresh = self.joined_card.btn_action
        self.btn_sync = self.subs_card.btn_action
        self._empty_joined: QWidget | None = self.joined_card._empty_hint

    def _apply_filter(self, text: str) -> None:
        """搜索过滤 — 两张卡各跑一遍 (用 self._joined 字典查 title)。"""
        self.joined_card.apply_filter(text, self._joined)
        self.subs_card.apply_filter(text, self._joined)

    # ---- 数据装载 ----

    def set_joined(self, channels: list[ChannelDTO]) -> None:
        """外部装入已加入频道(DTO 列表)— 由 `MainWindow._refresh_state` 调。"""
        self._joined = {c.id: c for c in channels}
        self.joined_card.set_items(channels, count_template="已加入频道 · {n}")
        # 触发过滤(数据变了)
        self._apply_filter(self.search_edit.text())

    def set_subscribed(self, channels: list[ChannelDTO]) -> None:
        """外部装入已监听频道(白名单)— 由 `MainWindow._refresh_state` 调。"""
        self._subscribed_ids = {c.id for c in channels}
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
            if isinstance(e, ChannelSubscribed) and e.channel is not None:
                self._add_to_subscribed_list(e.channel)
            elif isinstance(e, ChannelUnsubscribed):
                self._remove_from_subscribed_list(e.channel_id)

        bus.subscribe(ChannelSubscribed, _on)
        bus.subscribe(ChannelUnsubscribed, _on)

    def _add_to_subscribed_list(self, ch: ChannelDTO) -> None:
        if ch.id in self._subscribed_ids:
            return
        # 也写入 joined(以防 joined 还没刷新)
        self._joined[ch.id] = ch
        self._subscribed_ids.add(ch.id)
        self.subs_card.add_item(ch)
        self.subs_card.count_label.setText(f"已监听:{len(self._subscribed_ids)}")

    def _remove_from_subscribed_list(self, channel_id: int) -> None:
        self._subscribed_ids.discard(channel_id)
        self.subs_card.remove_by_cid(channel_id)
        self.subs_card.count_label.setText(f"已监听:{len(self._subscribed_ids)}")

    # ---- 槽 ----

    def _on_refresh(self) -> None:
        async def _go() -> None:
            chs = await self.app.list_joined_channels()
            # 用 Signal 而非 QMetaObject.invokeMethod —— 后者把 Python list 经
            # Qt 元对象系统转 C++ 会 "Cannot copy-convert (list) to C++"。
            # Signal.emit 在 qasync 里跨 loop iteration 自然 queued,语义等价。
            self.joined_loaded.emit(chs)

        run_coro(self.loop, _go(), error_label="refresh_joined")

    def _apply_joined(self, chs: list[ChannelDTO]) -> None:
        self.set_joined(chs)

    def _on_joined_double_click(self, cid: int) -> None:
        """`_ChannelListCard.item_double_clicked` 直接给 channel_id。"""
        ch = self._joined.get(cid)
        if ch is None:
            return
        if cid in self._subscribed_ids:
            return  # 已订阅,双击无效(改在已监听栏里退订)
        run_coro(
            self.loop,
            self.app.subscribe_channel(ch),
            error_label="subscribe_channel",
        )

    def _on_subscribed_double_click(self, cid: int) -> None:
        if cid is None:
            return
        run_coro(
            self.loop,
            self.app.unsubscribe_channel(int(cid)),
            error_label="unsubscribe_channel",
        )

    def _on_sync_clicked(self) -> None:
        """用户多选 + 全量同步:收集 selected channel_ids,emit sync_requested。

        多选优先;没选则全量;都为空才弹提示框。
        """
        ids = self.subs_card.selected_cids()
        if not ids:
            # 没选:全部订阅
            ids = self.subs_card.all_cids()
        if not ids:
            QMessageBox.information(self, "全量同步", "已监听列表为空,先订阅频道")
            return
        self.sync_requested.emit(ids)

    def refresh_theme(self) -> None:
        """主题切换后调用 — 重渲两个 list 的频道类型图标(tinted color 跟主题变)。

        数据缓存 _joined / _subscribed_ids 不动,只是 setIcon 重画一次。
        """
        joined = list(self._joined.values())
        if joined:
            self.set_joined(joined)
        if self._subscribed_ids:
            sub = [c for cid, c in self._joined.items() if cid in self._subscribed_ids]
            if sub:
                self.set_subscribed(sub)
