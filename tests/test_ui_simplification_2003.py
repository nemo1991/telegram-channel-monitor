"""v2.0.3 UI 减法契约 — D1/D2 状态栏、D3 头栏/设置页、D4/D6 大盘。

2026-11-17:先写契约(红)再改实现(绿)。守护的验收点见
`.workbuddy/plans/v2.0.3-产品规划.md` FR-01~FR-06 / FR-08 / FR-10 / FR-11。

B 类守护文件(`test_status_bar_layout.py` / `test_live_readonly_2002.py`)不在这里
重建,只在原文件更新断言。
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import tgmonitor.ui.widgets.status_bar as status_bar_mod  # noqa: E402
from tgmonitor.core.dto import ChannelDTO  # noqa: E402
from tgmonitor.core.events import (  # noqa: E402
    ErrorOccurred,
    EventBus,
    LoginStateChanged,
    MessageReceived,
    SettingsChanged,
)
from tgmonitor.core.monitor.service import MonitorService  # noqa: E402
from tgmonitor.ui.controllers.app_state_controller import AppStateController  # noqa: E402
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget  # noqa: E402
from tgmonitor.ui.widgets.header_bar import HeaderBar  # noqa: E402
from tgmonitor.ui.widgets.settings_page import SettingsPage  # noqa: E402
from tgmonitor.ui.widgets.status_bar import StatusBar, _UnifiedStateLabel  # noqa: E402

# ---------------------------------------------------------------- D1/D2


def _status_bar(qapp: QApplication) -> StatusBar:
    return StatusBar(SimpleNamespace(bus=EventBus()), None)


def test_status_bar_drops_backend_and_last_message(qapp: QApplication) -> None:
    """D1:剔除 DB:OS 开发态信息与「最后消息时间」(与大盘重复)。"""
    sb = _status_bar(qapp)
    assert not hasattr(status_bar_mod, "_BackendLabel")
    assert not hasattr(status_bar_mod, "_LastMessageLabel")
    assert sb.findChild(QLabel, "statusBackendLabel") is None
    assert sb.findChild(QLabel, "statusLastMessageLabel") is None
    assert not hasattr(sb, "set_backend_label")
    assert not hasattr(sb, "set_last_message_time")
    sb.deleteLater()


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("uninit", "启动中"),
        ("phone_required", "未登录"),
        ("code_required", "需验证码"),
        ("email_required", "需邮箱地址"),
        ("email_code_required", "需邮箱验证码"),
        ("registration_required", "需注册"),
        ("password_required", "需 2FA"),
        ("error", "错误"),
        ("closed", "会话关闭"),
        ("logging_out", "登出中"),
        ("closing", "关闭中"),
    ],
)
def test_unified_state_login_states(qapp: QApplication, state: str, expected: str) -> None:
    """D2:12 态登录状态映射(除 ready 走连接态)在底部单一状态位显示。"""
    sb = _status_bar(qapp)
    sb.set_login_state(state)
    text = sb.findChild(_UnifiedStateLabel).text()
    assert expected in text
    assert "TG 已连接" not in text
    sb.deleteLater()


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("waiting_for_network", "TG 等待网络"),
        ("connecting", "TG 连接中"),
        ("updating", "TG 同步中"),
        ("ready", "TG 已连接"),
        ("unknown", "TG 状态未知"),
    ],
)
def test_unified_state_connection_states_when_ready(
    qapp: QApplication, state: str, expected: str
) -> None:
    """D2:已登录时显示 5 态连接状态要点。"""
    sb = _status_bar(qapp)
    sb.set_login_state("ready")
    sb.set_connection_state(state)
    assert expected in sb.findChild(_UnifiedStateLabel).text()
    sb.deleteLater()


def test_unified_state_login_takes_priority(qapp: QApplication) -> None:
    """D2:未 ready 的登录态优先于任何连接态,避免误判「在收消息」。"""
    sb = _status_bar(qapp)
    sb.set_connection_state("ready")
    sb.set_login_state("code_required")
    text = sb.findChild(_UnifiedStateLabel).text()
    assert "需验证码" in text
    assert "TG 已连接" not in text
    sb.deleteLater()


def test_unified_state_only_one_instance(qapp: QApplication) -> None:
    """D2:底部只有一个状态标签,不并列两个「状态」。"""
    sb = _status_bar(qapp)
    assert len(sb.findChildren(_UnifiedStateLabel)) == 1
    sb.deleteLater()


# ---------------------------------------------------------------- D3


def test_header_bar_is_title_only(qapp: QApplication) -> None:
    """D3:头栏保留纯标题(品牌锚点),状态与登录/登出按钮全部移除。"""
    header = HeaderBar()
    for name in ("state_dot", "state_label", "btn_action", "btn_logout"):
        assert not hasattr(header, name), f"HeaderBar 不应再持有 {name}"
    assert not hasattr(header, "update_state")
    header.deleteLater()


def _settings_page(tmp_path, *, app: MagicMock | None = None) -> tuple[SettingsPage, MagicMock]:
    from tgmonitor.core.config import Settings

    mock_app = app or MagicMock()
    mock_app.settings = Settings.for_test()
    return SettingsPage(app=mock_app, loop=MagicMock(), env_path=tmp_path / ".env"), mock_app


def test_settings_page_has_login_logout_entries(qapp, tmp_path) -> None:
    """D3:账户凭证分组提供登录/登出入口。"""
    page, _ = _settings_page(tmp_path)
    assert page.btn_login is not None
    assert page.btn_logout is not None
    page.deleteLater()


def test_auth_buttons_live_inside_credentials_group(qapp, tmp_path) -> None:
    """v2.0.4 FR-02:登录/登出入口归属「账户凭证」分组内部,不在块外。"""
    from PySide6.QtWidgets import QGroupBox

    page, _ = _settings_page(tmp_path)
    assert page.btn_login.parent() is not None
    assert page.btn_logout.parent() is not None

    def ancestors(widget):
        cur = widget
        while cur is not None:
            yield cur
            cur = cur.parent()

    credentials = [
        w
        for w in ancestors(page.btn_logout)
        if isinstance(w, QGroupBox) and "账户凭证" in w.title()
    ]
    assert credentials, "登出按钮不在『账户凭证』分组内"
    # 登录按钮共享同一归属
    assert any(w in credentials for w in ancestors(page.btn_login))
    page.deleteLater()


def test_settings_page_initial_state_from_client(qapp, tmp_path) -> None:
    """首次打开设置页即按 core 真实状态显示单一入口,不同时出现两个按钮。"""
    _page, mock_app = _settings_page(tmp_path)
    mock_app.client = SimpleNamespace(state="ready")
    page = SettingsPage(app=mock_app, loop=MagicMock(), env_path=tmp_path / ".env2")
    assert page.btn_login.isHidden() is True
    assert page.btn_logout.isHidden() is False
    page.deleteLater()


@pytest.mark.parametrize(
    ("state", "login_visible", "logout_visible"),
    [
        ("ready", False, True),
        ("uninit", True, False),
        ("phone_required", True, False),
        ("error", True, False),
    ],
)
def test_settings_page_login_state_switch(
    qapp, tmp_path, state: str, login_visible: bool, logout_visible: bool
) -> None:
    """D3:设置页入口随登录态切换,头栏不再承担该职责。"""
    page, _ = _settings_page(tmp_path)
    page.set_login_state(state)
    assert page.btn_login.isHidden() is not login_visible
    assert page.btn_logout.isHidden() is not logout_visible
    page.deleteLater()


def test_settings_page_login_opens_login_dialog(qapp, tmp_path) -> None:
    """D3:设置页登录按钮弹 LoginDialog(内部自动推进验证码/2FA)。"""
    page, mock_app = _settings_page(tmp_path)
    with patch("tgmonitor.ui.widgets.login_dialog.LoginDialog") as dlg_cls:
        page.btn_login.click()
        dlg_cls.assert_called_once()
    page.deleteLater()


def test_settings_page_logout_calls_auth_logout(qapp, tmp_path) -> None:
    """D3:设置页登出按钮复用 app.auth.logout()。"""
    page, mock_app = _settings_page(tmp_path)
    page.set_login_state("ready")  # 登出入口仅在已登录态可点
    # run_coro 被 patch → logout 返回值无人 await,用普通 Mock 避免协程泄漏警告
    mock_app.auth.logout = MagicMock()
    with patch("tgmonitor.ui.widgets.settings_page.run_coro") as run_coro:
        page.btn_logout.click()
        run_coro.assert_called_once()
    page.deleteLater()


def test_state_controller_drives_status_and_settings(qapp) -> None:
    """D2/D3:VM 与 EventBus 两条登录态路径都驱动底部状态位与设置页入口。"""
    from tests.test_app_state_controller import _make_ctx

    ctx, _ = _make_ctx()
    ctrl = AppStateController(ctx)
    ctrl.on_login_state("error")
    ctx.status_bar.set_login_state.assert_called_once_with("error")
    ctx.settings_page.set_login_state.assert_called_once_with("error")


@pytest.mark.asyncio
async def test_state_controller_bus_drives_status_and_settings(qapp) -> None:
    """D2/D3:EventBus 路径不再碰 header,改驱动底部状态位。"""
    from tests.test_app_state_controller import _make_ctx

    ctx, _ = _make_ctx()
    ctrl = AppStateController(ctx)
    await ctrl.on_bus_login(LoginStateChanged(state="ready"))
    ctx.status_bar.set_login_state.assert_called_once_with("ready")
    ctx.settings_page.set_login_state.assert_called_once_with("ready")


# ---------------------------------------------------------------- D4/D6


def _app_stub(joined_count: int = 7) -> MagicMock:
    from tgmonitor.core.config import Settings

    app = MagicMock()
    app.bus = EventBus()
    app.client = SimpleNamespace(state="ready", me=None)
    app.settings = Settings.for_test()
    app.storage.count_tg_subscribed = AsyncMock(return_value=joined_count)
    app.storage.aggregate_per_channel = AsyncMock(return_value={})
    return app


def _monitor_stub(ids: list[int]) -> MagicMock:
    monitor = MagicMock(spec=MonitorService)
    monitor.monitored_ids = ids
    return monitor


def _dashboard(qapp: QApplication, ids: list[int], channels: dict[int, ChannelDTO]):
    app = _app_stub()
    monitor = _monitor_stub(ids)
    dash = DashboardWidget(app, monitor, channels_provider=lambda: channels)
    return dash, app


def test_dashboard_has_no_action_buttons(qapp: QApplication) -> None:
    """D4:大盘不再承载刷新 / 导出 / 全量同步入口。"""
    dash, _ = _dashboard(qapp, [], {})
    assert not hasattr(dash, "btn_refresh")
    assert not hasattr(dash, "btn_export")
    assert not hasattr(dash, "btn_sync")
    assert not hasattr(dash, "on_refresh")
    assert not hasattr(dash, "on_export")
    assert not hasattr(dash, "on_sync_all")
    dash.deleteLater()


@pytest.mark.asyncio
async def test_recent_activity_ignores_message_received(qapp: QApplication) -> None:
    """D4/FR-05:最近活动不显示消息列表,其它事件仍记录。"""
    dash, _ = _dashboard(qapp, [], {})
    assert dash.activity_list.count() == 0

    await dash._on_bus_event(MessageReceived(message=None))
    assert dash.activity_list.count() == 0

    await dash._on_bus_event(ErrorOccurred(source="test", message="boom"))
    assert dash.activity_list.count() == 1
    dash.deleteLater()


@pytest.mark.asyncio
async def test_activity_row_height_covers_wrapped_text(qapp: QApplication) -> None:
    """v2.0.4 FR-01:行高按实际宽度算,长文本换行后上下不被裁。"""
    dash, _ = _dashboard(qapp, [], {})
    available = max(120, dash.activity_list.viewport().width())
    dash.activity_list.resize(available, 300)

    long_text = "已监听频道: " + "超长频道名称" * 20
    await dash._on_bus_event(SettingsChanged(what=long_text))

    item = dash.activity_list.item(0)
    widget = dash.activity_list.itemWidget(item)
    assert item.sizeHint().height() >= widget.height_for_width(available)
    # 关键回归:sizeHint 必须大于单行高度,否则多行被裁
    assert item.sizeHint().height() > 40
    dash.deleteLater()


@pytest.mark.asyncio
async def test_activity_timestamp_uses_event_time(qapp: QApplication) -> None:
    """v2.0.4 FR-01:活动条目时间取事件发生时刻,不是渲染时刻。"""
    from datetime import UTC, datetime

    dash, _ = _dashboard(qapp, [], {})
    past = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
    await dash._on_bus_event(SettingsChanged(what="x", occurred_at=past))
    widget = dash.activity_list.itemWidget(dash.activity_list.item(0))
    # UI 按本地时区展示(库内存 UTC),与事件时刻等价——不是渲染时刻。
    expected = past.astimezone().strftime("%H:%M:%S")
    assert widget.findChild(QLabel).text() == expected  # 2020 年的时刻,不可能是渲染时刻
    dash.deleteLater()


@pytest.mark.asyncio
async def test_joined_count_comes_from_tg_subscribed(qapp: QApplication) -> None:
    """D5/FR-06:已加入频道取 tg_subscribed 库计数,与监听数不同。"""
    dash, app = _dashboard(qapp, [11], {})
    assert await dash._fetch_joined_count() == 7
    app.storage.count_tg_subscribed.assert_awaited_once_with()
    dash.deleteLater()


@pytest.mark.asyncio
async def test_channel_stats_show_real_title_and_kind(qapp: QApplication) -> None:
    """D6/FR-08:频道列显示真实 title,类型列显示中文 kind,无 kind 留空。"""
    channels = {
        11: ChannelDTO(id=11, title="科技早报", kind="channel"),
        22: ChannelDTO(id=22, title="闲聊群", kind="supergroup"),
        33: ChannelDTO(id=33, title="", kind="basic_group"),
        44: ChannelDTO(id=44, title="无类型频道", kind=""),
    }
    dash, app = _dashboard(qapp, [11, 22, 33, 44], channels)
    app.storage.aggregate_per_channel = AsyncMock(
        return_value={11: SimpleNamespace(messages=3), 22: SimpleNamespace(messages=5)}
    )

    # v2.0.7 FR-01:total 仅供已删除的「消息总数」顶卡,现已从返回值移除。
    rows = await dash._fetch_channel_rows()
    assert rows[0] == ("科技早报", "频道", 3)
    assert rows[1] == ("闲聊群", "群组", 5)
    assert rows[2] == ("#33", "群组", 0)
    assert rows[3] == ("无类型频道", "", 0)

    dash._apply_stats_table(rows)
    assert dash.stats_table.item(0, 0).text() == "科技早报"
    assert dash.stats_table.item(0, 1).text() == "频道"
    assert dash.stats_table.item(3, 1).text() == ""
    dash.deleteLater()
