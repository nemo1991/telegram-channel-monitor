"""2026-09-17 PR 6:`_truncate_tail` deque 不变量 property test。

PR 6 plan 测点 5:随机 append + pop 序列 → deque 长度 ≤ MAX_ITEMS +
newest-first 顺序保。

测法:
- 构造随机 MessageDTO 序列(0 ~ MAX_ITEMS+50)
- 调 MessageListModel.append() N 次
- 断言:`count() ≤ MAX_ITEMS` + `row_of_key` 对应位置正确(newest = row 0)
- 断言:被截断的 key 不在 `_index_of` 里
- 边界:append 同 key 两次 → 不增加 row 数(原地替换)
- 边界:append > MAX_ITEMS 条 → 最旧的被截断(保留 newest)

bug 锁定:`test_truncate_tail_drops_oldest_when_exceeds_max` 锁最旧被删。
"""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tgmonitor.core.dto import MessageDTO
from tgmonitor.ui.widgets.message_view import MessageListModel, MessageView

# ========== helpers ==========


def _make_msg(cid: int, mid: int) -> MessageDTO:
    return MessageDTO(
        id=0,
        channel_id=cid,
        telegram_msg_id=mid,
        date=datetime(2026, 9, 17, tzinfo=UTC),
        text=f"text-{cid}-{mid}",
    )


# ========== property:append N 条 → invariant 保持 ==========


@given(
    cid=st.integers(min_value=1, max_value=10),
    mids=st.lists(
        st.integers(min_value=1, max_value=10_000),
        min_size=1,
        max_size=MessageView.MAX_ITEMS + 100,
    ),
)
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_truncate_tail_invariant(cid: int, mids: list[int]) -> None:
    """Append N 条随机 mid → `count() ≤ MAX_ITEMS` + newest 在 row 0。

    重复 mid 会触发 dedup(原地替换,不入新行),所以 `len(set(mids))` ≥ `count()`
    也得验证。
    """
    model = MessageListModel()
    for mid in mids:
        model.append(_make_msg(cid, mid))

    # invariant 1:行数不超过 MAX_ITEMS
    assert model.rowCount() <= MessageView.MAX_ITEMS

    # invariant 2:实际保留的行数 = min(唯一 mid 数, MAX_ITEMS)
    unique_mids = set(mids)
    expected_rows = min(len(unique_mids), MessageView.MAX_ITEMS)
    assert model.rowCount() == expected_rows, (
        f"应保留 {expected_rows} 行(唯一 mid {len(unique_mids)} / MAX {MessageView.MAX_ITEMS}),"
        f"实测 {model.rowCount()}"
    )

    # invariant 3:newest-first — 最后唯一 mid 应在 row 0(若未截断)
    # 取最后一次出现且尚未被截断的 mid
    last_unique_mid: int | None = None
    seen = set()
    for mid in mids:
        if mid not in seen:
            last_unique_mid = mid
            seen.add(mid)
    if last_unique_mid is not None:
        actual_row = model._index_of.get((cid, last_unique_mid))
        # 只有当 last_unique_mid 没被截断(超 MAX_ITEMS 才截)时,断言 row 0
        if actual_row is not None:
            assert actual_row == 0, (
                f"最新 unique mid={last_unique_mid} 应在 row 0,实测 {actual_row}"
            )


# ========== 边界:append 同 key 两次 → 不增加行数 ==========


@given(
    cid=st.integers(min_value=1, max_value=10),
    mid=st.integers(min_value=1, max_value=1000),
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_append_same_key_dedup_does_not_add_row(cid: int, mid: int) -> None:
    """Append 同 `(cid, mid)` 多次 → 不增加 row 数(原地替换)。

    regression:PR #9 改 `list.insert(0) → deque.appendleft`,dedup 分支
    `_items[row] = m` 必须保证 deque 支持 `__setitem__` 且 rowCount 不变。
    """
    model = MessageListModel()
    for _ in range(3):
        model.append(_make_msg(cid, mid))

    assert model.rowCount() == 1, (
        f"dedup 应保留 1 行,实测 {model.rowCount()}"
    )
    # 索引应为 0(head)
    assert model._index_of.get((cid, mid)) == 0


# ========== 边界:append > MAX_ITEMS → 最旧被截断 ==========


def test_truncate_tail_drops_oldest_when_exceeds_max() -> None:
    """Append 10001 条 → 第 1 条 `(1, 0)` 应被截断。

    锁定 PR #9 regression:之前用 `popleft()` 误删最新,改 `pop()` 后语义恢复。
    """
    model = MessageListModel()
    cid = 1
    for i in range(MessageView.MAX_ITEMS + 1):
        model.append(_make_msg(cid, i))

    assert model.rowCount() == MessageView.MAX_ITEMS
    # (1, 0) 是最早 append 的 → 应被截断
    assert (cid, 0) not in model._index_of, "(1, 0) 应被截断"
    # (1, MAX_ITEMS) 是最后 append 的 → 应在 row 0(head)
    assert model._index_of.get((cid, MessageView.MAX_ITEMS)) == 0, (
        f"(1, {MessageView.MAX_ITEMS}) 应在 row 0"
    )


# ========== 边界:截断后 _format_cache 也清 ==========


def test_truncate_tail_clears_format_cache_for_dropped_key() -> None:
    """被截断的 key 在 `_format_cache` 里也应被清。

    `_format_cache` 在 `data(FormattedRole)` 读取时填充,append 不填。
    这里直接 monkey-patch 模拟 paint 路径,避免真实 Qt paint 流程。
    """
    model = MessageListModel()
    cid = 1
    # 灌 5 条
    for i in range(5):
        model.append(_make_msg(cid, i))
    # 手动塞 _format_cache 模拟 paint 过的 row
    model._format_cache[(cid, 0)] = "fake-doc-0"
    model._format_cache[(cid, 1)] = "fake-doc-1"
    model._format_cache[(cid, 2)] = "fake-doc-2"
    model._format_cache[(cid, 3)] = "fake-doc-3"
    model._format_cache[(cid, 4)] = "fake-doc-4"
    assert len(model._format_cache) == 5

    # 灌到 MAX_ITEMS+5(让 (0..4) 被截断)
    for i in range(5, MessageView.MAX_ITEMS + 5):
        model.append(_make_msg(cid, i))

    # 被截断的 key(0..4)从 cache 删
    for i in range(5):
        assert (cid, i) not in model._format_cache, (
            f"被截断的 (cid={cid}, mid={i}) 应从 cache 删"
        )
    # 只剩 0 条(原本只有 5 条被截断,cache 全清;存活行未填 cache)
    assert len(model._format_cache) == 0, (
        f"截断后 _format_cache 应清空,剩 {len(model._format_cache)} 条"
    )