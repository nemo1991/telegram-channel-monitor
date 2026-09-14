"""PR #4 详情面板可拖拽 + show_message 增量重建测试 — 2026-09-14 v1.7.5。

覆盖场景:
- MessageDetail 删 `setMaximumWidth(420)`(让 QSplitter 接管,用户可拖宽)
- show_message 同 cid+mid → 跳过 rebuild(保留滚动位置)
- show_message 不同 cid/mid → 走 rebuild 路径
- show_message(None) → 走 empty state 重建路径
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.core.dto import MessageDTO  # noqa: E402
from tgmonitor.ui.widgets.message_detail import MessageDetail  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_msg(cid: int = 1, mid: int = 100, text: str = "hello") -> MessageDTO:
    """构造最小 MessageDTO — 详情面板只需 cid/mid/text。"""
    from datetime import UTC, datetime

    return MessageDTO(
        id=cid * 1_000_000 + mid,
        channel_id=cid,
        telegram_msg_id=mid,
        text=text,
        author="tester",
        date=datetime(2026, 9, 14, 10, 0, 0, tzinfo=UTC),
    )


def test_message_detail_no_maximum_width(qapp) -> None:
    """2026-09-14 v1.7.5 PR #4 (P0-H):MessageDetail 删 setMaximumWidth(420) — QSplitter 接管。"""
    detail = MessageDetail()
    detail.show()
    qapp.processEvents()
    # 仍设 minimum=280(防 splitter 拖到极窄被隐)
    assert detail.minimumWidth() == 280, "min 应保留 280"
    # 关键:max 应该 = 16777215(QWidget 默认 MAX,即不限制)
    assert detail.maximumWidth() >= 10000, (
        f"max 应不限制(splitter 接管),实际 {detail.maximumWidth()}"
    )


def test_show_message_same_cid_mid_keeps_widget(qapp) -> None:
    """2026-09-14 v1.7.5 PR #4 (P2):同 cid+mid 调 show_message → 不重建 widget,滚动位置保留。"""
    detail = MessageDetail()
    detail.show()
    qapp.processEvents()

    m = _make_msg(cid=1, mid=100, text="第一条")
    detail.show_message(m)
    qapp.processEvents()
    first_widget = detail.widget()

    # 再调同 cid+mid(模拟 reactions 推送触发重新 emit)
    m2 = _make_msg(cid=1, mid=100, text="第一条(updated)")
    detail.show_message(m2)
    qapp.processEvents()
    same_widget = detail.widget()

    assert first_widget is same_widget, "同 cid+mid 应跳过 rebuild(保留 widget 引用)"
    assert detail._current is m2  # noqa: SLF001 — DTO 引用应刷


def test_show_message_different_cid_rebuilds(qapp) -> None:
    """不同 cid → 必须 rebuild(走新 widget)。"""
    detail = MessageDetail()
    detail.show()
    qapp.processEvents()

    detail.show_message(_make_msg(cid=1, mid=100))
    qapp.processEvents()
    first_widget = detail.widget()

    detail.show_message(_make_msg(cid=2, mid=200))
    qapp.processEvents()
    second_widget = detail.widget()

    assert first_widget is not second_widget, "不同 cid 应 rebuild(new widget 引用)"


def test_show_message_different_mid_rebuilds(qapp) -> None:
    """同 cid 不同 mid → 仍 rebuild。"""
    detail = MessageDetail()
    detail.show()
    qapp.processEvents()

    detail.show_message(_make_msg(cid=1, mid=100))
    qapp.processEvents()
    first_widget = detail.widget()

    detail.show_message(_make_msg(cid=1, mid=200))
    qapp.processEvents()
    second_widget = detail.widget()

    assert first_widget is not second_widget


def test_show_message_none_resets_to_empty_state(qapp) -> None:
    """None → 走 _build_empty_state(清 widget,回到占位)。"""
    detail = MessageDetail()
    detail.show()
    qapp.processEvents()

    detail.show_message(_make_msg(cid=1, mid=100))
    qapp.processEvents()
    assert detail.widget() is not None

    detail.show_message(None)
    qapp.processEvents()
    # empty state 也是 widget(占位 panel),但 _current 清空
    assert detail._current is None  # noqa: SLF001


def test_show_message_from_empty_to_msg_rebuilds(qapp) -> None:
    """empty → msg → 同 msg → 应只在第一次 rebuild。"""
    detail = MessageDetail()
    detail.show()
    qapp.processEvents()

    # empty state(初始) → 有 widget(占位)
    detail.show_message(_make_msg(cid=1, mid=100))
    qapp.processEvents()
    widget_after_first = detail.widget()

    # 同 cid+mid 再调 → 跳过
    detail.show_message(_make_msg(cid=1, mid=100, text="refresh"))
    qapp.processEvents()
    assert detail.widget() is widget_after_first


def test_message_detail_after_show_message_can_scroll(qapp) -> None:
    """show_message 后能拿到 QScrollArea 内部 scroll bar(留作 splitter 集成测试)。"""
    detail = MessageDetail()
    detail.show()
    qapp.processEvents()

    detail.show_message(_make_msg(cid=1, mid=100, text="line1\n" * 50))
    qapp.processEvents()
    sb = detail.verticalScrollBar()
    # 能改 value(滚动位置)即说明 splittable 容器工作
    sb.setValue(50)
    qapp.processEvents()
    assert sb.value() == 50, "QScrollArea 滚动条工作正常"

    # 同 cid+mid 调 → 滚动位置应保留(skipped rebuild)
    detail.show_message(_make_msg(cid=1, mid=100))
    qapp.processEvents()
    assert detail.verticalScrollBar().value() == 50, "同 cid+mid 应保留滚动位置"


# ---- 静态检查:splitter 在 main_window 中接管 ----


def test_main_window_live_page_uses_qsplitter() -> None:
    """2026-09-14 v1.7.5 PR #4 (P0-H):main_window LIVE body 改 QSplitter。"""
    import re
    from pathlib import Path

    src = Path("src/tgmonitor/ui/main_window.py").read_text(encoding="utf-8")
    # LIVE body 应是 QSplitter,不是 QHBoxLayout 装 live_view + message_detail
    assert "QSplitter(Qt.Horizontal)" in src, (
        "main_window.py 没找到 QSplitter(Qt.Horizontal) — LIVE body 应是 splitter"
    )
    # 不再直接用 QHBoxLayout 装 live_view + message_detail
    pattern = re.compile(r"QHBoxLayout\(\)[\s\S]{0,200}live_view[\s\S]{0,200}message_detail")
    assert not pattern.search(src), (
        "main_window.py 还有旧的 QHBoxLayout 装 live_view + message_detail"
    )


def test_message_detail_no_set_maximum_width_call() -> None:
    """MessageDetail 不再调 setMaximumWidth(420) — 静态检查(忽略注释)。"""
    from pathlib import Path

    src = Path("src/tgmonitor/ui/widgets/message_detail.py").read_text(encoding="utf-8")
    # 删注释行 + 空行后扫实际代码
    code_lines = [
        line for line in src.splitlines() if line.strip() and not line.strip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert "setMaximumWidth" not in code, (
        "message_detail.py 代码还有 setMaximumWidth 调用 — 应让 QSplitter 接管"
    )
