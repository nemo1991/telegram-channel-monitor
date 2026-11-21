"""v2.0.8 UI 契约测试:账户凭证登录入口 / 左栏辨识度 / 消息管理页。

均为静态 + offscreen 断言,不依赖真实 Telegram 账号;沿用项目的
qapp / qloop / build_real_main_window 约定(见 tests/fixtures/_qt_runtime.py)。
"""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QHBoxLayout, QPushButton  # noqa: E402

from tests.fixtures._factories import make_message  # noqa: E402
from tests.fixtures._real_window import build_real_main_window  # noqa: E402
from tgmonitor.core.auth_service import AuthService  # noqa: E402
from tgmonitor.core.config import Settings  # noqa: E402
from tgmonitor.core.events import EventBus  # noqa: E402
from tgmonitor.core.settings_store import parse_env_file  # noqa: E402
from tgmonitor.core.telegram.fake_client import FakeTelegramClient  # noqa: E402
from tgmonitor.ui.widgets.message_manager_widget import (  # noqa: E402
    MessageManagerWidget,
)
from tgmonitor.ui.widgets.settings_page import SettingsPage  # noqa: E402


def _settings_page(qapp, tmp_path) -> SettingsPage:
    app = MagicMock()
    app.settings = Settings.for_test()
    app.client.state = "uninit"
    return SettingsPage(app=app, loop=MagicMock(), env_path=tmp_path / ".env")


