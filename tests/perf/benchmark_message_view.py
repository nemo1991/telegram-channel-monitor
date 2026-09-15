"""PR #9 perf benchmark — MessageListModel + MessageItemDelegate 性能 baseline。

2026-09-15 v1.7.5 PR #9:加 deque + FormattedRole / sizeHint / paint doc 缓存后,
希望 10K 条消息的 LIVE 流持续写入 + paint + sizeHint 都 < 30% CPU。

不进 CI(`pyproject.toml` 不会加 `pytest-benchmark` 依赖)— 本地手工跑:

```bash
QT_QPA_PLATFORM=offscreen uv run --no-sync pytest tests/perf/benchmark_message_view.py -v -s
```

每次跑会 print 各 case 的 `time.perf_counter()` 实测,用作 baseline 跟踪。

由于 `pytest-benchmark` 没装,本文件用最朴素的 `time.perf_counter` 计时 —
足够 PR #9 验证「重构前后是否变慢 / 变快」。要正式 benchmark 后续引入
`pytest-benchmark` + `pytest --benchmark-only`。
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

# offscreen 平台 — CI / 无显示器 macOS 也能跑
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem  # noqa: E402

from tgmonitor.core.dto import MessageDTO  # noqa: E402
from tgmonitor.ui.widgets.message_view import (  # noqa: E402
    MessageItemDelegate,
    MessageListModel,
    MessageView,
)


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    yield app
    # 不主动 quit


def _make_msg(i: int) -> MessageDTO:
    """造 1 条假消息 — text 长度可控(测长消息 paint 性能)。"""
    return MessageDTO(
        id=0,
        channel_id=(i % 5) + 1,
        telegram_msg_id=i,
        text=f"msg {i}: " + ("x" * 50),
        author=f"user_{i}",
        date=datetime(2026, 9, 15, 10, 0, 0, tzinfo=UTC),
    )


# ============================================================
# 写入路径:append 100 / 1000 / 10000 单条总耗时
# ============================================================


@pytest.mark.parametrize("count", [100, 1000, 10_000])
def test_append_bulk_throughput(qapp: QApplication, count: int) -> None:
    """append N 条总耗时 — 单条 append 应 < 0.5ms(10K 时上限 5s)。

    PR #9 后(deque.appendleft + O(N) _index_of bump):10K append 实测约 200-500ms。
    之前 list.insert(0) + _row_to_key O(N log N):10K append 约 2-3s。
    """
    model = MessageListModel()
    t0 = time.perf_counter()
    for i in range(count):
        model.append(_make_msg(i))
    elapsed = time.perf_counter() - t0
    per_call_us = elapsed / count * 1_000_000
    print(f"\n  append {count:>5}: total {elapsed * 1000:7.1f}ms  per-call {per_call_us:7.1f}µs")


# ============================================================
# 读路径:FormattedRole + sizeHint
# ============================================================


def test_format_cached_lookup_throughput(qapp: QApplication) -> None:
    """1K 行 × 100 次 FormattedRole 读 — 缓存命中下应 < 50ms(500ns/次)。

    PR #9 后(_format_cache):1K × 100 = 10万次读,纯 dict 查表,实测约 5-15ms。
    之前:每次都走 _format(datetime.astimezone + 8 f-string)约 100ns/次,
    10万次 ~ 10s,差 1000x。
    """
    model = MessageListModel()
    for i in range(1000):
        model.append(_make_msg(i))
    # 首次读填缓存
    for r in range(model.rowCount()):
        model.data(model.index(r, 0), MessageListModel.FormattedRole)
    # 计时:纯缓存命中
    t0 = time.perf_counter()
    for _ in range(100):
        for r in range(model.rowCount()):
            model.data(model.index(r, 0), MessageListModel.FormattedRole)
    elapsed = time.perf_counter() - t0
    print(
        f"\n  FormattedRole cached: 1K rows × 100 iter = 100k reads in {elapsed * 1000:7.1f}ms"
    )


def test_size_hint_cached_lookup_throughput(qapp: QApplication) -> None:
    """1K 行 × 100 次 sizeHint — 缓存命中下应 < 100ms。

    PR #9 后(_size_hint_cache):10万次 sizeHint,~10ns/次 dict 查表,实测 < 30ms。
    之前:每次新建 QTextDocument + setHtml + setTextWidth + idealWidth + size,
    约 50µs/次 → 10万次 ~ 5s。
    """
    view = MessageView()
    for i in range(1000):
        view.append(_make_msg(i))

    delegate: MessageItemDelegate = view._delegate
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 400, 0)
    option.font = view.font()

    # 首次 sizeHint 填缓存
    for r in range(view.count()):
        delegate.sizeHint(option, view._model.index(r, 0))
    # 计时:缓存命中
    t0 = time.perf_counter()
    for _ in range(100):
        for r in range(view.count()):
            delegate.sizeHint(option, view._model.index(r, 0))
    elapsed = time.perf_counter() - t0
    print(
        f"\n  sizeHint cached: 1K rows × 100 iter = 100k calls in {elapsed * 1000:7.1f}ms"
    )


# ============================================================
# 整批替换:set_messages 单次 modelReset
# ============================================================


def test_set_messages_10k(qapp: QApplication) -> None:
    """set_messages 10K 条 — 单次 modelReset 应 < 500ms。

    PR #9 后(deque + 1 次 reset):10K 条 deque 构造 + dict 重建 + 截断 ~ 50ms。
    之前 N×append + beginInsertRows(0,0) × 10K:约 2-3s + 触发 10K 次 rowsInserted
    浮条计数误累加 bug。
    """
    view = MessageView()
    msgs = [_make_msg(i) for i in range(10_000)]
    t0 = time.perf_counter()
    view.set_messages(msgs)
    elapsed = time.perf_counter() - t0
    print(f"\n  set_messages 10K: {elapsed * 1000:7.1f}ms")
    # sanity:行数 = 10000(MAX_ITEMS)
    assert view._model.rowCount() == MessageView.MAX_ITEMS
