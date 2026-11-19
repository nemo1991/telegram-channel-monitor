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
from tgmonitor.core.config import Settings  # noqa: E402
from tgmonitor.ui.widgets.login_dialog import LoginDialog  # noqa: E402
from tgmonitor.ui.widgets.message_manager_widget import (  # noqa: E402
    MessageManagerWidget,
)
from tgmonitor.ui.widgets.settings_page import SettingsPage  # noqa: E402


def _settings_page(qapp, tmp_path, *, phone: str = "+8613800000000") -> SettingsPage:
    app = MagicMock()
    app.settings = Settings.for_test(phone=phone)
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


# ---- FR-01:账户凭证登录入口 ----


def test_login_logout_live_in_session_row_and_default_removed(qapp, tmp_path) -> None:
    """登录/登出占用原「默认」按钮位置;账户凭证分组不再有「默认」。"""
    page = _settings_page(qapp, tmp_path)
    row_w = page.in_session_dir.parentWidget()
    assert row_w is not None
    layout = row_w.layout()
    assert isinstance(layout, QHBoxLayout)

    buttons = [layout.itemAt(i).widget() for i in range(layout.count())]
    buttons = [w for w in buttons if isinstance(w, QPushButton)]
    assert page.btn_login in buttons
    assert page.btn_logout in buttons
    assert [w.text() for w in buttons if w is not page.btn_login and w is not page.btn_logout] == [
        "浏览…"
    ]

    # 凭证分组内不应再出现「默认」按钮
    texts = [b.text() for b in page._account_gb.findChildren(QPushButton)]
    assert "默认" not in texts
    assert page.btn_login.text() == "登录"  # 无「…」
    page.deleteLater()


def test_settings_login_passes_credential_phone(qapp, tmp_path) -> None:
    """点登录:填在凭证表单里的手机号直接交给 LoginDialog。"""
    page = _settings_page(qapp, tmp_path, phone="+8613900000000")
    with patch("tgmonitor.ui.widgets.login_dialog.LoginDialog") as dlg_cls:
        page.btn_login.click()
    dlg_cls.assert_called_once()
    assert dlg_cls.call_args.kwargs["phone"] == "+8613900000000"
    page.deleteLater()


def test_login_dialog_auto_submits_credential_phone(qapp, qloop) -> None:
    """传入手机号后自动提交,不再停在手机号输入页。"""
    app = MagicMock()
    app.settings.phone = ""
    app.client.state = "phone_required"
    app.auth.submit_phone = AsyncMock(return_value=("code_required", None))
    dlg = LoginDialog(app=app, loop=qloop, phone="+8613800000000")
    _wait_until(lambda: app.auth.submit_phone.called)
    # 用 called 而非 awaited:coroutine 在 main thread 创建时即登记 call,
    # await 发生在共享 loop 线程,断言 await_count 有竞态。
    app.auth.submit_phone.assert_called_once_with("+8613800000000")
    dlg.deleteLater()


def test_login_dialog_without_phone_keeps_manual_page(qapp, qloop) -> None:
    """未传手机号时保持旧的手动输入行为(主界面直接登录可用)。"""
    app = MagicMock()
    app.settings.phone = ""
    app.client.state = "phone_required"
    app.auth.submit_phone = AsyncMock(return_value=("code_required", None))
    dlg = LoginDialog(app=app, loop=qloop)
    assert dlg.stack.currentIndex() == 0
    assert dlg.in_phone.text() == ""
    app.auth.submit_phone.assert_not_called()
    dlg.deleteLater()


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
    """筛选栏条件原样透传给 core:关键词 / 收藏 / 标签 / 日期 / limit。"""
    win = build_real_main_window(qloop, tmp_path)
    mm = win.message_manager
    mm.edit_search.setText("hello")
    mm.chk_favorite.setChecked(True)
    mm.chk_tagged.setChecked(True)
    mm.chk_date.setChecked(True)

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
    assert kwargs["favorite_only"] is True
    assert kwargs["tag_only"] is True
    assert kwargs["include_unmonitored"] is True
    assert kwargs["limit"] == 500
    assert kwargs["date_from"] is not None and kwargs["date_to"] is not None
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
    mm.table.selectRow(0)
    detail = mm.detail.toPlainText()
    assert "完整正文 content" in detail
    assert "alpha" in detail
    assert "备注文本" in detail
    assert "pic.jpg" in detail
    assert "★" in mm.table.item(0, 4).text()
    win.close()