def _wait_until(pred, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        QApplication.processEvents()
        time.sleep(0.02)
    raise AssertionError("pred() 超时")


# ---- FR-01:账户凭证二维码登录入口(本版本唯一登录方式)----


def test_qr_login_logout_in_session_row_default_removed(qapp, tmp_path) -> None:
    """二维码登录/登出占用原「默认」按钮位置;不再有手机号「登录」按钮。"""
    page = _settings_page(qapp, tmp_path)
    row_w = page.in_session_dir.parentWidget()
    assert row_w is not None
    layout = row_w.layout()
    assert isinstance(layout, QHBoxLayout)

    buttons = [layout.itemAt(i).widget() for i in range(layout.count())]
    buttons = [w for w in buttons if isinstance(w, QPushButton)]
    assert page.btn_qr_login in buttons
    assert page.btn_logout in buttons
    assert not hasattr(page, "btn_login")  # 手机号登录入口已移除
    assert [w.text() for w in buttons if w not in (page.btn_qr_login, page.btn_logout)] == ["浏览…"]
    assert page.btn_qr_login.text() == "二维码登录"
    page.deleteLater()


def test_settings_qr_login_saves_credentials_and_requests_qr(qapp, qloop, tmp_path) -> None:
    """点二维码登录:校验 → 保存 api 凭据 → 真调 start_qr_login → 弹扫码框。"""
    bus = EventBus()
    client = FakeTelegramClient()
    settings = Settings.for_test(api_id=1, api_hash="x" * 32)
    auth = AuthService(bus=bus, client=client, settings=settings)
    app = MagicMock()
    app.settings = settings
    app.client = client
    app.auth = auth
    app.bus = bus

    page = SettingsPage(app=app, loop=qloop, env_path=tmp_path / ".env")
    page.in_api_id.setValue(4242)
    page.in_api_hash.setText("y" * 32)

    with patch("tgmonitor.ui.widgets.qr_login_dialog.QrLoginDialog") as dlg_cls:
        page.btn_qr_login.click()
        _wait_until(lambda: dlg_cls.called)

    # 只写 api 凭据,不再产出 TG_PHONE
    pairs = parse_env_file(tmp_path / ".env").pairs
    assert pairs["TG_API_ID"] == "4242"
    assert pairs["TG_API_HASH"] == "y" * 32
    assert "TG_PHONE" not in pairs
    assert client.state == "qr_required"  # 真走到二维码待扫码
    assert dlg_cls.call_args.args[2] == "tg://login?token=fake-test-token"
    page.deleteLater()


def test_settings_qr_login_rejects_invalid_api_credentials(qapp, qloop, tmp_path) -> None:
    """api 凭据校验失败:不写盘、不生成二维码、不弹框。"""
    app = MagicMock()
    app.settings = Settings.for_test(api_id=1, api_hash="x" * 32)
    app.client.state = "uninit"
    app.auth.start_qr_login = AsyncMock()
    page = SettingsPage(app=app, loop=qloop, env_path=tmp_path / ".env")
    page.in_api_id.setValue(0)
    page.in_api_hash.setText("short")

    with (
        patch("tgmonitor.ui.widgets.settings_page.QMessageBox.warning") as warn,
        patch("tgmonitor.ui.widgets.qr_login_dialog.QrLoginDialog") as dlg_cls,
    ):
        page.btn_qr_login.click()
    assert warn.called
    assert "TG_API_ID" in str(warn.call_args)
    app.auth.start_qr_login.assert_not_awaited()
    dlg_cls.assert_not_called()
    assert not (tmp_path / ".env").exists()
    page.deleteLater()


# ---- FR-02:左栏辨识度 ----


def test_nav_bar_dark_background_and_text_contrast(qapp, qloop, tmp_path) -> None:
    """nav 自身有底色,idle 前景与底色亮度差足够大。"""
    from tgmonitor.ui.nav_bar import _palette

    win = build_real_main_window(qloop, tmp_path)
    p = _palette()
    assert win.nav.backgroundRole() != 0  # 有 backgroundRole
    assert QColor(p["nav_bg"]).lightness() < 80
    assert QColor(p["inactive_fg"]).lightness() - QColor(p["nav_bg"]).lightness() > 120
    assert QColor(p["active_fg"]).lightness() > QColor(p["active_bg"]).lightness()
    assert p["active_bg"] != p["nav_bg"]
    win.close()


def test_nav_icons_fully_visible_without_left_border(qapp, qloop, tmp_path) -> None:
    """去掉 3px 左边框后图标容器 28px、图标 24px,文字与按钮等宽不被裁。"""
    win = build_real_main_window(qloop, tmp_path)
    for btn in win.nav._buttons:
        assert "border-left" not in btn.styleSheet()
        ico = btn._ico_label
        assert ico.width() >= 28 and ico.pixmap().width() <= ico.width()
        assert btn._txt_label.width() == btn.width()
        assert ico.x() >= 0 and ico.x() + ico.width() <= btn.width()
    win.close()


# ---- FR-03:消息管理页 ----


def test_message_manager_page_wired_in_nav(qapp, qloop, tmp_path) -> None:
    """导航 4 项 / stack 4 页;索引 1 = 消息管理页。"""
    from tgmonitor.ui.nav_bar import _NAV_LABELS_SRC

    win = build_real_main_window(qloop, tmp_path)
    assert _NAV_LABELS_SRC == ("大盘", "消息", "媒体", "设置")
    assert len(win.nav._buttons) == 4
    assert win.stack.count() == 4
    assert isinstance(win.stack.widget(1), MessageManagerWidget)
    assert win.message_manager is win.stack.widget(1)
    win.nav.set_current(1)
    QApplication.processEvents()
    assert win.stack.currentIndex() == 1
    win.close()


def test_message_manager_query_passes_filters_to_core(qapp, qloop, tmp_path) -> None:
    """筛选栏条件原样透传给 core:关键词 / 日期预设 / 全量 limit=None。"""
    win = build_real_main_window(qloop, tmp_path)
    mm = win.message_manager
    mm.edit_search.setText("hello")
    idx = mm.cmb_date.findData("7d")
    mm.cmb_date.setCurrentIndex(idx)

    msgs = [
        make_message(channel_id=100, msg_id=7, text="hello world", tags=["tag"], is_favorite=True)
    ]
    list_messages = AsyncMock(return_value=msgs)
    mm._app.messages.list_messages = list_messages

    mm.refresh()
    _wait_until(lambda: mm.table.rowCount() == 1)
    # 窗口 bootstrap 也会调 list_messages,取带本测试关键词的那次调用
    calls = [c for c in list_messages.call_args_list if c.kwargs.get("search") == "hello"]
    assert len(calls) == 1
    kwargs = calls[0].kwargs
    assert kwargs["search"] == "hello"
    assert kwargs["include_unmonitored"] is True
    assert kwargs["limit"] is None  # v2.0.9:全量查询,UI 本地分页
    assert kwargs["date_from"] is not None and kwargs["date_to"] is not None
    # v2.0.9:收藏/标签/置顶筛选已移除 —— 不再透传这三个参数
    assert "favorite_only" not in kwargs
    assert "tag_only" not in kwargs
    assert "pinned_only" not in kwargs
    assert mm.table.item(0, 3).text() == "hello world"
    win.close()


def test_message_manager_row_selection_shows_detail(qapp, qloop, tmp_path) -> None:
    """选中结果行 → 详情区显示完整正文 / 标签 / 媒体名。"""
    win = build_real_main_window(qloop, tmp_path)
    mm = win.message_manager
    from tgmonitor.core.dto import MediaDTO, MediaType

    msg = make_message(
        channel_id=100,
        msg_id=9,
        text="完整正文 content",
        tags=["alpha"],
        notes="备注文本",
        is_favorite=True,
        media=[
            MediaDTO(
                type=MediaType.PHOTO,
                mime_type="image/jpeg",
                file_name="pic.jpg",
                file_size=100,
            )
        ],
    )
    mm._apply_messages([msg])
    assert mm.table.rowCount() == 1
    assert mm.table.columnCount() == 4  # v2.0.9:已删「标记」列
    mm.table.selectRow(0)
    detail = mm.detail.toPlainText()
    assert "完整正文 content" in detail
    assert "alpha" in detail
    assert "备注文本" in detail
    assert "pic.jpg" in detail
    win.close()


# ---- v2.0.9:左栏二次修正 / 消息页筛选与分页 ----


def test_nav_v209_colors_and_pill(qapp, qloop, tmp_path) -> None:
    """idle 中亮暖灰、hover 与 active 同蓝系(色差小)、选中态圆角背景。"""
    from tgmonitor.ui.nav_bar import _LIGHT

    # v2.1.0:idle 从纯白改中亮暖灰 —— 与深底仍有足够亮度差,但不是纯白
    idle_l = QColor(_LIGHT["inactive_fg"]).lightness()
    assert 150 <= idle_l <= 220  # 中亮,非纯白(255)
    assert idle_l - QColor(_LIGHT["nav_bg"]).lightness() > 110
    # hover 与 active 同色系:色相接近(都落在蓝色区间 200~230),色差小
    h_hue = QColor(_LIGHT["hover_bg"]).hue()
    a_hue = QColor(_LIGHT["active_bg"]).hue()
    assert h_hue >= 0 and a_hue >= 0
    assert abs(h_hue - a_hue) <= 10
    assert QColor(_LIGHT["hover_bg"]).lightness() < QColor(_LIGHT["active_bg"]).lightness()
    assert int(_LIGHT["pill_radius"]) >= 6  # 圆角胶囊

    win = build_real_main_window(qloop, tmp_path)
    active = win.nav._buttons[0]
    assert "border-radius" in active._bg.styleSheet()
    assert _LIGHT["active_bg"].lower() in active._bg.styleSheet().lower()
    win.close()


def test_nav_v210_paints_background_not_via_qss(qapp, qloop, tmp_path) -> None:
    """v2.1.0:nav 底色靠 paintEvent 自绘,不依赖 QSS/palette background 跨平台解析。"""
    from tgmonitor.ui.nav_bar import VerticalNavBar

    assert "paintEvent" in VerticalNavBar.__dict__  # 自身实现(非继承)
    win = build_real_main_window(qloop, tmp_path)
    # grab 出的像素确实是深色底 —— 即便真机 QSS 不生效也由 QPainter 保证
    img = win.nav.grab().toImage()
    assert QColor(img.pixelColor(2, 2)).name() == "#1e2433"
    win.close()


def test_message_pager_row_above_detail(qapp, qloop, tmp_path) -> None:
    """v2.1.0:条数信息与分页按钮同一行,该行位于详情区上方。"""
    from PySide6.QtWidgets import QHBoxLayout

    win = build_real_main_window(qloop, tmp_path)
    mm = win.message_manager
    # 四者共享同一父布局(pager 行)
    parents = {
        "status": mm.lbl_status.parentWidget(),
        "prev": mm.btn_prev.parentWidget(),
        "page": mm.lbl_page.parentWidget(),
        "next": mm.btn_next.parentWidget(),
    }
    assert len(set(parents.values())) == 1  # 同一个 parent widget
    # pager 是嵌套在详情 pane 的 QVBoxLayout 里的 QHBoxLayout —— 找到它
    parent = mm.lbl_status.parentWidget()
    top_lay = parent.layout()
    hbox = next(
        top_lay.itemAt(i).layout()
        for i in range(top_lay.count())
        if isinstance(top_lay.itemAt(i).layout(), QHBoxLayout)
    )
    widgets = {hbox.itemAt(i).widget() for i in range(hbox.count())}
    assert {mm.lbl_status, mm.btn_prev, mm.lbl_page, mm.btn_next} <= widgets
    # 该行在详情文本框之上:top_lay 里 pager 布局的索引 < detail 的索引
    # (窗口未 show 时几何坐标恒为 0,用布局顺序判更稳)
    pager_idx = next(i for i in range(top_lay.count()) if top_lay.itemAt(i).layout() is hbox)
    detail_idx = next(i for i in range(top_lay.count()) if top_lay.itemAt(i).widget() is mm.detail)
    assert pager_idx < detail_idx
    win.close()


def test_message_date_custom_toggles_date_edits(qapp, qloop, tmp_path) -> None:
    """日期预设:默认两个日期框隐藏,选「自定义…」才显示。"""
    win = build_real_main_window(qloop, tmp_path)
    mm = win.message_manager
    assert [mm.cmb_date.itemData(i) for i in range(mm.cmb_date.count())] == [
        "all",
        "today",
        "7d",
        "30d",
        "custom",
    ]
    # 用 isHidden() 判显式显隐 —— 窗口未 show 时 isVisible() 恒为 False
    assert mm.date_from.isHidden() and mm.date_to.isHidden()
    idx = mm.cmb_date.findData("custom")
    mm.cmb_date.setCurrentIndex(idx)
    QApplication.processEvents()
    assert not mm.date_from.isHidden() and not mm.date_to.isHidden()
    # 自定义区间透传给 core
    start, end = mm._selected_dates()
    assert start is not None and end is not None and start < end
    mm.cmb_date.setCurrentIndex(0)
    assert mm._selected_dates() == (None, None)
    win.close()


def test_message_filters_and_query_buttons_uniform(qapp, qloop, tmp_path) -> None:
    """v2.0.9:标注筛选已移除;查询/重置按钮同尺寸。"""
    win = build_real_main_window(qloop, tmp_path)
    mm = win.message_manager
    assert not hasattr(mm, "chk_favorite")
    assert not hasattr(mm, "chk_tagged")
    assert not hasattr(mm, "chk_pinned")
    assert mm.btn_query.size() == mm.btn_reset.size()
    win.close()


def test_message_pagination_over_all_rows(qapp, qloop, tmp_path) -> None:
    """全量结果按每页 50 条切片,上一页/下一页可翻,总数来自全量。"""
    from tgmonitor.ui.widgets.message_manager_widget import _PAGE_SIZE

    win = build_real_main_window(qloop, tmp_path)
    mm = win.message_manager
    msgs = [make_message(channel_id=100, msg_id=i, text=f"msg-{i}") for i in range(_PAGE_SIZE + 5)]
    mm._apply_messages(msgs)

    assert mm.table.rowCount() == _PAGE_SIZE
    assert mm.lbl_page.text() == "1 / 2"
    assert "共 55 条消息" in mm.lbl_status.text()
    assert not mm.btn_prev.isEnabled() and mm.btn_next.isEnabled()

    mm._on_page_next()
    assert mm.lbl_page.text() == "2 / 2"
    assert mm.table.rowCount() == 5
    assert mm.btn_prev.isEnabled() and not mm.btn_next.isEnabled()

    mm._on_page_prev()
    assert mm.lbl_page.text() == "1 / 2"
    assert mm.table.rowCount() == _PAGE_SIZE

    # 查询重置回第一页
    mm._page = 1
    mm._apply_messages(msgs)
    assert mm._page == 0
    win.close()
