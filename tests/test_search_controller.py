"""`SearchController` 单元测试 — 2026-10-04 plan 阶段 3.3。

覆盖纯逻辑(handler → ctx 调用)— widget / VM 都用 mock,不依赖 Qt offscreen
paint path race(同 test_search_bar 在 win32 / linux / darwin CI 跳过的根因)。

为什么用 mock:
- controller 不持有 MainWindow 引用,只通过 ctx 访问 `header.search_bar` /
  `live_view` / `vm` — 测 controller 等价于测"正确的 ctx 字段被正确调用"
- 不需要 qapp:mock 对象无 Qt 元对象系统需求
- 与 `test_search_bar.py` 互补:SearchBar 是 widget 行为测试,本测试是
  controller 编排逻辑测试
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tgmonitor.ui.controllers._base import MainWindowCtx  # noqa: E402
from tgmonitor.ui.controllers.search_controller import SearchController  # noqa: E402


def _make_ctx() -> tuple[MainWindowCtx, dict[str, Any]]:
    """构造 MainWindowCtx + 暴露常用 mock 引用给测试体。

    returns:
      (ctx, refs) — `refs` 是测试方便拿的命名引用 dict,避免每个 test 都
      `ctx.live_view.set_filter.assert_called_with(...)` 这种长链。
    """
    # ctx 用 dataclass,只填 controller 实际访问的字段;其它字段用 MagicMock 占位
    # (controller 不引用那些字段,test 不访问它们)。
    live_view = MagicMock(name="live_view")
    live_view.set_messages = MagicMock()
    live_view.set_filter = MagicMock()
    live_view.set_empty_state = MagicMock()
    vm = MagicMock(name="vm")
    vm.search_messages = MagicMock()
    # header.search_bar 单独 mock,放在 header 上(真实代码 `ctx.header.search_bar`)
    search_bar = MagicMock(name="search_bar")
    search_bar.text.return_value = ""
    search_bar.date_range.return_value = (None, None)
    search_bar.scope.return_value = False
    search_bar.is_favorite_active.return_value = False
    search_bar.is_tag_active.return_value = False
    search_bar.is_pinned_active.return_value = False
    header = MagicMock(name="header")
    header.search_bar = search_bar
    # search_debounce 是 QTimer — controller 只调 .start(),mock 即可
    search_debounce = MagicMock(name="search_debounce")
    ctx = MainWindowCtx(
        app=MagicMock(name="app"),
        monitor=MagicMock(name="monitor"),
        loop=MagicMock(name="loop"),
        vm=vm,
        env_path=MagicMock(name="env_path"),
        main_window=MagicMock(name="main_window"),
        live_view=live_view,
        message_detail=MagicMock(name="message_detail"),
        media_manager=MagicMock(name="media_manager"),
        selection_toolbar=MagicMock(name="selection_toolbar"),
        dashboard=MagicMock(name="dashboard"),
        channel_panel=MagicMock(name="channel_panel"),
        header=header,
        status_bar=MagicMock(name="status_bar"),
        nav=MagicMock(name="nav"),
        stack=MagicMock(name="stack"),
        tray=None,
        search_debounce=search_debounce,
        media_refresh_debounce=MagicMock(name="media_refresh_debounce"),
    )
    refs = {
        "live_view": live_view,
        "vm": vm,
        "search_bar": search_bar,
        "search_debounce": search_debounce,
        "header": header,
    }
    return ctx, refs


# ============================================================
# on_search_changed:双过滤 + searching overlay + debounce 启动
# ============================================================


def test_on_search_changed_empty_clears_searching_state() -> None:
    """空输入(纯空白)→ 不切 searching overlay,只启 debounce。

    PR #B5 + PR #5 (P0-L):非空 txt 才覆盖 live_empty / no_subscribed。
    """
    ctx, refs = _make_ctx()
    ctrl = SearchController(ctx)
    ctrl.on_search_changed("   ")
    refs["live_view"].set_empty_state.assert_not_called()
    refs["live_view"].set_filter.assert_called_once_with(
        "   ", favorite_only=False, tag_only=False, pinned_only=False
    )
    refs["search_debounce"].start.assert_called_once()


def test_on_search_changed_non_empty_triggers_searching_overlay() -> None:
    """非空 txt → set_empty_state("searching") 覆盖 live_empty。

    PR #5 (P0-L):搜索激活时显示「无匹配结果」overlay。
    """
    ctx, refs = _make_ctx()
    ctrl = SearchController(ctx)
    ctrl.on_search_changed("hello")
    refs["live_view"].set_empty_state.assert_called_once_with("searching")
    refs["live_view"].set_filter.assert_called_once_with(
        "hello", favorite_only=False, tag_only=False, pinned_only=False
    )


def test_on_search_changed_propagates_filter_toggles() -> None:
    """★/🏷/📌 任一 toggle 激活 → set_filter 同时收到(即时快过滤 narrow)。

    PR #8:3 toggle 状态透传,UI 即时 narrow 已加载 LIVE 流。
    """
    ctx, refs = _make_ctx()
    refs["search_bar"].is_favorite_active.return_value = True
    refs["search_bar"].is_pinned_active.return_value = True
    ctrl = SearchController(ctx)
    ctrl.on_search_changed("abc")
    refs["live_view"].set_filter.assert_called_once_with(
        "abc", favorite_only=True, tag_only=False, pinned_only=True
    )


# ============================================================
# on_search_date_changed:立即触发,不走 debounce
# ============================================================


def test_on_search_date_changed_triggers_immediately() -> None:
    """日期范围调整 → 立即调 vm.search_messages(不等待 debounce)。

    PR #B5:用户主动调日历 = 期望立刻响应。
    """
    ctx, refs = _make_ctx()
    refs["search_bar"].text.return_value = "x"
    ctrl = SearchController(ctx)
    ctrl.on_search_date_changed(datetime(2026, 1, 1), None)
    refs["vm"].search_messages.assert_called_once()
    # debounce 不应被启动(日期变化不走 debounce)
    refs["search_debounce"].start.assert_not_called()


# ============================================================
# on_search_scope_changed + 3 toggle:重启动 debounce
# ============================================================


@pytest.mark.parametrize(
    "method_name",
    [
        "on_search_scope_changed",
        "on_search_favorite_toggled",
        "on_search_tag_toggled",
        "on_search_pinned_toggled",
    ],
)
def test_toggle_handlers_start_debounce(method_name: str) -> None:
    """scope + 3 filter toggle handler 全部 → 只 start debounce,不直接调 vm。

    vm 实际调用由 debounce fire → `on_debounce_fire` → `run_search_query` 触发。
    """
    ctx, refs = _make_ctx()
    ctrl = SearchController(ctx)
    getattr(ctrl, method_name)(True)
    refs["search_debounce"].start.assert_called_once()
    refs["vm"].search_messages.assert_not_called()


# ============================================================
# on_debounce_fire → run_search_query
# ============================================================


def test_on_debounce_fire_dispatches_to_run_search_query() -> None:
    """300ms debounce 到点 → 调 run_search_query。"""
    ctx, refs = _make_ctx()
    refs["search_bar"].text.return_value = "hello"
    ctrl = SearchController(ctx)
    ctrl.on_debounce_fire()
    refs["vm"].search_messages.assert_called_once()


# ============================================================
# run_search_query:空 query 清视图,非空透传 vm
# ============================================================


def test_run_search_query_empty_clears_view() -> None:
    """空 query(全 False / None)→ set_messages([]) 清空,不走 vm。"""
    ctx, refs = _make_ctx()
    ctrl = SearchController(ctx)
    ctrl.run_search_query()
    refs["live_view"].set_messages.assert_called_once_with([])
    refs["vm"].search_messages.assert_not_called()


def test_run_search_query_propagates_text_only() -> None:
    """text 非空 → vm.search_messages 收到 text + 默认 scope 'subscribed'。

    PR #B5:channel_ids 不显式传,VM 按 scope 自动选「已订阅」。
    """
    ctx, refs = _make_ctx()
    refs["search_bar"].text.return_value = "hello"
    ctrl = SearchController(ctx)
    ctrl.run_search_query()
    refs["vm"].search_messages.assert_called_once_with(
        text="hello",
        date_from=None,
        date_to=None,
        limit=200,
        scope="subscribed",
        favorite_only=False,
        tag_only=False,
        pinned_only=False,
    )


def test_run_search_query_scope_all_passes_through() -> None:
    """scope toggle 切「全部」→ VM 收到 scope='all'(含已退订频道历史)。

    PR #D2:全部 = 跨频道聚合(含 storage 仍有历史的已退订频道)。
    """
    ctx, refs = _make_ctx()
    refs["search_bar"].text.return_value = "x"
    refs["search_bar"].scope.return_value = True
    ctrl = SearchController(ctx)
    ctrl.run_search_query()
    call_kwargs = refs["vm"].search_messages.call_args.kwargs
    assert call_kwargs["scope"] == "all"


def test_run_search_query_passes_through_date_range() -> None:
    """日期范围任一非 None → date_from / date_to 透传(不必 text 非空)。"""
    ctx, refs = _make_ctx()
    refs["search_bar"].date_range.return_value = (datetime(2026, 1, 1), None)
    ctrl = SearchController(ctx)
    ctrl.run_search_query()
    call_kwargs = refs["vm"].search_messages.call_args.kwargs
    assert call_kwargs["date_from"] == datetime(2026, 1, 1)
    assert call_kwargs["date_to"] is None


def test_run_search_query_passes_through_filter_toggles() -> None:
    """★/🏷/📌 任一 toggle 激活 → vm 收到对应 favorite_only / tag_only / pinned_only。"""
    ctx, refs = _make_ctx()
    refs["search_bar"].text.return_value = "x"
    refs["search_bar"].is_favorite_active.return_value = True
    refs["search_bar"].is_tag_active.return_value = True
    ctrl = SearchController(ctx)
    ctrl.run_search_query()
    call_kwargs = refs["vm"].search_messages.call_args.kwargs
    assert call_kwargs["favorite_only"] is True
    assert call_kwargs["tag_only"] is True
    assert call_kwargs["pinned_only"] is False


def test_run_search_query_filter_only_with_empty_text_runs_vm() -> None:
    """text 空但 ★ toggle 激活 → 仍调 vm(favorite_only 透传),不走清视图分支。

    边界:仅 favorite_only=True / text="" / date 全 None → 不是"空 query"。
    """
    ctx, refs = _make_ctx()
    refs["search_bar"].is_favorite_active.return_value = True
    ctrl = SearchController(ctx)
    ctrl.run_search_query()
    refs["vm"].search_messages.assert_called_once()
    refs["live_view"].set_messages.assert_not_called()
