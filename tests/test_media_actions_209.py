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
    UNDOWNLOADED_FILTER,
    ChannelDTO,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    SortDir,
    SortKey,
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
            None,
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
        (None, False),
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
        "mediaActionDownload": None,
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


# ---- 2026-11-22 媒体页修复:翻页 / 频道名 ----------------------------------


def test_page_nav_are_icon_tool_buttons(widget: MediaManagerWidget) -> None:
    """翻页箭头为 tinted SVG 图标按钮,不再依赖字体 glyph。"""
    from PySide6.QtWidgets import QToolButton  # noqa: PLC0415

    for btn, name in ((widget.btn_prev, "mediaPagePrev"), (widget.btn_next, "mediaPageNext")):
        assert isinstance(btn, QToolButton)
        assert btn.objectName() == name
        assert not btn.icon().isNull(), f"{name} 缺图标"
        assert not btn.text(), f"{name} 不应有字形文本"
        assert btn.toolTip() in ("上一页", "下一页")


def test_current_filters_carry_offset_limit_and_sort(widget: MediaManagerWidget) -> None:
    """UI 每页 50、当前页 2 → offset=100 / limit=50,sort 也一并导出。"""
    widget._page = 2
    widget._total = 137
    widget.cmb_sort.setCurrentIndex(0)
    widget.cmb_dir.setCurrentIndex(0)
    f = widget.current_filters()
    assert f["offset"] == 100
    assert f["limit"] == 50
    assert f["sort"] is SortKey.DATE
    assert f["sort_dir"] is SortDir.DESC


def test_page_clamp_requests_reload(widget: MediaManagerWidget) -> None:
    """总数变小导致当前页越界 → 钳回合法页并主动 refresh,避免空白页。"""
    calls: list[int] = []
    widget.refresh_requested.connect(lambda: calls.append(1))
    widget._page = 4
    widget.on_media_loaded(([], 51))  # 只剩 2 页
    assert widget._page == 1
    assert widget.lbl_page.text() == "2 / 2"
    assert len(calls) == 1, f"钳制后应重拉一次: {calls}"


def test_refresh_controller_forwards_all_pagination_args() -> None:
    """回归守卫:controller 不得 drop offset / limit / sort / sort_dir。"""
    from types import SimpleNamespace  # noqa: PLC0415

    from tgmonitor.ui.controllers.media_actions_controller import (  # noqa: PLC0415
        MediaActionsController,
    )

    f = {
        "channel_id": CHANNEL_ID,
        "status": None,
        "media_type": None,
        "search": "",
        "sort": SortKey.SIZE,
        "sort_dir": SortDir.ASC,
        "offset": 150,
        "limit": 50,
        "total": 200,
    }
    got: dict[str, object] = {}

    def fake_load(**kwargs: object) -> None:
        got.update(kwargs)

    ctx = SimpleNamespace(
        media_manager=SimpleNamespace(
            set_monitored_channels=lambda _channels: None,
            current_filters=lambda: f,
        ),
        vm=SimpleNamespace(load_media_list=fake_load, monitored_channels={}),
    )
    MediaActionsController(ctx).on_media_refresh()
    assert got == {
        "channel_id": CHANNEL_ID,
        "status": None,
        "media_type": None,
        "search": "",
        "undownloaded": False,
        "limit": 50,
        "offset": 150,
        "sort": SortKey.SIZE,
        "sort_dir": SortDir.ASC,
    }


def test_channel_filter_chinese_placeholder_and_title(widget: MediaManagerWidget) -> None:
    """频道下拉重建后占位仍是中文,列表项以频道名(title)为主。"""
    channels = [
        ChannelDTO(id=CHANNEL_ID, title="产品研发交流群", username="dev_room"),
        ChannelDTO(id=1002, title="行业资讯速递"),
    ]
    widget.set_monitored_channels(channels)
    texts = [widget.cmb_channel.itemText(i) for i in range(widget.cmb_channel.count())]
    assert texts == ["全部频道", "产品研发交流群", "行业资讯速递"]
    assert not any(t.startswith("@") for t in texts), "下拉不应只显 @username"


