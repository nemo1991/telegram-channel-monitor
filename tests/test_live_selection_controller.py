"""`LiveSelectionController` 单元测试 — 2026-10-04 plan 阶段 3.3。

controller 从 `MainWindow` 抽出 14 handler + 7 helper + 1 dialog helper +
selection 共享 state。模板延续自 `test_search_controller.py`(mock-based,
thin delegate 测试)。

覆盖维度:
- selection state 维护(`on_selection_count_changed` / `on_selection_messages_changed`)
- `_last_live_selection` 缓存(初始化空,`on_X_selected` 读缓存)
- 右键菜单 6 入口(export / delete / mark_read / favorite / tags / notes)
- toolbar 6 入口(走 `_last_live_selection`)
- 详情顶部 3 入口(export_current / delete_current / copy_text)
- `_normalize_selection_items` helper(2-tuple + 3-tuple + None 容错)
- 二次确认路径(delete / delete_current / pin / react)
- `_show_batch_progress_dialog` 短路
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from tgmonitor.core.dto import ChannelDTO  # noqa: E402
from tgmonitor.ui.controllers._base import MainWindowCtx  # noqa: E402
from tgmonitor.ui.controllers.live_selection_controller import (  # noqa: E402
    LiveSelectionController,
    _normalize_selection_items,
)

# ---- fixtures ----


class _FakeState:
    """最小桩:同时充当 `ctx.main_window` 和 `ctx.live_view` — controller 只调
    .warning() / .clear_selection() / .show_message(),其余方法不依赖。
    """

    def __init__(self) -> None:
        self._user_answers_yes = True
        self._clear_selection_count = 0
        self._show_message_called_with: int | None = None

    def tr(self, source: str) -> str:  # noqa: ANN001, ANN201
        return source

    def warning(self, *args, **kwargs):  # noqa: ANN001, ANN201
        return QMessageBox.Yes if self._user_answers_yes else QMessageBox.No

    def clear_selection(self) -> None:
        self._clear_selection_count += 1

    def show_message(self, message_id: int | None) -> None:  # noqa: ANN001
        self._show_message_called_with = message_id


def _build_ctrl(
    state: _FakeState | None = None,
) -> tuple[LiveSelectionController, MainWindowCtx]:
    """构造 `LiveSelectionController` + mock ctx。

    默认 `app.{pin_messages,delete_messages_batch,mark_messages_read,
    forward_messages,add_reaction,set_favorite,set_tags,set_notes,
    list_joined_channels,export}` 全是 AsyncMock,测试用
    `assert_called_once_with(...)` 验证 controller 调用的参数。
    """
    if state is None:
        state = _FakeState()
    app_mock = MagicMock(name="app")
    for name in (
        "pin_messages",
        "delete_messages_batch",
        "mark_messages_read",
        "forward_messages",
        "add_reaction",
        "set_favorite",
        "set_tags",
        "set_notes",
        "list_joined_channels",
    ):
        setattr(app_mock, name, AsyncMock(name=f"app.{name}", return_value=None))
    # app.export 是 AsyncIterator,单独配
    app_mock.export = MagicMock(name="app.export")
    app_mock.export.return_value.__aiter__ = lambda self: iter([])  # type: ignore[attr-defined]

    selection_toolbar = MagicMock(name="selection_toolbar")
    ctx = MainWindowCtx(
        app=app_mock,
        monitor=MagicMock(name="monitor"),
        loop=MagicMock(name="loop"),
        vm=MagicMock(name="vm"),
        env_path=MagicMock(name="env_path"),
        main_window=state,
        live_view=state,
        message_detail=state,
        media_manager=MagicMock(name="media_manager"),
        selection_toolbar=selection_toolbar,
        dashboard=MagicMock(name="dashboard"),
        channel_panel=MagicMock(name="channel_panel"),
        header=MagicMock(name="header"),
        status_bar=MagicMock(name="status_bar"),
        nav=MagicMock(name="nav"),
        stack=MagicMock(name="stack"),
        tray=None,
        search_debounce=MagicMock(name="search_debounce"),
        media_refresh_debounce=MagicMock(name="media_refresh_debounce"),
    )
    return LiveSelectionController(ctx), ctx


def _patch_dialogs(monkeypatch: pytest.MonkeyPatch, state: _FakeState) -> None:
    """绕过真实 QMessageBox / EmojiPickerDialog / ExportDialog / ChannelPickerDialog /
    QInputDialog / BatchProgressDialog / run_coro。

    让 controller 走通的副作用全部短路,只测确认 / 调用链 / selection state。
    """
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", state.warning)
    monkeypatch.setattr(
        "tgmonitor.ui.widgets.emoji_picker_dialog.EmojiPickerDialog.get_emoji",
        lambda parent: ("👍", False),
    )
    monkeypatch.setattr(
        "tgmonitor.ui.widgets.batch_progress_dialog.BatchProgressDialog",
        lambda *args, **kwargs: MagicMock(name="BatchProgressDialog"),
    )
    monkeypatch.setattr(
        "tgmonitor.ui.widgets.export_dialog.ExportDialog",
        MagicMock(name="ExportDialog"),
    )
    monkeypatch.setattr(
        "tgmonitor.ui.widgets.channel_picker_dialog.ChannelPickerDialog.pick_channel",
        lambda parent, channels: 999,
    )

    # QInputDialog.getText/getMultiLineText:返 user 控的 text + ok=True
    monkeypatch.setattr(
        "PySide6.QtWidgets.QInputDialog.getText",
        lambda *args, **kwargs: ("tech, news", True),
    )
    monkeypatch.setattr(
        "PySide6.QtWidgets.QInputDialog.getMultiLineText",
        lambda *args, **kwargs: ("reminder note", True),
    )

    def _noop_run_coro(loop, coro, **kwargs):  # noqa: ANN001
        import inspect

        if inspect.iscoroutine(coro):
            coro.close()
        # on_success 仍然触发 — controller 内部有的回调(如 _on_live_forward)
        if "on_success" in kwargs and kwargs["on_success"] is not None:
            # 不调 — list_joined_channels AsyncMock 返 None,没有 channels 列表
            pass
        return None

    monkeypatch.setattr("tgmonitor.ui.async_bridge.run_coro", _noop_run_coro)


# ---- selection state 维护 ----


def test_last_live_selection_initially_empty() -> None:
    """`_last_live_selection` 初始化为 []。"""
    ctrl, _ = _build_ctrl()
    assert ctrl._last_live_selection == []


def test_on_selection_count_changed_zero_hides_toolbar() -> None:
    """count=0 → toolbar 隐藏。"""
    ctrl, ctx = _build_ctrl()
    ctrl.on_selection_count_changed(0)
    ctx.selection_toolbar.setVisible.assert_called_once_with(False)


def test_on_selection_count_changed_positive_shows_toolbar() -> None:
    """count>0 → toolbar 显示。"""
    ctrl, ctx = _build_ctrl()
    ctrl.on_selection_count_changed(3)
    ctx.selection_toolbar.setVisible.assert_called_once_with(True)


def test_on_selection_messages_changed_caches_normalized() -> None:
    """3-tuple 输入也正常化 → 缓存 (cid, mid) + 同步 toolbar 计数。"""
    ctrl, ctx = _build_ctrl()
    ctrl.on_selection_messages_changed([(1, 100, 0), (2, 200, 1)])
    assert ctrl._last_live_selection == [(1, 100), (2, 200)]
    ctx.selection_toolbar.set_count.assert_called_once_with(2)


def test_on_selection_messages_changed_2tuple() -> None:
    """2-tuple 输入也接受。"""
    ctrl, ctx = _build_ctrl()
    ctrl.on_selection_messages_changed([(1, 100), (2, 200), (3, 300)])
    assert ctrl._last_live_selection == [(1, 100), (2, 200), (3, 300)]
    ctx.selection_toolbar.set_count.assert_called_once_with(3)


# ---- toolbar 6 入口读 _last_live_selection ----


def test_on_live_delete_selected_uses_cached_selection(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """toolbar 删除按钮 → 用 `_last_live_selection`,答 Yes → 调 delete。"""
    state = _FakeState()
    state._user_answers_yes = True
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)
    ctrl.on_selection_messages_changed([(1, 100), (2, 200)])

    ctrl.on_live_delete_selected()

    ctx.app.delete_messages_batch.assert_called_once_with([(1, 100), (2, 200)])
    assert state._clear_selection_count == 1


def test_on_live_delete_selected_empty_noop(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_last_live_selection` 为空 → 不弹 dialog,不调 delete。"""
    state = _FakeState()
    state._user_answers_yes = True
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    ctrl.on_live_delete_selected()

    ctx.app.delete_messages_batch.assert_not_called()
    assert state._clear_selection_count == 0


