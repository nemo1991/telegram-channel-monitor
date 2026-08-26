"""MediaManagerWidget 单元测试(2026-08-25 v1.3.0 PR #6)— 排序 / 分页交互。

QT_QPA_PLATFORM=offscreen 无 GUI 跑;只测 widget 内部逻辑:
- filter bar 新增的 sort / dir combo / page nav 控件
- current_filters 透传 sort/sort_dir/offset
- on_media_loaded 接收 `(rows, total)` tuple 后更新 _total + 翻页按钮状态
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from tgmonitor.core.dto import (
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    SortDir,
    SortKey,
)
from tgmonitor.ui.widgets.media_manager_widget import MediaManagerWidget


@pytest.fixture
def qt_app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app  # type: ignore[return-value]


@pytest.fixture
def widget(qt_app: QApplication) -> MediaManagerWidget:
    w = MediaManagerWidget()
    return w


def _msg(channel_id: int, msg_id: int, media: list[MediaDTO]) -> MessageDTO:
    return MessageDTO(
        id=0, channel_id=channel_id, telegram_msg_id=msg_id, text="", media=media,
    )


def _done(file_size: int = 1024) -> MediaDTO:
    return MediaDTO(
        type=MediaType.PHOTO, mime_type="image/jpeg", file_name="x.jpg",
        file_size=file_size, telegram_file_id="fid",
        object_key="media/x.jpg", object_backend="local",
        download_status=MediaDownloadStatus.DONE,
    )


def test_widget_has_sort_and_dir_combos(widget: MediaManagerWidget) -> None:
    """PR #6:filter bar 加 Sort / Dir / Page nav。"""
    assert hasattr(widget, "cmb_sort")
    assert hasattr(widget, "cmb_dir")
    assert hasattr(widget, "btn_prev")
    assert hasattr(widget, "btn_next")
    assert hasattr(widget, "lbl_page")


def test_widget_default_sort_is_date_desc(widget: MediaManagerWidget) -> None:
    """PR #6:默认 sort=DATE / sort_dir=DESC(v1.2.0 行为保留)。

    注:PySide6 `addItem(label, data)` 配合 str-based Enum(`SortKey(str, Enum)`)
    会把 user data 转回 `str` — 所以 currentData() 返 `'date'` 而不是 SortKey
    enum。widget.current_filters 内部走 isinstance(SortKey) 兜底转。
    """
    assert widget.cmb_sort.currentData() == SortKey.DATE.value  # 'date'
    assert widget.cmb_dir.currentData() == SortDir.DESC.value  # 'desc'


def test_widget_default_sort_resolves_to_date_via_filter_helper(
    widget: MediaManagerWidget,
) -> None:
    """PR #6:current_filters 在 currentData 返字符串时仍能解析回 SortKey enum。"""
    f = widget.current_filters()
    assert f["sort"] == SortKey.DATE
    assert f["sort_dir"] == SortDir.DESC


def test_current_filters_includes_sort_dir_offset(widget: MediaManagerWidget) -> None:
    """PR #6:current_filters 多 3 字段,VM.load_media_list 用。"""
    f = widget.current_filters()
    assert "sort" in f and f["sort"] == SortKey.DATE
    assert "sort_dir" in f and f["sort_dir"] == SortDir.DESC
    assert f["offset"] == 0  # 初始 page=0 → offset=0
    assert f["total"] == 0  # 初始 total=0


def test_on_media_loaded_accepts_tuple_payload(
    widget: MediaManagerWidget, qt_app: QApplication,
) -> None:
    """PR #6:on_media_loaded 接收 `(rows, total)` tuple。"""
    msg = _msg(100, 1, [_done()])
    widget.on_media_loaded(([(msg, 0, _done())], 5))
    assert len(widget._rows) == 1
    assert widget._total == 5
    assert widget.lbl_page.text() == "1 / 1"
    qt_app.processEvents()


def test_page_nav_disables_buttons_at_boundaries(
    widget: MediaManagerWidget, qt_app: QApplication,
) -> None:
    """PR #6:5 条 media + page_size=2 → 3 页;page 1 / 3 翻页按钮状态正确。"""
    rows = [(_msg(100, i, [_done()]), 0, _done()) for i in range(5)]
    # 把 page_size 调到 2 便于测试边界
    widget._page_size = 2
    # Page 1(5 条 / 2 = 3 页)— page=0,btn_prev disabled,btn_next enabled
    widget.on_media_loaded((rows, 5))
    assert widget._page == 0
    assert not widget.btn_prev.isEnabled()
    assert widget.btn_next.isEnabled()
    # Page 2
    widget._on_page_next()
    assert widget._page == 1
    assert widget.btn_prev.isEnabled()
    assert widget.btn_next.isEnabled()
    # Page 3
    widget._on_page_next()
    assert widget._page == 2
    assert widget.btn_prev.isEnabled()
    assert not widget.btn_next.isEnabled()
    # 再 next 不动
    widget._on_page_next()
    assert widget._page == 2
    qt_app.processEvents()


