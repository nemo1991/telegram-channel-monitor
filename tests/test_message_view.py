"""MessageView 渲染格式测试 — 本地时区 / 频道名 / msg id。

2026-09-02 v1.5.3 PR #D1:`QListWidget` → `QListView` + `MessageListModel` +
`MessageItemDelegate`。所有 `_format` 走 `view._format(m)` shim(内部调
`model._format`);`view._seen` 改为 property(读 `model._index_of`);
`view.item(i).text()` / `.isHidden()` / `.background()` 改为 adapter
走 `model.data(idx, role)` — 保持测试 focus 在渲染语义而非 model 协议。

需要 QApplication:widget 实例化要求 QGuiApplication 存活。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

# offscreen 平台:CI / 无显示器 macOS 也能跑
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


from tests.fixtures._factories import make_message
from tgmonitor.core.dto import MessageDTO
from tgmonitor.ui.widgets.message_view import (
    MessageItemDelegate,
    MessageListModel,
    MessageView,
)

# ---- shim helpers — 把 model 协议包成测试熟悉的 API ----


def _item_text(view: MessageView, row: int) -> str:
    """2026-09-02 v1.5.3 PR #D1:`view.item(i).text()` 替代品。

    走 `model.data(model.index(i, 0), FormattedRole)` — delegate paint 时
    也是这条路径。
    """
    if row < 0 or row >= view.count():
        return ""
    idx = view._model.index(row, 0)
    return view._model.data(idx, MessageListModel.FormattedRole) or ""


def _item_is_hidden(view: MessageView, row: int) -> bool:
    """`view.item(i).isHidden()` 替代品。"""
    if row < 0 or row >= view.count():
        return True
    idx = view._model.index(row, 0)
    return bool(view._model.data(idx, MessageListModel.HiddenRole))


def _item_has_media_bg(view: MessageView, row: int) -> bool:
    """`view.item(i).background() != QBrush()` 替代品 — 判媒体行底色。"""
    if row < 0 or row >= view.count():
        return False
    idx = view._model.index(row, 0)
    return bool(view._model.data(idx, MessageListModel.HasMediaRole))


# ---- _format 时间显示 ----


# ---- 频道名 / msg id ----


# ---- set_channel_titles 行为 ----


# ---- append → 实际渲染 ----


def test_append_dedup_updates_existing_row(qapp):
    """同 (channel_id, telegram_msg_id) 重复 append → 更新文本而非新增行。"""
    view = MessageView()
    m1 = make_message(
        id=0,
        channel_id=100,
        msg_id=1,
        text="first",
        author=None,
        date=datetime(2026, 7, 15, 13, 50, 10),
    )
    m2 = make_message(
        id=0,
        channel_id=100,
        msg_id=1,
        text="edited",
        author=None,
        date=datetime(2026, 7, 15, 13, 51, 0),
    )
    view.append(m1)
    view.append(m2)
    assert view.count() == 1
    assert "edited" in _item_text(view, 0)


# ---- 媒体 DTO 回归(Signal(object) 路径) ----


# ---- 空状态覆盖:首启 / 数据到达 / 清空 ----


# ---- remove_row (2026-08-24 Media Manager 接入) ----


def _make_msg(channel_id: int, telegram_msg_id: int) -> MessageDTO:
    """2026-10-04 plan 阶段 1.3.D:走 `make_message` 工厂 + `**overrides` 传
    `text="x"` / `author=None` / 固定 `date`。本函数在 24 callsite 用 —
    工厂泛化后等价于 `make_message(channel_id=cid, msg_id=mid, text="x",
    author=None, date=...)`,净减 5 行。
    """

    return make_message(
        channel_id=channel_id,
        msg_id=telegram_msg_id,
        text="x",
        author=None,
        date=datetime(2026, 7, 15, 13, 0, 0),
    )


# ============================================================
# 2026-09-02 v1.5.2 PR #B5:MessageView.set_messages 批量替换。
# ============================================================


def test_set_messages_then_live_append_no_duplicate(qapp):
    """PR #B5:set_messages 后 live append 已有 key → 不增行(走 append 的 replace 分支)。"""
    view = MessageView()
    m = _make_msg(1, 100)
    view.set_messages([m])
    assert view.count() == 1

    # live 推一条同 key 的更新消息
    view.append(m)
    # 没增行 — count 仍 1
    assert view.count() == 1
    assert view.row_of_key(1, 100) is not None


# ============================================================
# 2026-09-02 v1.5.3 PR #D1:QListView + MessageListModel + delegate 新协议测试。
# ============================================================


