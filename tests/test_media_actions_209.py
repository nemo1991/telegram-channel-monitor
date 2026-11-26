"""v2.0.9 媒体页行操作契约 — offscreen 安全的对象树 / 信号断言。

取代 `test_media_manager_widget.py`（27 例三平台全 skip、零覆盖）。本文件：
- 不 `show()` widget、不做真实 paint、不反复 processEvents —— 避开旧测试
  因 `show() + processEvents()` 触发的 Qt paint path race（那正是旧文件被
  三平台整体 skip 的根因）。
- 只断言结构性契约：Signal 签名 / 64 位 id 往返、行尾按钮类型与 objectName、
  菜单项 enabled 规则、_RowKey 定位协议、LIGHTBOX_PREVIEWABLE_TYPES 反向导出。
- 几何相关用对象树/相对关系而非 `mapToGlobal`（参考 `test_ui_208.py` 范式）。

PR-2 起：行尾 4 个 QToolButton 图标（下载/打开/重试/⋯）+ ⋯ 的 QMenu
（Reveal/Copy/Delete），替换原 5 个文字 QPushButton。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import (
    QApplication,
    QListWidgetItem,
    QMenu,
    QToolButton,
)

from tgmonitor.core.dto import (
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
)
from tgmonitor.ui.widgets._mm_base import _RowKey
from tgmonitor.ui.widgets.media_manager_widget import (
    MediaManagerWidget,
)

# 真实 Telegram chat_id —— Signal(int) 是 C++ 32 位,emit 会 OverflowError。
CHANNEL_ID = -1001375475051
MSG_ID = 42
MEDIA_IDX = 0

# 行尾常驻按钮的 objectName（FR-03 契约;2026-11-23 下载/打开合并为 1 个主按钮）。
ACTION_NAMES = (
    "mediaActionDownload",
    "mediaRowMore",
)


@pytest.fixture
def widget(qapp: QApplication) -> MediaManagerWidget:
    w = MediaManagerWidget()
    yield w
    w.deleteLater()


def _msg(channel_id: int = CHANNEL_ID, msg_id: int = MSG_ID) -> MessageDTO:
    return MessageDTO(
        id=0,
        channel_id=channel_id,
        telegram_msg_id=msg_id,
        text="",
        media=[],
    )


def _media(status: MediaDownloadStatus) -> MediaDTO:
    return MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name="photo.jpg",
        file_size=1024,
        telegram_file_id="fid",
        object_key="media/photo.jpg" if status == MediaDownloadStatus.DONE else None,
        object_backend="local" if status == MediaDownloadStatus.DONE else None,
        download_status=status,
    )


def _render(w: MediaManagerWidget, status: MediaDownloadStatus) -> None:
    """渲染一行,不 show、不 processEvents —— 纯对象树构建。"""
    med = _media(status)
    w.on_media_loaded(([(_msg(), MEDIA_IDX, med)], 1))


def _row_buttons(w: MediaManagerWidget) -> list[QToolButton]:
    item: QListWidgetItem = w.list.item(0)
    assert item is not None, "row 0 未渲染"
    row = w.list.itemWidget(item)
    assert row is not None
    return row.findChildren(QToolButton)


def _row_menu(w: MediaManagerWidget) -> QMenu:
    more = next(b for b in _row_buttons(w) if b.objectName() == "mediaRowMore")
    # 2026-11-22:不再 setMenu —— 挂 menu 会让原生样式多画一个下箭头。
    assert more.menu() is None, "⋯ 不应 setMenu(会多一个原生下箭头)"
    menu = more.findChild(QMenu, "mediaRowMenu")
    assert menu is not None, "⋯ 点击弹出的 QMenu 应作为按钮子对象存在"
    return menu


# ---- 对外契约:Signal 签名与反向导出 -------------------------------------


@pytest.mark.parametrize(
    "signal_name",
    [
        "download_requested",  # 2026-11-22 v2.0.9 FR-06 新增
        "open_requested",
        "retry_requested",
        "delete_requested",
        "reveal_requested",
        "copy_requested",
        "preview_requested",
    ],
)
def test_row_signals_carry_64bit_channel_id(widget: MediaManagerWidget, signal_name: str) -> None:
    """7 个行 Signal（含新增 download）均承载 64 位 channel_id。"""
    sig = getattr(widget, signal_name)
    got: list[tuple[int, int, int]] = []
    sig.connect(lambda c, m, i: got.append((c, m, i)))
    sig.emit(CHANNEL_ID, MSG_ID, MEDIA_IDX)
    assert got == [(CHANNEL_ID, MSG_ID, MEDIA_IDX)]


# ---- _RowKey 定位协议 ---------------------------------------------------


def test_progress_outline_is_square_not_arc() -> None:
    """方形环绕守卫:进度线全轴对齐且贴住 40×40 缩略图四边,不再是内切圆弧。"""
    from PySide6.QtCore import QPointF, QRectF

    from tgmonitor.ui.widgets._mm_render import _ProgressThumb

    rect = QRectF(0, 0, 40, 40)
    assert _ProgressThumb._progress_segments(rect, 0) == []
    half = _ProgressThumb._progress_segments(rect, 50)
    assert half and all(start.x() == end.x() or start.y() == end.y() for start, end in half), (
        "进度线必须沿矩形边(水平/垂直),不应是圆弧"
    )
    full = _ProgressThumb._progress_segments(rect, 100)
    assert len(full) == 5, f"完整一圈应为 5 段,实际 {len(full)}"
    assert full[0][0] == full[-1][1] == QPointF(20, 0), "应从顶边中点起止闭合"
    assert all(0 <= p.x() <= 40 and 0 <= p.y() <= 40 for seg in full for p in seg), (
        "进度不得越出缩略图外框"
    )
    # 25% = 周长 160 的 40:顶边右半 20 + 右边 20 → 落到右边中点。
    assert _ProgressThumb._progress_segments(rect, 25)[-1][1] == QPointF(40, 20)


# ---- 行尾按钮契约（2 个 QToolButton:主按钮 + ⋯ 菜单）----------------------


@pytest.mark.parametrize(
    ("status", "signal_name"),
    [
        (None, "download_requested"),
        (MediaDownloadStatus.DOWNLOADING, "download_requested"),
        (MediaDownloadStatus.FAILED, "retry_requested"),
        (MediaDownloadStatus.DONE, "preview_requested"),
    ],
)
def test_unified_primary_button_dispatches_by_status(
    widget: MediaManagerWidget, status: MediaDownloadStatus, signal_name: str
) -> None:
    """2026-11-23:下载/打开合并为一个按钮,按行状态分派首下/重下/预览。

    DONE 走 preview_requested(lightbox)而非 open_requested —— 用户实测「打开
    按钮无法打开、点缩略图能开」,合并后统一走能用的那条路径。
    """
    _render(widget, status)
    got: list[tuple[int, int, int]] = []
    for name in ("download_requested", "retry_requested", "preview_requested", "open_requested"):
        getattr(widget, name).connect(
            lambda c, m, i, n=name: got.append((c, m, i)) if n == signal_name else None
        )
    by_name = {b.objectName(): b for b in _row_buttons(widget)}
    btn = by_name["mediaActionDownload"]
    btn.click()
    assert got == [(CHANNEL_ID, MSG_ID, MEDIA_IDX)], f"{status} 应走 {signal_name}"
    # 2026-11-23:下载/重下点击后即时禁用;预览(DONE)不禁用。
    if signal_name == "preview_requested":
        assert btn.isEnabled(), "预览不应禁用按钮"
    else:
        assert not btn.isEnabled(), "发起下载后应即时禁用按钮"


# ---- 2026-11-22 媒体页修复:翻页 / 频道名 ----------------------------------


# ---- 2026-11-22 重渲选中态 IndexError 守卫 ---------------------------------


# ---- 2026-11-22 用户选型:PENDING 批量下载与状态区分 ------------------------


def test_batch_download_emits_only_pending_keys(widget: MediaManagerWidget) -> None:
    """所选含 PENDING + DONE 时,只 emit PENDING 的 key。"""
    pending = _media(None)
    done = _media(MediaDownloadStatus.DONE)
    msg = _msg()
    widget.on_media_loaded(
        (
            [
                (msg, 0, pending),
                (msg, 1, done),
            ],
            2,
        )
    )
    widget.list.selectAll()
    got: list[list[_RowKey]] = []
    widget.batch_download_requested.connect(lambda keys: got.append(keys))
    widget._on_batch_download()
    assert len(got) == 1
    assert got[0] == [_RowKey(CHANNEL_ID, MSG_ID, 0)]


# ---- 2026-11-24:下载完成重渲不应把列表滚回首行 -------------------------------


class _FakeScrollBar:
    """只记录 value()/setValue() 的滚动条替身。

    离屏 widget 未 show() 时 QListWidget 的 scroll range 恒为 0,无法直接
    断言真实滚动值;这里替换滚动条方法,守卫 `_render_list` 的存/取逻辑。
    """

    def __init__(self) -> None:
        self.current = 0
        self.history: list[int] = []

    def value(self) -> int:
        return self.current

    def setValue(self, value: int) -> None:  # noqa: N802 — Qt 命名
        self.history.append(value)
        self.current = value


def _many_rows(n: int) -> list[tuple[MessageDTO, int, MediaDTO]]:
    return [
        (
            _msg(msg_id=i),
            MEDIA_IDX,
            _media(MediaDownloadStatus.DONE if i % 2 else None),
        )
        for i in range(n)
    ]


def test_rerender_preserves_scroll_position(
    widget: MediaManagerWidget, monkeypatch: pytest.MonkeyPatch
) -> None:
    """下载完成等原地 refresh 重建整表后,滚动条应回到原位置。"""
    vbar, hbar = _FakeScrollBar(), _FakeScrollBar()
    monkeypatch.setattr(widget.list, "verticalScrollBar", lambda: vbar)
    monkeypatch.setattr(widget.list, "horizontalScrollBar", lambda: hbar)

    rows = _many_rows(50)
    widget.on_media_loaded((rows, 50))
    assert vbar.history == [0], "首次渲染显式回到顶部"

    vbar.setValue(240)
    hbar.setValue(64)
    vbar.history.clear()
    hbar.history.clear()

    # 模拟 MediaDownloaded → refresh_requested → on_media_loaded 的同一页重渲。
    widget.on_media_loaded((rows, 50))
    assert vbar.history[-1] == 240, "重渲后纵向滚动应保留"
    assert hbar.history[-1] == 64, "重渲后横向滚动应保留"
