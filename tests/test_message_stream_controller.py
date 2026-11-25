"""`MessageStreamController` 单元测试 — 2026-10-04 plan 阶段 3.3。

覆盖 4 handler(`on_message_received` / `on_message_edited` /
`on_message_interactions_changed` / `on_bus_message_deleted`)— 用 mock
ctx(同 test_search_controller / test_live_selection_controller 模板)。

为什么用 mock:
- controller 不持有 MainWindow 引用,只通过 ctx 访问 `live_view` /
  `message_detail` / `vm` / `status_bar` — 测 controller 等价于测
  「正确的 ctx 字段被正确调用」
- 不需要 qapp:mock 对象无 Qt 元对象系统需求,避 offscreen paint race
- 与 test_live_view / test_main_window_message_event_* 互补:widget
  / EventBus 测试在更高层,本测试是 controller 编排逻辑回归守门
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tests.fixtures._factories import make_message  # noqa: E402
from tgmonitor.core.dto import ChannelDTO  # noqa: E402
from tgmonitor.core.events import MessageDeleted  # noqa: E402
from tgmonitor.ui.controllers._base import MainWindowCtx  # noqa: E402
from tgmonitor.ui.controllers.message_stream_controller import (  # noqa: E402
    MessageStreamController,
)


def _make_ctx(
    *,
    monitored_channels: dict[int, ChannelDTO] | None = None,
) -> tuple[MainWindowCtx, dict[str, MagicMock]]:
    """构造 MainWindowCtx + 暴露常用 mock 引用给测试体。

    命名引用 dict 让 test 用 `refs["live_view"].append.assert_called_with(m)`
    这种短链,不每次都写 `ctx.live_view.append.assert_called_with(m)`。
    """
    live_view = MagicMock(name="live_view")
    vm = MagicMock(name="vm")
    vm.monitored_channels = monitored_channels if monitored_channels is not None else {}
    status_bar = MagicMock(name="status_bar")
    message_detail = MagicMock(name="message_detail")
    ctx = MainWindowCtx(
        app=MagicMock(name="app"),
        monitor=MagicMock(name="monitor"),
        loop=MagicMock(name="loop"),
        vm=vm,
        env_path=MagicMock(name="env_path"),
        main_window=MagicMock(name="main_window"),
        window_title_setter=MagicMock(name="window_title_setter"),
        quit_app=MagicMock(name="quit_app"),
        live_view=live_view,
        message_detail=message_detail,
        media_manager=MagicMock(name="media_manager"),
        dashboard=MagicMock(name="dashboard"),
        channel_panel=MagicMock(name="channel_panel"),
        status_bar=status_bar,
        nav=MagicMock(name="nav"),
        stack=MagicMock(name="stack"),
        tray=None,
        media_refresh_debounce=MagicMock(name="media_refresh_debounce"),
    )
    refs = {
        "live_view": live_view,
        "vm": vm,
        "status_bar": status_bar,
        "message_detail": message_detail,
    }
    return ctx, refs


def _make_msg(*, channel_id: int = 100, telegram_msg_id: int = 5):
    """构造最简 MessageDTO 用于 handler 输入(走项目 factory,默认填齐
    `id` / `date` / `text` 等必填字段)。"""
    return make_message(channel_id=channel_id, msg_id=telegram_msg_id)


# ============================================================
# `on_message_received` 行为
# ============================================================


def test_on_message_received_calls_set_channel_titles_and_append() -> None:
    """新消息 → set_channel_titles(从 vm.monitored_channels)+ append(m)。"""
    ctx, refs = _make_ctx()
    ctrl = MessageStreamController(ctx)
    m = _make_msg(channel_id=100)
    ctrl.on_message_received(m)
    refs["live_view"].set_channel_titles.assert_called_once_with({})
    refs["live_view"].append.assert_called_once_with(m)


# ============================================================
# `on_message_edited` 行为
# ============================================================


# ============================================================
# `on_message_interactions_changed` 行为
# ============================================================


# ============================================================
# `on_bus_message_deleted` 行为(async subscriber)
# ============================================================


@pytest.mark.asyncio
async def test_on_bus_message_deleted_removes_live_row() -> None:
    """MessageDeleted → LIVE 流 remove_row + activity toast。"""
    ctx, refs = _make_ctx()
    ctrl = MessageStreamController(ctx)
    e = MessageDeleted(channel_id=100, telegram_msg_id=5)
    await ctrl.on_bus_message_deleted(e)
    refs["live_view"].remove_row.assert_called_once_with(100, 5)
    refs["status_bar"].show_activity.assert_called_once()
    args = refs["status_bar"].show_activity.call_args
    assert "已删除 #100/5" in args.args[0]
    assert args.kwargs == {"timeout_ms": 2000}