def test_model_truncates_at_max_items(qapp):
    """PR #D1:`append` 触发 MAX_ITEMS 截断 — 超过 N+1 条后只保留 N 条。

    2026-09-03 v1.5.4 PR #P1:MAX_ITEMS 1000 → 10000,本测试 append MAX_ITEMS+1 条
    验证截断边界。性能上 PR #P1 同时修了 _truncate_tail O(N)→O(1),10K 截断不再
    退化为 O(N²)。
    """
    view = MessageView()
    # MAX_ITEMS = 10000,append 10001 条
    for i in range(MessageView.MAX_ITEMS + 1):
        view.append(_make_msg(1, i))
    assert view._model.rowCount() == MessageView.MAX_ITEMS
    # 最早 append 的 (1, 0) 应被截断(最新 MAX_ITEMS 条留)
    assert view.row_of_key(1, 0) is None
    assert view.row_of_key(1, MessageView.MAX_ITEMS) is not None


def test_plain_to_html_escapes_special_chars(qapp):
    """PR #D1:delegate `_plain_to_html` 转义 < > & 避免被当 HTML 解析。"""

    out = MessageItemDelegate._plain_to_html("a < b & c > d\neol")
    assert "&lt;" in out
    assert "&gt;" in out
    assert "&amp;" in out
    assert "<br>" in out  # 换行


def test_replace_message_updates_row_in_place(qapp):
    """PR #D1:replace_message 编辑事件 — 已存在 row 重 format 不增行。"""
    view = MessageView()
    m_orig = _make_msg(1, 1)
    view.append(m_orig)
    m_edited = make_message(
        id=0,
        channel_id=1,
        msg_id=1,
        text="edited!",
        author=None,
        date=datetime(2026, 7, 15, 13, 50, 10),
    )
    view.replace_message(m_edited)
    assert view.count() == 1
    assert "edited!" in _item_text(view, 0)


# ============================================================
# legacy helper — 旧测试用 `QBrush()` 判底色,保留 1 个测试 shim 验证迁移路径
# ============================================================


# ============================================================
# 2026-09-03 v1.5.4 PR #P1:_truncate_tail O(1) 重构 + MAX_ITEMS=10000 实验性 bump。
# ============================================================


def _assert_invariant(view: MessageView) -> None:
    """PR #9:`_index_of[(cid, mid)] == row` 必须与 `_items[row]` 严格对应。

    `_items` 是 deque,物理位置 0 = newest,size = _index_of size;deque[i]
    是 O(1) 均摊索引,可直接当 list 用。
    """
    model = view._model
    assert len(model._items) == view.count(), (
        f"items size={len(model._items)} != view.count()={view.count()}"
    )
    for r in range(len(model._items)):
        m = model._items[r]
        key = (m.channel_id, m.telegram_msg_id)
        assert model.row_of_key(*key) == r, (
            f"invariant broken at row={r}: _items[{r}]=({m.channel_id}, {m.telegram_msg_id}) "
            f"but row_of_key({key})={model.row_of_key(*key)}"
        )


def test_stress_rolling_window_100_append_dedup_truncate(qapp):
    """2026-11-16 v2.0.2:**stress test** — append 1K 条(远超 100 上限)+ 中途
    100 次 remove,验证滚动窗口不变量:

    - rowCount 恒 ≤ MAX_ITEMS(100),只保留最新 100 条
    - `_index_of` 与 `_items` 严格镜像(invariant)
    - 旧消息按 FIFO 被截断(窗口滚动)

    MAX_ITEMS 10000 → 100 后原 10K stress 规模已无意义,改为 1K append 守
    「大批量 append 下滚动窗口 + invariant」仍在。
    """
    view = MessageView()
    # 1K append — 远超 100 上限,触发反复 _truncate_tail
    for i in range(1_000):
        view.append(_make_msg(1, i))
    assert view._model.rowCount() == 100
    # 最新 (1, 999) 保留,最旧 (1, 0) 已滚出
    assert view.row_of_key(1, 999) is not None
    assert view.row_of_key(1, 0) is None
    # invariant 验证
    _assert_invariant(view)
    # 中途 remove 后仍 ≤ 100 且 invariant 成立
    for i in range(900, 1_000, 10):
        view.remove_row(1, i)
    assert view._model.rowCount() <= 100
    _assert_invariant(view)


# ============================================================
# 2026-09-10 v1.7.3:行渲染 ★ / 标签: / 备注: / [置顶]
# ============================================================
#
# v1.7.2 加了 is_favorite / tags / notes / is_pinned 字段但没渲染,用户右键
# ★ 后行没变化。本版本补上 _format_meta_icons。


