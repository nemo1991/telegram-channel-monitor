"""v2.0.9 媒体页行操作契约 — offscreen 安全的对象树 / 信号断言。

取代 `test_media_manager_widget.py`（27 例三平台全 skip、零覆盖）。本文件：
- 不 `show()` widget、不做真实 paint、不反复 processEvents —— 避开旧测试
  因 `show() + processEvents()` 触发的 Qt paint path race（那正是旧文件被
  三平台整体 skip 的根因）。
- 只断言结构性契约：Signal 签名 / 64 位 id 往返、行尾按钮类型与 objectName、
  菜单项 enabled 规则、_RowKey 定位协议、LIGHTBOX_PREVIEWABLE_TYPES 反向导出。
- 几何相关用对象树/相对关系而非 `mapToGlobal`（参考 `test_ui_208.py` 范式）。

v2.0.9 分两步：PR-1 建骨架、覆盖**现状**文字按钮契约（保证绿，先立守护网）；
PR-2 重构为 QToolButton + ⋯ 菜单后，本文件同 PR 更新为新契约。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QListWidgetItem, QPushButton  # noqa: E402

from tgmonitor.core.dto import (  # noqa: E402
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
)
from tgmonitor.ui.widgets._mm_base import _RowKey  # noqa: E402
from tgmonitor.ui.widgets.media_manager_widget import (  # noqa: E402
    LIGHTBOX_PREVIEWABLE_TYPES,
    MediaManagerWidget,
)

# 真实 Telegram chat_id —— Signal(int) 是 C++ 32 位,emit 会 OverflowError。
CHANNEL_ID = -1001375475051
MSG_ID = 42
MEDIA_IDX = 0


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


def _row_buttons(w: MediaManagerWidget) -> list[QPushButton]:
    item: QListWidgetItem = w.list.item(0)
    assert item is not None, "row 0 未渲染"
    row = w.list.itemWidget(item)
    assert row is not None
    return row.findChildren(QPushButton)


# ---- 对外契约:Signal 签名与反向导出 -------------------------------------


@pytest.mark.parametrize(
    "signal_name",
    [
        "open_requested",
        "retry_requested",
        "delete_requested",
        "reveal_requested",
        "copy_requested",
        "preview_requested",
    ],
)
def test_row_signals_carry_64bit_channel_id(widget: MediaManagerWidget, signal_name: str) -> None:
    """PR-1 守护:6 个行 Signal 均承载 64 位 channel_id（PR-2 新增 download 同签名）。"""
    sig = getattr(widget, signal_name)
    got: list[tuple[int, int, int]] = []
    sig.connect(lambda c, m, i: got.append((c, m, i)))
    sig.emit(CHANNEL_ID, MSG_ID, MEDIA_IDX)
    assert got == [(CHANNEL_ID, MSG_ID, MEDIA_IDX)]


def test_download_signal_not_yet_defined_pr1(widget: MediaManagerWidget) -> None:
    """PR-1 锚点:download_requested 尚未接线（PR-2 起此断言改为存在且同签名）。"""
    assert not hasattr(widget, "download_requested")


def test_lightbox_previewable_types_reexported() -> None:
    """反向导出契约:message_detail 从本 module 导入,不得移除。"""
    import tgmonitor.ui.widgets.message_detail as detail_mod  # noqa: PLC0415

    assert detail_mod.LIGHTBOX_PREVIEWABLE_TYPES is LIGHTBOX_PREVIEWABLE_TYPES


# ---- _RowKey 定位协议 ---------------------------------------------------


def test_row_key_locator_protocol() -> None:
    key = _RowKey(channel_id=CHANNEL_ID, telegram_msg_id=MSG_ID, media_idx=MEDIA_IDX)
    assert key == _RowKey(CHANNEL_ID, MSG_ID, MEDIA_IDX)
    assert hash(key) == hash(_RowKey(CHANNEL_ID, MSG_ID, MEDIA_IDX))


def test_render_populates_status_labels_by_row_key(widget: MediaManagerWidget) -> None:
    """进度协议:渲染行后 _status_labels 按 _RowKey 定位,事件进来可刷新文字。"""
    _render(widget, MediaDownloadStatus.DOWNLOADING)
    key = _RowKey(CHANNEL_ID, MSG_ID, MEDIA_IDX)
    assert key in widget._status_labels

    from tgmonitor.core.events import MediaDownloadProgress

    widget.on_download_progress(
        MediaDownloadProgress(
            channel_id=CHANNEL_ID,
            telegram_msg_id=MSG_ID,
            media_idx=MEDIA_IDX,
            downloaded=512,
            total=1024,
        )
    )
    text = widget._status_labels[key].text()
    assert "/" in text and "%" in text, f"进度文字未格式化: {text!r}"


def test_progress_unknown_row_silent(widget: MediaManagerWidget) -> None:
    """进度事件 key 不存在时静默跳过,不抛 KeyError。"""
    from tgmonitor.core.events import MediaDownloadProgress

    widget.on_media_loaded(([], 0))
    widget.on_download_progress(
        MediaDownloadProgress(
            channel_id=999,
            telegram_msg_id=1,
            media_idx=0,
            downloaded=10,
            total=100,
        )
    )  # 不崩即过


# ---- 行尾按钮契约（PR-1:现状 5 个文字按钮）------------------------------


@pytest.mark.parametrize("label", ["Open", "Reveal", "Copy", "Retry", "Delete"])
def test_row_has_five_text_action_buttons_pr1(widget: MediaManagerWidget, label: str) -> None:
    """PR-1 锚点:现状每行 5 个文字 QPushButton（PR-2 重构后更新为图标契约）。"""
    _render(widget, MediaDownloadStatus.DONE)
    buttons = _row_buttons(widget)
    assert any(b.text() == label for b in buttons), f"缺少按钮: {label}"


def test_row_button_enabled_rules_pr1(widget: MediaManagerWidget) -> None:
    """现状 enabled 规则:Open/Reveal/Copy 仅 DONE、Retry 仅 FAILED、Delete 恒可用。"""
    by_label = lambda w: {b.text(): b for b in _row_buttons(w)}  # noqa: E731

    _render(widget, MediaDownloadStatus.DONE)
    done = by_label(widget)
    assert done["Open"].isEnabled()
    assert done["Reveal"].isEnabled()
    assert done["Copy"].isEnabled()
    assert not done["Retry"].isEnabled()
    assert done["Delete"].isEnabled()

    _render(widget, MediaDownloadStatus.FAILED)
    failed = by_label(widget)
    assert not failed["Open"].isEnabled()
    assert failed["Retry"].isEnabled()
    assert failed["Delete"].isEnabled()

    _render(widget, MediaDownloadStatus.PENDING)
    pending = by_label(widget)
    assert not pending["Open"].isEnabled()
    assert not pending["Retry"].isEnabled()
    assert pending["Delete"].isEnabled()


def test_row_button_emits_correct_args_pr1(widget: MediaManagerWidget) -> None:
    """按钮点击 emit (channel_id, telegram_msg_id, media_idx) —— 64 位 id 不溢出。"""
    _render(widget, MediaDownloadStatus.DONE)
    got: list[tuple[int, int, int]] = []
    widget.reveal_requested.connect(lambda c, m, i: got.append((c, m, i)))
    by_label = {b.text(): b for b in _row_buttons(widget)}
    by_label["Reveal"].click()
    assert got == [(CHANNEL_ID, MSG_ID, MEDIA_IDX)]