def test_row_channel_label_prefers_title(widget: MediaManagerWidget) -> None:
    """列表「所属频道」列以频道名为主,@username 只进 tooltip。"""
    from PySide6.QtWidgets import QLabel  # noqa: PLC0415

    ch = ChannelDTO(id=CHANNEL_ID, title="产品研发交流群", username="dev_room")
    widget.set_monitored_channels([ch])
    _render(widget, MediaDownloadStatus.DONE)
    item: QListWidgetItem = widget.list.item(0)
    row = widget.list.itemWidget(item)
    assert row is not None
    texts = [lbl.text() for lbl in row.findChildren(QLabel)]
    assert "产品研发交流群" in texts
    assert "@dev_room" not in texts
    assert any(
        lbl.text() == "产品研发交流群" and lbl.toolTip() == "@dev_room"
        for lbl in row.findChildren(QLabel)
    )


# ---- 2026-11-22 重渲选中态 IndexError 守卫 ---------------------------------


def test_find_row_by_key_stale_index_returns_none(widget: MediaManagerWidget) -> None:
    """`_last_keys`(旧) 长于 `_rows`(新) 时,旧 key 不许越界取值。"""
    med = _media(MediaDownloadStatus.DONE)
    widget._rows = [(_msg(), MEDIA_IDX, med)]
    widget._last_keys = [
        _RowKey(CHANNEL_ID, MSG_ID, MEDIA_IDX),
        _RowKey(999, 999, 0),  # 旧行,索引 1 已超出新 _rows
    ]
    assert widget._find_row_by_key(_RowKey(CHANNEL_ID, MSG_ID, MEDIA_IDX)) is widget._rows[0]
    assert widget._find_row_by_key(_RowKey(999, 999, 0)) is None
    widget._on_selection_changed()  # 不抛即过


def test_rerender_after_select_all_no_index_error(
    widget: MediaManagerWidget, monkeypatch: pytest.MonkeyPatch
) -> None:
    """选中 50 行后重渲成 3 行 —— `list.clear()` 的选中变化不得越界。

    PySide6 默认吞掉 slot 里的异常(只打印 traceback),所以这里给
    `_find_row_by_key` 挂一层捕获器,把异常收进列表再断言为空。
    """
    errors: list[Exception] = []
    orig = widget._find_row_by_key

    def guarded(key: _RowKey) -> object:
        try:
            return orig(key)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            return None

    monkeypatch.setattr(widget, "_find_row_by_key", guarded)

    def many(n: int) -> list[tuple[MessageDTO, int, MediaDTO]]:
        return [
            (
                _msg(msg_id=i),
                MEDIA_IDX,
                _media(MediaDownloadStatus.FAILED if i % 2 else MediaDownloadStatus.DONE),
            )
            for i in range(n)
        ]

    widget.on_media_loaded((many(50), 50))
    widget.list.selectAll()
    widget.on_media_loaded((many(3), 3))
    assert errors == [], f"重渲期间发生异常: {errors}"
    assert len(widget._last_keys) == 3
    assert not widget.btn_delete_sel.isEnabled(), "重渲后选中集应清空"


# ---- 2026-11-22 用户选型:PENDING 批量下载与状态区分 ------------------------


def test_status_text_distinguishes_pending_from_downloading(
    widget: MediaManagerWidget,
) -> None:
    """PENDING 显示「未下载」灰字,DOWNLOADING 才是 ⏳,tooltip 也分开。"""
    from tgmonitor.ui.widgets._mm_base import _STATUS_TEXT  # noqa: PLC0415

    assert _STATUS_TEXT[None] == "未下载"
    assert _STATUS_TEXT[MediaDownloadStatus.DOWNLOADING] == "⏳"

    key = _RowKey(CHANNEL_ID, MSG_ID, MEDIA_IDX)
    _render(widget, None)
    pending = widget._status_labels[key]
    assert pending.text() == "未下载"
    assert "下载" in pending.toolTip() and "未下载" in pending.toolTip()

    _render(widget, MediaDownloadStatus.DOWNLOADING)
    downloading = widget._status_labels[key]
    assert downloading.text() == "⏳"
    assert downloading.toolTip() != pending.toolTip()


def test_pending_download_error_shown_in_tooltip(widget: MediaManagerWidget) -> None:
    """无 downloader 写下的原因必须能在 PENDING tooltip 里看到。"""
    from dataclasses import replace  # noqa: PLC0415

    med = replace(
        _media(None),
        download_error="未配置下载器,无法下载原文件",
    )
    widget.on_media_loaded(([(_msg(), MEDIA_IDX, med)], 1))
    lbl = widget._status_labels[_RowKey(CHANNEL_ID, MSG_ID, MEDIA_IDX)]
    assert "未配置下载器" in lbl.toolTip()


