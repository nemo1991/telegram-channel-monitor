"""v2.0.3 UI 减法契约 — D1/D2 状态栏、D3 头栏/设置页、D4/D6 大盘。

2026-11-17:先写契约(红)再改实现(绿)。守护的验收点见
`.workbuddy/plans/v2.0.3-产品规划.md` FR-01~FR-06 / FR-08 / FR-10 / FR-11。

B 类守护文件(`test_status_bar_layout.py` / `test_live_readonly_2002.py`)不在这里
重建,只在原文件更新断言。
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.core.dto import ChannelDTO  # noqa: E402
from tgmonitor.core.events import (  # noqa: E402
    EventBus,
    LoginStateChanged,
)
from tgmonitor.core.monitor.service import MonitorService  # noqa: E402
from tgmonitor.ui.controllers.app_state_controller import AppStateController  # noqa: E402
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget  # noqa: E402
from tgmonitor.ui.widgets.settings_page import SettingsPage  # noqa: E402
from tgmonitor.ui.widgets.status_bar import StatusBar  # noqa: E402

# ---------------------------------------------------------------- D1/D2


def _status_bar(qapp: QApplication) -> StatusBar:
    return StatusBar(SimpleNamespace(bus=EventBus()), None)


# ---------------------------------------------------------------- D3


def _settings_page(
    tmp_path, *, app: MagicMock | None = None, loop=None
) -> tuple[SettingsPage, MagicMock]:
    from tgmonitor.core.config import Settings

    mock_app = app or MagicMock()
    mock_app.settings = Settings.for_test()
    return (
        SettingsPage(
            app=mock_app,
            loop=loop if loop is not None else MagicMock(),
            env_path=tmp_path / ".env",
        ),
        mock_app,
    )


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
