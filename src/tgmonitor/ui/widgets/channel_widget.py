"""ChannelWidget — 主窗口侧栏下半部。

把"监听谁"这事最简化:
- **上半栏「全部(已加入)」**:从 Telegram 现拉的全部频道/群组,**双击 = 订阅**
- **下半栏「已监听」**:`AppService._subscribed` 当前白名单,**双击 = 退订**

事件:`ChannelSubscribed / ChannelUnsubscribed` 会刷新下半栏。
设计原则:订阅是高频操作,不应该藏在工具栏「刷新频道」里然后一揽子全量订。
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import ChannelSubscribed, ChannelUnsubscribed

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.events import Event, EventBus

log = logging.getLogger(__name__)


_ICON_CHANNEL = QColor("#3390ec")
_ICON_SUPER = QColor("#5cb85c")
_ICON_GROUP = QColor("#f0ad4e")


def _kind_color(kind: str) -> QColor:
    return {
        "channel": _ICON_CHANNEL,
        "supergroup": _ICON_SUPER,
        "group": _ICON_GROUP,
    }.get(kind, _ICON_GROUP)


def _paint_color_block(color: QColor, size: int = 14) -> "QIcon":
    """生成一个纯色小方块作图标,区分频道类型。

    返回 `QIcon`(含一个 QPixmap)而不是 QBrush — `QListWidgetItem.setIcon` 只接
    QIcon / QPixmap,不接 QBrush。
    """
    from PySide6.QtGui import QIcon, QPainter, QPixmap  # 局部,避免顶层循环 import

    pm = QPixmap(QSize(size, size))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setBrush(color)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(1, 1, size - 2, size - 2, 3, 3)
    p.end()
    return QIcon(pm)


class ChannelWidget(QGroupBox):
    # 异步拉频道后 → 主线程刷新 list
    joined_loaded = Signal(list)

    def __init__(
        self,
        app: AppService,
        loop: asyncio.AbstractEventLoop,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("频道", parent)
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
        root.setContentsMargins(10, 16, 10, 10)
        root.setSpacing(6)

        # 上栏
        head_joined = QHBoxLayout()
        self.lbl_joined_count = QLabel("全部(已加入):0")
        head_joined.addWidget(self.lbl_joined_count)
        head_joined.addStretch(1)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setToolTip("从 Telegram 拉取当前账号加入的全部频道/群组")
        self.btn_refresh.clicked.connect(self._on_refresh)
        head_joined.addWidget(self.btn_refresh)
        root.addLayout(head_joined)

        self.lst_joined = QListWidget()
        self.lst_joined.setAlternatingRowColors(True)
        self.lst_joined.itemDoubleClicked.connect(self._on_joined_double_click)
        root.addWidget(self.lst_joined, 3)

        # 提示
        hint = QLabel("💡 双击一行 → 加入监听白名单")
        hint.setProperty("role", "hint")
        root.addWidget(hint)

        # 下栏
        head_subs = QHBoxLayout()
        self.lbl_subs_count = QLabel("已监听:0")
        head_subs.addWidget(self.lbl_subs_count)
        head_subs.addStretch(1)
        root.addLayout(head_subs)
        self.lst_subscribed = QListWidget()
        self.lst_subscribed.setAlternatingRowColors(True)
        self.lst_subscribed.itemDoubleClicked.connect(self._on_subscribed_double_click)
        root.addWidget(self.lst_subscribed, 2)

        hint2 = QLabel("💡 双击一行 → 移出监听")
        hint2.setProperty("role", "hint")
        root.addWidget(hint2)

    # ---- 数据装载 ----

    def set_joined(self, channels: list[ChannelDTO]) -> None:
        self._joined = {c.id: c for c in channels}
        self.lst_joined.clear()
        for ch in sorted(channels, key=lambda c: (c.title or "").lower()):
            item = QListWidgetItem(ch.display)
            item.setData(Qt.UserRole, ch.id)
            item.setIcon(_paint_color_block(_kind_color(ch.kind)))
            self.lst_joined.addItem(item)
        self.lbl_joined_count.setText(f"全部(已加入):{len(channels)}")

    def set_subscribed(self, channels: list[ChannelDTO]) -> None:
        self._subscribed_ids = {c.id for c in channels}
        self.lst_subscribed.clear()
        for ch in sorted(channels, key=lambda c: (c.title or "").lower()):
            item = QListWidgetItem(ch.display)
            item.setData(Qt.UserRole, ch.id)
            item.setIcon(_paint_color_block(_kind_color(ch.kind)))
            self.lst_subscribed.addItem(item)
        self.lbl_subs_count.setText(f"已监听:{len(channels)}")

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
        item = QListWidgetItem(ch.display)
        item.setData(Qt.UserRole, ch.id)
        item.setIcon(_paint_color_block(_kind_color(ch.kind)))
        self.lst_subscribed.addItem(item)
        self.lbl_subs_count.setText(f"已监听:{len(self._subscribed_ids)}")

    def _remove_from_subscribed_list(self, channel_id: int) -> None:
        self._subscribed_ids.discard(channel_id)
        for i in range(self.lst_subscribed.count()):
            it = self.lst_subscribed.item(i)
            if it.data(Qt.UserRole) == channel_id:
                self.lst_subscribed.takeItem(i)
                break
        self.lbl_subs_count.setText(f"已监听:{len(self._subscribed_ids)}")

    # ---- 槽 ----

    def _on_refresh(self) -> None:
        async def _go() -> None:
            chs = await self.app.list_joined_channels()
            # 用 Signal 而非 QMetaObject.invokeMethod —— 后者把 Python list 经
            # Qt 元对象系统转 C++ 会 "Cannot copy-convert (list) to C++"。
            # Signal.emit 在 qasync 里跨 loop iteration 自然 queued,语义等价。
            self.joined_loaded.emit(chs)

        asyncio.run_coroutine_threadsafe(_go(), self.loop)

    def _apply_joined(self, chs: list[ChannelDTO]) -> None:
        self.set_joined(chs)

    def _on_joined_double_click(self, item: QListWidgetItem) -> None:
        cid = item.data(Qt.UserRole)
        ch = self._joined.get(cid)
        if ch is None:
            return
        if cid in self._subscribed_ids:
            return  # 已订阅,双击无效(改在已监听栏里退订)
        fut = asyncio.run_coroutine_threadsafe(
            self.app.subscribe_channel(ch), self.loop
        )

        def _on_done(f) -> None:
            try:
                f.result()
            except Exception as exc:  # noqa: BLE001
                log.exception("subscribe_channel failed: %s", exc)

        fut.add_done_callback(_on_done)

    def _on_subscribed_double_click(self, item: QListWidgetItem) -> None:
        cid = item.data(Qt.UserRole)
        if cid is None:
            return
        fut = asyncio.run_coroutine_threadsafe(
            self.app.unsubscribe_channel(int(cid)), self.loop
        )

        def _on_done(f) -> None:
            try:
                f.result()
            except Exception as exc:  # noqa: BLE001
                log.exception("unsubscribe_channel failed: %s", exc)

        fut.add_done_callback(_on_done)
