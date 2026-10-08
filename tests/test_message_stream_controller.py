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
from tgmonitor.core.dto import ChannelDTO, ReactionDTO  # noqa: E402
from tgmonitor.core.events import MessageDeleted, MessageInteractionsChanged  # noqa: E402
from tgmonitor.ui.controllers._base import MainWindowCtx  # noqa: E402
from tgmonitor.ui.controllers.message_stream_controller import (  # noqa: E402
    MessageStreamController,
)


def _make_ctx(
    *,
    known_channels: dict[int, ChannelDTO] | None = None,
) -> tuple[MainWindowCtx, dict[str, MagicMock]]:
    """构造 MainWindowCtx + 暴露常用 mock 引用给测试体。

    命名引用 dict 让 test 用 `refs["live_view"].append.assert_called_with(m)`
    这种短链,不每次都写 `ctx.live_view.append.assert_called_with(m)`。
    """
    live_view = MagicMock(name="live_view")
    vm = MagicMock(name="vm")
    vm.known_channels = known_channels if known_channels is not None else {}
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
        selection_toolbar=MagicMock(name="selection_toolbar"),
        dashboard=MagicMock(name="dashboard"),
        channel_panel=MagicMock(name="channel_panel"),
        header=MagicMock(name="header"),
        status_bar=status_bar,
        nav=MagicMock(name="nav"),
        stack=MagicMock(name="stack"),
        tray=None,
        search_debounce=MagicMock(name="search_debounce"),
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
    """新消息 → set_channel_titles(从 vm.known_channels)+ append(m)。"""
    ctx, refs = _make_ctx()
    ctrl = MessageStreamController(ctx)
    m = _make_msg(channel_id=100)
    ctrl.on_message_received(m)
    refs["live_view"].set_channel_titles.assert_called_once_with({})
    refs["live_view"].append.assert_called_once_with(m)


def test_on_message_received_throttles_activity_with_channel_title() -> None:
    """新消息 → throttle_activity 用 vm.known_channels 里的频道标题。"""
    ch = ChannelDTO(id=100, title="Tech News")
    ctx, refs = _make_ctx(known_channels={100: ch})
    ctrl = MessageStreamController(ctx)
    ctrl.on_message_received(_make_msg(channel_id=100))
    refs["status_bar"].throttle_activity.assert_called_once()
    args = refs["status_bar"].throttle_activity.call_args
    # 第 1 个定位参是 key("message_received"),第 2 是文案(title="+1 Tech News")
    assert args.args[0] == "message_received"
    assert "+1 Tech News" in args.args[1]
    assert args.kwargs == {"min_interval_ms": 1500, "timeout_ms": 1200}


def test_on_message_received_falls_back_to_channel_id_when_unknown() -> None:
    """未知频道标题时显示 `+1 #N`,而不是抛 KeyError。"""
    ctx, refs = _make_ctx(known_channels={})
    ctrl = MessageStreamController(ctx)
    ctrl.on_message_received(_make_msg(channel_id=999))
    args = refs["status_bar"].throttle_activity.call_args
    assert "+1 #999" in args.args[1]


# ============================================================
# `on_message_edited` 行为
# ============================================================


def test_on_message_edited_calls_replace_message() -> None:
    """编辑 → live_view.replace_message(m),不增删 row。"""
    ctx, refs = _make_ctx()
    ctrl = MessageStreamController(ctx)
    m = _make_msg()
    ctrl.on_message_edited(m)
    refs["live_view"].replace_message.assert_called_once_with(m)
    # 不调 append / remove_row
    refs["live_view"].append.assert_not_called()
    refs["live_view"].remove_row.assert_not_called()


# ============================================================
# `on_message_interactions_changed` 行为
# ============================================================


def test_on_message_interactions_changed_refreshes_live_row_reactions() -> None:
    """interactions 变化 → LIVE 行 reactions 局部刷新。"""
    ctx, refs = _make_ctx()
    ctrl = MessageStreamController(ctx)
    reactions = [ReactionDTO(emoji="👍", count=3)]
    e = MessageInteractionsChanged(channel_id=100, telegram_msg_id=5, reactions=reactions)
    refs["live_view"].dto_by_key.return_value = None  # 不画详情
    ctrl.on_message_interactions_changed(e)
    refs["live_view"].refresh_reactions.assert_called_once_with(100, 5, reactions)


def test_on_message_interactions_changed_ignores_non_matching_type() -> None:
    """非 MessageInteractionsChanged 实例 → 直接 return,不污染状态。"""
    ctx, refs = _make_ctx()
    ctrl = MessageStreamController(ctx)
    ctrl.on_message_interactions_changed("not an event")  # type: ignore[arg-type]
    refs["live_view"].refresh_reactions.assert_not_called()
    refs["message_detail"].refresh_if_showing.assert_not_called()


def test_on_message_interactions_changed_refreshes_detail_when_dto_found() -> None:
    """详情面板正显示同一条 → refresh_if_showing(cid, mid)。"""
    ctx, refs = _make_ctx()
    ctrl = MessageStreamController(ctx)
    refs["live_view"].dto_by_key.return_value = _make_msg()
    e = MessageInteractionsChanged(channel_id=100, telegram_msg_id=5, reactions=None)
    ctrl.on_message_interactions_changed(e)
    refs["live_view"].refresh_reactions.assert_called_once_with(100, 5, None)
    refs["message_detail"].refresh_if_showing.assert_called_once_with(100, 5)


def test_on_message_interactions_changed_skips_detail_refresh_when_no_dto() -> None:
    """LIVE view 没有该 key(dto_by_key=None)→ 不调详情面板刷新。"""
    ctx, refs = _make_ctx()
    ctrl = MessageStreamController(ctx)
    refs["live_view"].dto_by_key.return_value = None
    e = MessageInteractionsChanged(channel_id=100, telegram_msg_id=5, reactions=None)
    ctrl.on_message_interactions_changed(e)
    refs["live_view"].refresh_reactions.assert_called_once()
    refs["message_detail"].refresh_if_showing.assert_not_called()


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


@pytest.mark.asyncio
async def test_on_bus_message_deleted_ignores_non_matching_type() -> None:
    """非 MessageDeleted 实例 → 直接 return,不删 row、不出状态。"""
    ctx, refs = _make_ctx()
    ctrl = MessageStreamController(ctx)
    await ctrl.on_bus_message_deleted(object())  # type: ignore[arg-type]
    refs["live_view"].remove_row.assert_not_called()
    refs["status_bar"].show_activity.assert_not_called()


def test_message_stream_controller_handler_signatures_match_plan() -> None:
    """plan 阶段 3.3:4 handler 全公开 — 防止后续重构漏抽。

    这条是「存在性」守门:handler 必须在 controller 类本身上,不能变
    underscore-deleted。`on_bus_message_deleted` 是 async(跟 EventBus
    subscriber 签名兼容),其它 3 个 sync。
    """
    expected_sync = [
        "on_message_received",
        "on_message_edited",
        "on_message_interactions_changed",
    ]
    expected_async = ["on_bus_message_deleted"]
    import inspect

    for m in expected_sync:
        method = getattr(MessageStreamController, m)
        assert callable(method), f"{m} not callable"
        assert not inspect.iscoroutinefunction(method), f"{m} must be sync"
    for m in expected_async:
        method = getattr(MessageStreamController, m)
        assert callable(method), f"{m} not callable"
        assert inspect.iscoroutinefunction(method), f"{m} must be async"