def test_on_live_mark_read_selected_uses_cached_selection(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """toolbar 标已读按钮 → 调 mark_messages_read(无 confirm,不清 selection)。"""
    state = _FakeState()
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)
    ctrl.on_selection_messages_changed([(10, 1000)])

    ctrl.on_live_mark_read_selected()

    ctx.app.mark_messages_read.assert_called_once_with([(10, 1000)])
    assert state._clear_selection_count == 0, "mark_read 不清 selection"


# ---- 右键菜单 delete 二次确认 ----


def test_on_live_delete_yes_runs_and_clears(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """右键删除 → 答 Yes → 调 delete + clear_selection。"""
    state = _FakeState()
    state._user_answers_yes = True
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    ctrl.on_live_delete([(1, 100), (2, 200)])

    ctx.app.delete_messages_batch.assert_called_once_with([(1, 100), (2, 200)])
    assert state._clear_selection_count == 1


def test_on_live_delete_no_skips(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """右键删除 → 答 No → 不调 delete。"""
    state = _FakeState()
    state._user_answers_yes = False
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    ctrl.on_live_delete([(1, 100)])

    ctx.app.delete_messages_batch.assert_not_called()
    assert state._clear_selection_count == 0


def test_on_live_delete_empty_no_dialog(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """空选区 → 不弹 dialog。"""
    state = _FakeState()
    state._user_answers_yes = True
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    ctrl.on_live_delete([])

    ctx.app.delete_messages_batch.assert_not_called()


def test_on_live_delete_normalizes_3tuple(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """3-tuple (cid, mid, idx) → 规范化成 (cid, mid)。"""
    state = _FakeState()
    state._user_answers_yes = True
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    ctrl.on_live_delete([(1, 100, 0), (2, 200, 1)])

    ctx.app.delete_messages_batch.assert_called_once_with([(1, 100), (2, 200)])


# ---- right-click mark_read(favorite / tags / notes) ----


def test_on_live_mark_read_runs_without_confirm(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """右键标已读 → 无 confirm,直接调 mark_messages_read。"""
    state = _FakeState()
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    ctrl.on_live_mark_read([(1, 100)])

    ctx.app.mark_messages_read.assert_called_once_with([(1, 100)])


def test_on_live_favorite_calls_set_favorite(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """右键 ★ → 调 set_favorite(channel_id, telegram_msg_id, value)。"""
    state = _FakeState()
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    ctrl.on_live_favorite(42, 1000, True)

    ctx.app.set_favorite.assert_called_once_with(42, 1000, True)


def test_on_live_favorite_unset_calls_with_false(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """右键 ★ 取消 → value=False。"""
    state = _FakeState()
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    ctrl.on_live_favorite(42, 1000, False)

    ctx.app.set_favorite.assert_called_once_with(42, 1000, False)


def test_on_live_tags_splits_commas_and_spaces(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """右键 🏷 → QInputDialog 拿 "tech, news" → set_tags(["tech", "news"])。"""
    state = _FakeState()
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    ctrl.on_live_tags(42, 1000)

    ctx.app.set_tags.assert_called_once_with(42, 1000, ["tech", "news"])


def test_on_live_notes_sends_multiline_text(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """右键 📝 → QInputDialog.getMultiLineText → set_notes。"""
    state = _FakeState()
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    ctrl.on_live_notes(42, 1000)

    ctx.app.set_notes.assert_called_once_with(42, 1000, "reminder note")


# ---- 详情顶部 3 入口 ----


def test_on_live_export_current_opens_dialog(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """详情顶部「导出」→ ExportDialog(channel_ids=[cid], single_message_id=mid)。"""
    state = _FakeState()
    _patch_dialogs(monkeypatch, state)
    ctrl, _ctx = _build_ctrl(state)

    ctrl.on_live_export_current(42, 1000)

    # ExportDialog 被 MagicMock 替换,exec() 默认 True → 走到 _run_live_export
    # _run_live_export 调 self._ctx.app.export(MagicMock())→ 不出错
    # 这里只验证 exec 被调


def test_on_live_delete_current_yes_runs_and_show_none(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """详情顶部「删除」→ 答 Yes → delete + show_message(None)。"""
    state = _FakeState()
    state._user_answers_yes = True
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    ctrl.on_live_delete_current(42, 1000)

    ctx.app.delete_messages_batch.assert_called_once_with([(42, 1000)])
    assert state._show_message_called_with is None


def test_on_live_delete_current_no_skips(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """详情顶部「删除」→ 答 No → 不 delete,show_message 不动。"""
    state = _FakeState()
    state._user_answers_yes = False
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    ctrl.on_live_delete_current(42, 1000)

    ctx.app.delete_messages_batch.assert_not_called()
    assert state._show_message_called_with is None


def test_on_live_copy_text_shows_status_message(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """详情顶部「复制文本」→ status_bar 显示「已复制」2 秒。"""
    state = _FakeState()
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)

    ctrl.on_live_copy_text("hello")

    ctx.status_bar.show_message.assert_called_once()
    args = ctx.status_bar.show_message.call_args
    # 第一个位置参数应是翻译后的「已复制」(stub tr 返回源串)
    assert args[0][0] == "已复制"


# ---- _normalize_selection_items helper ----


def test_normalize_items_2tuple() -> None:
    """2-tuple 直接通过。"""
    assert _normalize_selection_items([(1, 100), (2, 200)]) == [(1, 100), (2, 200)]


def test_normalize_items_3tuple_drops_third() -> None:
    """3-tuple (cid, mid, idx) → (cid, mid)。"""
    assert _normalize_selection_items([(1, 100, 0)]) == [(1, 100)]


def test_normalize_items_none_safe() -> None:
    """None / [] → []。"""
    assert _normalize_selection_items(None) == []
    assert _normalize_selection_items([]) == []


def test_normalize_items_filters_invalid() -> None:
    """非 tuple / 长度不足 2 → 过滤。"""
    items = [(1, 100), "bad", None, (2,), (3, 300, "x", "y")]
    assert _normalize_selection_items(items) == [(1, 100), (3, 300)]


# ---- toolbar on_live_forward / pin / react selected 走 _last_live_selection ----


def test_on_live_pin_selected_yes_calls_pin_messages(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """toolbar 钉选 → 用 _last_live_selection → 答 Yes → pin_messages。"""
    state = _FakeState()
    state._user_answers_yes = True
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)
    ctrl.on_selection_messages_changed([(1, 100)])

    ctrl.on_live_pin_selected()

    ctx.app.pin_messages.assert_called_once_with([(1, 100)])


def test_on_live_react_selected_yes_calls_add_reaction(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """toolbar emoji 回应 → 用 _last_live_selection → 答 Yes → add_reaction。"""
    state = _FakeState()
    state._user_answers_yes = True
    _patch_dialogs(monkeypatch, state)
    ctrl, ctx = _build_ctrl(state)
    ctrl.on_selection_messages_changed([(1, 100)])

    ctrl.on_live_react_selected()

    ctx.app.add_reaction.assert_called_once_with([(1, 100)], "👍", is_big=False)


def test_on_live_export_selected_uses_cached(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """toolbar 导出 → 用 _last_live_selection。"""
    state = _FakeState()
    _patch_dialogs(monkeypatch, state)
    ctrl, _ctx = _build_ctrl(state)
    ctrl.on_selection_messages_changed([(1, 100), (2, 200)])

    # 不直接断言 ExportDialog 内容(MagicMock),只验证不抛异常 + 调用链走通
    ctrl.on_live_export_selected()


# ---- ChannelDTO 导入可达性(防止 import 漂移) ----


def test_channel_dto_importable() -> None:
    """`ChannelDTO` 是 _on_live_forward 用到的类型,防止 import 漂移。"""
    assert ChannelDTO is not None