def test_batch_download_button_enabled_for_pending_only(
    widget: MediaManagerWidget,
) -> None:
    """选中 PENDING → 「下载所选」可用;选中 DONE → 不可用。"""
    _render(widget, None)
    widget.list.item(0).setSelected(True)
    assert widget.btn_download_sel.isEnabled()
    assert not widget.btn_retry_sel.isEnabled()

    _render(widget, MediaDownloadStatus.DONE)
    widget.list.item(0).setSelected(True)
    assert not widget.btn_download_sel.isEnabled()


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


def test_download_all_button_emits_signal(widget: MediaManagerWidget) -> None:
    """「下载全部待下」按钮 emit 无参信号。"""
    calls: list[int] = []
    widget.download_all_pending_requested.connect(lambda: calls.append(1))
    widget.btn_download_all.click()
    assert calls == [1]


def test_download_controller_forwards_batch_and_all() -> None:
    """控制器把批量选中 / 全部 PENDING 转给 VM,不自己重实现下载逻辑。"""
    from types import SimpleNamespace  # noqa: PLC0415

    from tgmonitor.ui.controllers.media_actions_controller import (  # noqa: PLC0415
        MediaActionsController,
    )

    got: dict[str, object] = {}
    ctx = SimpleNamespace(
        media_manager=SimpleNamespace(
            current_filters=lambda: {
                "channel_id": CHANNEL_ID,
                "search": "photo",
                "media_type": MediaType.PHOTO,
            }
        ),
        vm=SimpleNamespace(
            download_media_batch=lambda keys: got.__setitem__("keys", keys),
            download_all_pending=lambda **kw: got.__setitem__("all", kw),
        ),
    )
    ctrl = MediaActionsController(ctx)
    key = _RowKey(CHANNEL_ID, MSG_ID, MEDIA_IDX)
    ctrl.on_media_batch_download([key])
    ctrl.on_media_download_all_pending()
    assert got == {
        "keys": [key],
        "all": {
            "channel_id": CHANNEL_ID,
            "search": "photo",
            "media_type": MediaType.PHOTO,
        },
    }


def test_status_filter_has_undownloaded_option(widget: MediaManagerWidget) -> None:
    """2026-11-22:PENDING 枚举删除后,状态下拉必须有「未下载」哨兵档。"""
    cmb = widget.cmb_status
    data = [cmb.itemData(i) for i in range(cmb.count())]
    assert data[0] is None, "首项仍是「全部状态」(不过滤)"
    assert UNDOWNLOADED_FILTER in data, "必须有「未下载」档"
    # 三个已启动下载态仍在,且 PENDING 不再作为枚举被遍历出来。
    assert set(MediaDownloadStatus) == {
        MediaDownloadStatus.DOWNLOADING,
        MediaDownloadStatus.DONE,
        MediaDownloadStatus.FAILED,
    }
    assert set(data[2:]) == set(MediaDownloadStatus)


def test_refresh_translates_undownloaded_sentinel() -> None:
    """状态下拉「未下载」哨兵 → controller 翻成 undownloaded=True。"""
    from types import SimpleNamespace  # noqa: PLC0415

    from tgmonitor.ui.controllers.media_actions_controller import (  # noqa: PLC0415
        MediaActionsController,
    )

    got: dict[str, object] = {}

    def fake_load(**kwargs: object) -> None:
        got.update(kwargs)

    f = {
        "channel_id": None,
        "status": UNDOWNLOADED_FILTER,
        "media_type": None,
        "search": "",
        "sort": SortKey.DATE,
        "sort_dir": SortDir.DESC,
        "offset": 0,
        "limit": 50,
        "total": 0,
    }
    ctx = SimpleNamespace(
        media_manager=SimpleNamespace(
            set_monitored_channels=lambda _channels: None,
            current_filters=lambda: f,
        ),
        vm=SimpleNamespace(load_media_list=fake_load, monitored_channels={}),
    )
    MediaActionsController(ctx).on_media_refresh()
    assert got["undownloaded"] is True
    assert got["status"] is None
