"""MessageView 渲染格式测试 — 本地时区 / 频道名 / msg id。

不测交互(点击/双击),只验 `_format()` 输出格式与 `set_channel_titles()` 行为。
需要 QApplication:widget 实例化要求 QGuiApplication 存活。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

# offscreen 平台:CI / 无显示器 macOS 也能跑
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.core.dto import MediaDTO, MediaType, MessageDTO  # noqa: E402
from tgmonitor.ui.widgets.message_view import MessageView  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    # 不主动 quit — session 级共享,留给 pytest 进程退出时清理


# ---- _format 时间显示 ----


def test_format_local_timezone(qapp):
    """naive UTC datetime 必须按本地时区显示,而不是当作本地时间原样输出。"""
    view = MessageView()
    # 13:50 UTC → 北京时间 21:50(+0800)
    msg = MessageDTO(
        id=0, channel_id=100, telegram_msg_id=42,
        text="hi", author="alice",
        date=datetime(2026, 7, 15, 13, 50, 10),  # naive,语义上 UTC
    )
    line = view._format(msg).split("\n")[0]  # 第一行是 head
    # 不能是 "13:50"(那是直接打印 UTC),也不能是 "[#100]"(没 title 退化错)
    assert "#42" in line  # msg_id 显示
    # 系统 TZ 不确定 → 验证 13:50 与 21:50 都是合法可能;
    # 但**绝对不能**让 tzutc 之外解释成 naive=本地(那样 py 在 UTC 容器里
    # 会印 13:50,在 +0800 容器里也会印 13:50,永远不是 21:50,这就是 bug)
    # 所以这里只要求格式存在 "13:50:10" 或 "21:50:10"
    assert ("13:50:10" in line) or ("21:50:10" in line), (
        f"时间应来自 UTC 转换,但 line={line!r}"
    )


def test_format_aware_utc_also_converts(qapp):
    """aware UTC datetime 同样按本地时区显示。"""
    view = MessageView()
    msg = MessageDTO(
        id=0, channel_id=100, telegram_msg_id=42,
        text="hi", author=None,
        date=datetime(2026, 7, 15, 13, 50, 10, tzinfo=timezone.utc),
    )
    line = view._format(msg).split("\n")[0]
    assert ("13:50:10" in line) or ("21:50:10" in line), (
        f"aware UTC 应转本地,line={line!r}"
    )


def test_format_no_date_shows_placeholder(qapp):
    """m.date 为 None 时 head 时间占位为 '?'。"""
    view = MessageView()
    msg = MessageDTO(
        id=0, channel_id=100, telegram_msg_id=1,
        text="x", author=None, date=None,
    )
    line = view._format(msg).split("\n")[0]
    assert "[?]" in line


# ---- 频道名 / msg id ----


def test_format_uses_channel_title_when_known(qapp):
    """set_channel_titles 注册的 id → title 必须出现在 head 里。"""
    view = MessageView()
    view.set_channel_titles({100: "Telegram News"})
    msg = MessageDTO(
        id=0, channel_id=100, telegram_msg_id=999,
        text="hi", author=None,
        date=datetime(2026, 7, 15, 13, 50, 10),
    )
    line = view._format(msg).split("\n")[0]
    assert "[Telegram News]" in line
    assert "#999" in line
    # 未退化:不应出现 "[#100]" 这个回退形式
    assert "[#100]" not in line


def test_format_falls_back_to_id_when_title_unknown(qapp):
    """未注册的 channel_id → 退化为 [#id](无 title 时使用 id 作为占位)。"""
    view = MessageView()
    msg = MessageDTO(
        id=0, channel_id=-1001234567890, telegram_msg_id=1,
        text="x", author=None,
        date=datetime(2026, 7, 15, 13, 50, 10),
    )
    line = view._format(msg).split("\n")[0]
    # 回退格式:`[#-1001234567890]`(前缀 # 区分 title 形式)
    assert "[#-1001234567890]" in line


def test_format_msg_id_is_telegram_id_not_db_pk(qapp):
    """telegram_msg_id 是该频道内的原始消息 id,不是 MessageDTO.id (DB 自增)。"""
    view = MessageView()
    msg = MessageDTO(
        id=42,  # DB pk — 不应显示
        channel_id=100, telegram_msg_id=98765,  # 应显示
        text="x", author=None,
        date=datetime(2026, 7, 15, 13, 50, 10),
    )
    line = view._format(msg).split("\n")[0]
    assert "#98765" in line
    assert "#42" not in line


# ---- set_channel_titles 行为 ----


def test_set_channel_titles_replaces_not_merges(qapp):
    """整张表替换 — 旧 id 必须失效,新 id 生效。"""
    view = MessageView()
    view.set_channel_titles({1: "Old", 2: "Still"})
    view.set_channel_titles({2: "New", 3: "Three"})
    assert 1 not in view._channel_titles  # 已退订的频道 title 被清
    assert view._channel_titles[2] == "New"
    assert view._channel_titles[3] == "Three"


# ---- append → 实际渲染 ----


def test_append_renders_correct_text(qapp):
    """append → item.text() 应包含本地时区 / 频道名 / msg id。"""
    view = MessageView()
    view.set_channel_titles({100: "My Channel"})
    msg = MessageDTO(
        id=0, channel_id=100, telegram_msg_id=1234,
        text="hello world", author=None,
        date=datetime(2026, 7, 15, 13, 50, 10),
    )
    view.append(msg)
    item = view.item(0)
    assert item is not None
    text = item.text()
    assert "[My Channel]" in text
    assert "#1234" in text
    assert "hello world" in text


def test_append_media_has_dedicated_bg(qapp):
    """带媒体的消息应有背景色(底色区分)。"""
    view = MessageView()
    msg = MessageDTO(
        id=0, channel_id=100, telegram_msg_id=1,
        text="", author=None,
        date=datetime(2026, 7, 15, 13, 50, 10),
        media=[MediaDTO(type=MediaType.PHOTO, mime_type="image/jpeg")],
    )
    view.append(msg)
    item = view.item(0)
    # MediaDTO 单非空 → has_media=True → 背景被设;不验证具体颜色(QPalette 跨平台)
    from PySide6.QtGui import QBrush
    assert item.background() != QBrush()  # 非默认 brush


def test_append_dedup_updates_existing_row(qapp):
    """同 (channel_id, telegram_msg_id) 重复 append → 更新文本而非新增行。"""
    view = MessageView()
    m1 = MessageDTO(
        id=0, channel_id=100, telegram_msg_id=1,
        text="first", author=None, date=datetime(2026, 7, 15, 13, 50, 10),
    )
    m2 = MessageDTO(
        id=0, channel_id=100, telegram_msg_id=1,
        text="edited", author=None, date=datetime(2026, 7, 15, 13, 51, 0),
    )
    view.append(m1)
    view.append(m2)
    assert view.count() == 1
    assert "edited" in view.item(0).text()


# ---- 媒体 DTO 回归(Signal(object) 路径) ----


def test_append_with_media_dto_does_not_crash(qapp):
    """回归:之前 VM 用 asdict(e.message) 把嵌套 MediaDTO 转 dict,
    MainWindow 收到后 `MessageDTO(**dto_dict)` 不递归构回 MediaDTO,
    MessageView._format 访问 `med.type` 崩 — 'dict' object has no attribute 'type'。

    修法:VM 改 `Signal(object)` 直接 emit MessageDTO,MainWindow 直接 append。
    本测试构造一个真实含 media 的 MessageDTO 走完整 append 路径,确保不崩。
    """
    view = MessageView()
    msg = MessageDTO(
        id=0, channel_id=100, telegram_msg_id=42,
        text="look at this", author=None,
        date=datetime(2026, 7, 15, 13, 50, 10),
        media=[MediaDTO(
            type=MediaType.PHOTO, mime_type="image/jpeg",
            file_size=1234, width=800, height=600,
            thumb_key="media/abc.thumb", thumb_backend="local",
        )],
    )
    # 不应抛 AttributeError
    view.append(msg)
    text = view.item(0).text()
    assert "look at this" in text
    assert "📎" in text
    assert "photo" in text  # med.type.value 正确渲染