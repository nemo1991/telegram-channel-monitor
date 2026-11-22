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

import pytest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QListWidgetItem,
    QMenu,
    QToolButton,
)

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

# 行尾常驻按钮的 objectName（FR-03 契约）。
ACTION_NAMES = (
    "mediaActionDownload",
    "mediaActionOpen",
    "mediaActionRetry",
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
    menu = more.menu()
    assert menu is not None, "⋯ 按钮应挂 QMenu"
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


# ---- 行尾按钮契约（PR-2:4 个 QToolButton 图标 + ⋯ 菜单）-------------------


def test_row_has_four_tool_buttons_with_object_names(
    widget: MediaManagerWidget,
) -> None:
    """每行常驻恰好 4 个 QToolButton,每个有非空且唯一的 objectName。"""
    _render(widget, MediaDownloadStatus.DONE)
    buttons = _row_buttons(widget)
    assert len(buttons) == 4, f"常驻按钮数应为 4: {len(buttons)}"
    assert all(isinstance(b, QToolButton) for b in buttons)
    names = [b.objectName() for b in buttons]
    assert set(names) == set(ACTION_NAMES), f"objectName 不符: {names}"
    assert len(set(names)) == 4, f"objectName 应唯一: {names}"


def test_row_buttons_have_tooltips(widget: MediaManagerWidget) -> None:
    """可访问性:每个图标按钮带非空 tooltip。"""
    _render(widget, MediaDownloadStatus.DONE)
    for btn in _row_buttons(widget):
        assert btn.toolTip(), f"{btn.objectName()} 缺 tooltip"


def test_no_text_push_buttons_in_row(widget: MediaManagerWidget) -> None:
    """重构后原 5 个文字 QPushButton 命中数为 0(防回退)。"""
    from PySide6.QtWidgets import QPushButton  # noqa: PLC0415

    _render(widget, MediaDownloadStatus.DONE)
    item: QListWidgetItem = widget.list.item(0)
    row = widget.list.itemWidget(item)
    assert row is not None
    assert row.findChildren(QPushButton) == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            MediaDownloadStatus.PENDING,
            {"mediaActionDownload": True, "mediaActionOpen": False, "mediaActionRetry": False},
        ),
        (
            MediaDownloadStatus.FAILED,
            {"mediaActionDownload": False, "mediaActionOpen": False, "mediaActionRetry": True},
        ),
        (
            MediaDownloadStatus.DONE,
            {"mediaActionDownload": False, "mediaActionOpen": True, "mediaActionRetry": False},
        ),
        (
            MediaDownloadStatus.DOWNLOADING,
            {"mediaActionDownload": False, "mediaActionOpen": False, "mediaActionRetry": False},
        ),
    ],
)
def test_row_button_enabled_rules(
    widget: MediaManagerWidget,
    status: MediaDownloadStatus,
    expected: dict[str, bool],
) -> None:
    """常驻图标 enabled:下载仅 PENDING、打开仅 DONE、重试仅 FAILED。"""
    _render(widget, status)
    by_name = {b.objectName(): b for b in _row_buttons(widget)}
    for name, enabled in expected.items():
        assert by_name[name].isEnabled() is enabled, (
            f"{status.value}: {name} enabled={by_name[name].isEnabled()}, 期望 {enabled}"
        )
    assert by_name["mediaRowMore"].isEnabled(), "⋯ 菜单入口恒可用"


def test_menu_has_three_actions(widget: MediaManagerWidget) -> None:
    """⋯ 菜单含 Reveal / Copy / Delete 三项,替代原常驻文字按钮。"""
    _render(widget, MediaDownloadStatus.DONE)
    menu = _row_menu(widget)
    texts = [a.text() for a in menu.actions()]
    assert len(texts) == 3, f"菜单项数应为 3: {texts}"
    assert any("显示" in t for t in texts)
    assert any("复制" in t for t in texts)
    assert any("删除" in t for t in texts)


@pytest.mark.parametrize(
    ("status", "reveal_copy_enabled"),
    [
        (MediaDownloadStatus.DONE, True),
        (MediaDownloadStatus.PENDING, False),
        (MediaDownloadStatus.FAILED, False),
    ],
)
def test_menu_enabled_recomputed_on_show(
    widget: MediaManagerWidget,
    status: MediaDownloadStatus,
    reveal_copy_enabled: bool,
) -> None:
    """⋯ 菜单 aboutToShow 时按当前行状态重算 Reveal/Copy 的 enabled。

    直接 emit aboutToShow（不 show widget、不触发 paint),断言菜单项 enabled
    随行状态变化——证明不缓存跨行状态（RK-06）。
    """
    _render(widget, status)
    menu = _row_menu(widget)
    menu.aboutToShow.emit()
    reveal, copy, delete = menu.actions()
    assert reveal.isEnabled() is reveal_copy_enabled
    assert copy.isEnabled() is reveal_copy_enabled
    assert delete.isEnabled(), "Delete 恒可用"


@pytest.mark.parametrize(
    ("name", "signal_name"),
    [
        ("mediaActionDownload", "download_requested"),
        ("mediaActionOpen", "open_requested"),
        ("mediaActionRetry", "retry_requested"),
    ],
)
def test_row_button_click_emits_correct_args(
    widget: MediaManagerWidget, name: str, signal_name: str
) -> None:
    """常驻图标 click emit (channel_id, telegram_msg_id, media_idx) —— 64 位不溢出。"""
    status = {
        "mediaActionDownload": MediaDownloadStatus.PENDING,
        "mediaActionOpen": MediaDownloadStatus.DONE,
        "mediaActionRetry": MediaDownloadStatus.FAILED,
    }[name]
    _render(widget, status)
    got: list[tuple[int, int, int]] = []
    getattr(widget, signal_name).connect(lambda c, m, i: got.append((c, m, i)))
    by_name = {b.objectName(): b for b in _row_buttons(widget)}
    by_name[name].click()
    assert got == [(CHANNEL_ID, MSG_ID, MEDIA_IDX)]


@pytest.mark.parametrize(
    ("action_idx", "signal_name"),
    [(0, "reveal_requested"), (1, "copy_requested"), (2, "delete_requested")],
)
def test_menu_action_trigger_emits_correct_args(
    widget: MediaManagerWidget, action_idx: int, signal_name: str
) -> None:
    """菜单项 trigger emit 与常驻图标同一组 Signal,64 位 id 不溢出。"""
    _render(widget, MediaDownloadStatus.DONE)
    got: list[tuple[int, int, int]] = []
    getattr(widget, signal_name).connect(lambda c, m, i: got.append((c, m, i)))
    _row_menu(widget).actions()[action_idx].trigger()
    assert got == [(CHANNEL_ID, MSG_ID, MEDIA_IDX)]