def test_sort_change_resets_page_to_zero(
    widget: MediaManagerWidget, qt_app: QApplication,
) -> None:
    """PR #6:切 sort / dir combo → 翻回 page=0(filter 变化不应保留旧 page)。"""
    rows = [(_msg(100, i, [_done()]), 0, _done()) for i in range(5)]
    widget._page_size = 2
    widget.on_media_loaded((rows, 5))
    widget._on_page_next()  # 跳到 page 2
    assert widget._page == 1

    # 模拟 sort combo 触发 refresh(emit)
    widget.cmb_sort.setCurrentIndex(1)  # SIZE
    qt_app.processEvents()
    assert widget._page == 0


def test_refresh_signal_emitted_on_sort_change(
    widget: MediaManagerWidget, qt_app: QApplication,
) -> None:
    """PR #6:sort / dir combo 变化 → emit refresh_requested。"""
    emitted: list[int] = []
    widget.refresh_requested.connect(lambda: emitted.append(1))

    widget.cmb_sort.setCurrentIndex(1)
    qt_app.processEvents()
    widget.cmb_dir.setCurrentIndex(1)  # ASC
    qt_app.processEvents()

    assert len(emitted) >= 2


def test_page_next_emits_refresh(widget: MediaManagerWidget, qt_app: QApplication) -> None:
    """PR #6:点下一页 → emit refresh(VM 带新 offset 拉数据)。"""
    rows = [(_msg(100, i, [_done()]), 0, _done()) for i in range(5)]
    widget._page_size = 2
    widget.on_media_loaded((rows, 5))

    emitted: list[int] = []
    widget.refresh_requested.connect(lambda: emitted.append(1))
    widget._on_page_next()
    qt_app.processEvents()
    assert len(emitted) == 1


def test_total_zero_disables_page_nav(widget: MediaManagerWidget, qt_app: QApplication) -> None:
    """PR #6:total=0 时翻页按钮都禁用(避免空翻页)。"""
    widget.on_media_loaded(([], 0))
    assert not widget.btn_prev.isEnabled()
    assert not widget.btn_next.isEnabled()
    qt_app.processEvents()


def test_total_updates_label_correctly(
    widget: MediaManagerWidget, qt_app: QApplication,
) -> None:
    """PR #6:`lbl_page` 显示 `current / total_pages`。"""
    rows = [(_msg(100, i, [_done()]), 0, _done()) for i in range(7)]
    widget._page_size = 3  # 7 / 3 = 3 页(ceil)
    widget.on_media_loaded((rows, 7))
    assert widget.lbl_page.text() == "1 / 3"
    widget._on_page_next()
    assert widget.lbl_page.text() == "2 / 3"
    widget._on_page_next()
    assert widget.lbl_page.text() == "3 / 3"
    qt_app.processEvents()


# ---- 2026-08-25 v1.3.0 PR #7:Export CSV button -------------------------


def test_widget_has_export_csv_button(widget: MediaManagerWidget) -> None:
    """PR #7:toolbar 加 Export CSV 按钮 + `export_csv_requested` 信号。"""
    assert hasattr(widget, "btn_export_csv")
    assert hasattr(widget, "export_csv_requested")


def test_export_csv_emits_path_when_dialog_confirmed(
    widget: MediaManagerWidget, qt_app: QApplication, monkeypatch,
) -> None:
    """PR #7:点 Export CSV → QFileDialog.getSaveFileName 选路径 → emit
    `export_csv_requested(out_path)`。
    """
    from PySide6.QtWidgets import QFileDialog

    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: ("/tmp/test-export.csv", "CSV files (*.csv)")),
    )
    captured: list[str] = []
    widget.export_csv_requested.connect(lambda p: captured.append(p))

    widget._on_export_csv()
    qt_app.processEvents()
    assert captured == ["/tmp/test-export.csv"]


def test_export_csv_does_nothing_when_dialog_cancelled(
    widget: MediaManagerWidget, qt_app: QApplication, monkeypatch,
) -> None:
    """PR #7:用户 Cancel → 不 emit(空 path)。"""
    from PySide6.QtWidgets import QFileDialog

    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: ("", "")),
    )
    captured: list[str] = []
    widget.export_csv_requested.connect(lambda p: captured.append(p))

    widget._on_export_csv()
    qt_app.processEvents()
    assert captured == []


# 显式 import,避免 ruff F401
_ = Qt