def _make_meta_msg(
    *,
    channel_id: int = 1,
    telegram_msg_id: int = 100,
    is_favorite: bool = False,
    tags: list[str] | None = None,
    notes: str = "",
    is_pinned: bool = False,
) -> MessageDTO:
    """构造带 metadata 的 message DTO,默认全空。"""
    return make_message(
        id=0,
        channel_id=channel_id,
        msg_id=telegram_msg_id,
        text="x",
        author=None,
        date=datetime(2026, 7, 15, 13, 0, 0),
        is_favorite=is_favorite,
        tags=tags or [],
        notes=notes,
        is_pinned=is_pinned,
    )


# ============================================================
# 2026-09-11 v1.7.4:`_format_meta_icons` 加 reactions 渲染 +
# `MessageListModel.refresh_reactions` 局部更新
# ============================================================


def _make_msg_with_reactions(
    *,
    telegram_msg_id: int = 100,
    reactions: list | None = None,
) -> MessageDTO:
    """2026-09-11 v1.7.4:构造带 reactions 的 message DTO。

    2026-10-04 plan 阶段 1.3.D:走 `tests.fixtures._factories.make_message` +
    `**overrides` 传 `reactions=...`,不在本地手搓 `MessageDTO(...)`。
    """

    return make_message(
        channel_id=1,
        msg_id=telegram_msg_id,
        text="x",
        date=datetime(2026, 7, 15, 13, 0, 0),
        reactions=reactions,
    )


# ============================================================
# 2026-09-14 v1.7.5 PR #8:★/标签:/[置顶] filter 测试(AND 语义)
# ============================================================


def _hidden_role(view: MessageView, row: int) -> bool:
    """读 MessageListModel HiddenRole — True = hidden(被过滤掉)。"""
    idx = view.model().index(row, 0)
    return bool(view.model().data(idx, MessageListModel.HiddenRole))


def test_pr8_set_filter_combined_and_semantics(qapp):
    """PR #8:多 filter 同时启用 → AND 语义(任一不满足就 hidden)。

    3 条 append 后 newest-first 顺序:no_fav (row 0), no_pin (row 1),
    full_match (row 2)。
    """
    view = MessageView()
    # 完全命中:fav + tag + pinned
    full_match = _make_meta_msg(
        telegram_msg_id=100,
        is_favorite=True,
        tags=["tech"],
        is_pinned=True,
    )
    # 缺 pinned
    no_pin = _make_meta_msg(telegram_msg_id=101, is_favorite=True, tags=["tech"])
    # 缺 favorite
    no_fav = _make_meta_msg(telegram_msg_id=102, tags=["tech"], is_pinned=True)
    view.append(full_match)
    view.append(no_pin)
    view.append(no_fav)
    view.set_filter(favorite_only=True, tag_only=True, pinned_only=True)
    assert _hidden_role(view, 0) is True  # no_fav hidden(缺 fav)
    assert _hidden_role(view, 1) is True  # no_pin hidden(缺 pin)
    assert _hidden_role(view, 2) is False  # full_match 通过


# 注:_make_meta_msg 默认 text=f"msg{telegram_msg_id}",会被 "msg" 搜索命中
# (因未单独改 text)。上面组合测试显式改 .text 为 "alpha content" / "beta content"
# 以验证 text + 元数据 AND 语义。


# ============================================================
# 2026-09-15 v1.7.5 PR #9 perf:MessageListModel O(N²) → O(N) + sizeHint / paint cache
# ============================================================


def _under_coverage() -> bool:
    """当前进程是否被 coverage.py 插桩(`coverage run` / pytest-cov)。

    coverage 走 `sys.settrace`,**每一行**都加钩子 → 被测代码系统性慢 ~10x。
    绝对耗时断言在这种环境下不成立(CI macOS 实测 10K append 5.03ms > 5ms
    上限),且这是系统性偏差,取 min / 重试都救不回来 —— 只能跳过。
    """
    return sys.gettrace() is not None


def test_pr9_format_cache_invalidated_on_replace_message(qapp):
    """PR #9 perf:`replace_message` 让缓存里那条 (cid, mid) 失效 — 下次
    `data(FormattedRole)` 走 `_format()` 重算。
    """
    view = MessageView()
    msg1 = _make_msg(1, 100)
    msg1.text = "original"
    view.append(msg1)
    idx = view._model.index(0, 0)

    # 第一次 — 填缓存
    first = view._model.data(idx, MessageListModel.FormattedRole)
    assert "original" in first

    # replace_message — 改文本
    msg2 = _make_msg(1, 100)
    msg2.text = "edited"
    view.replace_message(msg2)

    # 缓存失效,新文本生效
    second = view._model.data(idx, MessageListModel.FormattedRole)
    assert "edited" in second, f"replace 后应显示新文本,实测 {second!r}"
