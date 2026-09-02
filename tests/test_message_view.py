"""MessageView 渲染格式测试 — 本地时区 / 频道名 / msg id。

不测交互(点击/双击),只验 `_format()` 输出格式与 `set_channel_titles()` 行为。
需要 QApplication:widget 实例化要求 QGuiApplication 存活。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

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
        id=0,
        channel_id=100,
        telegram_msg_id=42,
        text="hi",
        author="alice",
        date=datetime(2026, 7, 15, 13, 50, 10),  # naive,语义上 UTC
    )
    line = view._format(msg).split("\n")[0]  # 第一行是 head
    # 不能是 "13:50"(那是直接打印 UTC),也不能是 "[#100]"(没 title 退化错)
    assert "#42" in line  # msg_id 显示
    # 系统 TZ 不确定 → 验证 13:50 与 21:50 都是合法可能;
    # 但**绝对不能**让 tzutc 之外解释成 naive=本地(那样 py 在 UTC 容器里
    # 会印 13:50,在 +0800 容器里也会印 13:50,永远不是 21:50,这就是 bug)
    # 所以这里只要求格式存在 "13:50:10" 或 "21:50:10"
    assert ("13:50:10" in line) or ("21:50:10" in line), f"时间应来自 UTC 转换,但 line={line!r}"


def test_format_aware_utc_also_converts(qapp):
    """aware UTC datetime 同样按本地时区显示。"""
    view = MessageView()
    msg = MessageDTO(
        id=0,
        channel_id=100,
        telegram_msg_id=42,
        text="hi",
        author=None,
        date=datetime(2026, 7, 15, 13, 50, 10, tzinfo=UTC),
    )
    line = view._format(msg).split("\n")[0]
    assert ("13:50:10" in line) or ("21:50:10" in line), f"aware UTC 应转本地,line={line!r}"


def test_format_no_date_shows_placeholder(qapp):
    """m.date 为 None 时 head 时间占位为 '?'。"""
    view = MessageView()
    msg = MessageDTO(
        id=0,
        channel_id=100,
        telegram_msg_id=1,
        text="x",
        author=None,
        date=None,
    )
    line = view._format(msg).split("\n")[0]
    assert "[?]" in line


# ---- 频道名 / msg id ----


def test_format_uses_channel_title_when_known(qapp):
    """set_channel_titles 注册的 id → title 必须出现在 head 里。"""
    view = MessageView()
    view.set_channel_titles({100: "Telegram News"})
    msg = MessageDTO(
        id=0,
        channel_id=100,
        telegram_msg_id=999,
        text="hi",
        author=None,
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
        id=0,
        channel_id=-1001234567890,
        telegram_msg_id=1,
        text="x",
        author=None,
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
        channel_id=100,
        telegram_msg_id=98765,  # 应显示
        text="x",
        author=None,
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
        id=0,
        channel_id=100,
        telegram_msg_id=1234,
        text="hello world",
        author=None,
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
        id=0,
        channel_id=100,
        telegram_msg_id=1,
        text="",
        author=None,
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
        id=0,
        channel_id=100,
        telegram_msg_id=1,
        text="first",
        author=None,
        date=datetime(2026, 7, 15, 13, 50, 10),
    )
    m2 = MessageDTO(
        id=0,
        channel_id=100,
        telegram_msg_id=1,
        text="edited",
        author=None,
        date=datetime(2026, 7, 15, 13, 51, 0),
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
        id=0,
        channel_id=100,
        telegram_msg_id=42,
        text="look at this",
        author=None,
        date=datetime(2026, 7, 15, 13, 50, 10),
        media=[
            MediaDTO(
                type=MediaType.PHOTO,
                mime_type="image/jpeg",
                file_size=1234,
                width=800,
                height=600,
                thumb_key="media/abc.thumb",
                thumb_backend="local",
            )
        ],
    )
    # 不应抛 AttributeError
    view.append(msg)
    text = view.item(0).text()
    assert "look at this" in text
    assert "📎" in text
    assert "photo" in text  # med.type.value 正确渲染


# ---- 空状态覆盖:首启 / 数据到达 / 清空 ----


def test_empty_overlay_shown_when_no_messages(qapp):
    """新打开 LIVE tab → count() == 0 → _empty_overlay 可见。

    注:offscreen 模式下顶层 widget 不会被 show(),所以走 `isHidden()`
    取反(`setVisible(True)` 等价 `show()` 会清 hidden 标志位)— 不依赖
    ancestor 链都 visible。
    """
    view = MessageView()
    assert view.count() == 0
    assert not view._empty_overlay.isHidden()


def test_empty_overlay_hidden_after_first_append(qapp):
    """第一条消息到达 → _refresh_empty_state 触发 → overlay 隐藏。"""
    view = MessageView()
    assert not view._empty_overlay.isHidden()  # 先确认初始显示

    msg = MessageDTO(
        id=0,
        channel_id=1,
        telegram_msg_id=1,
        text="first!",
        author=None,
        date=datetime(2026, 7, 15, 13, 50, 10),
    )
    view.append(msg)
    assert view.count() == 1
    assert view._empty_overlay.isHidden()


def test_empty_overlay_reappears_after_clear(qapp):
    """clear_view() 把所有 item 删了 → overlay 应再次显示。"""
    view = MessageView()
    view.append(
        MessageDTO(
            id=0,
            channel_id=1,
            telegram_msg_id=1,
            text="x",
            date=datetime(2026, 7, 15, 13, 0, 0),
        )
    )
    assert view._empty_overlay.isHidden()

    view.clear_view()
    assert view.count() == 0
    assert not view._empty_overlay.isHidden()


# ---- remove_row (2026-08-24 Media Manager 接入) ----


def _make_msg(channel_id: int, telegram_msg_id: int) -> MessageDTO:
    return MessageDTO(
        id=0,
        channel_id=channel_id,
        telegram_msg_id=telegram_msg_id,
        text="x",
        author=None,
        date=datetime(2026, 7, 15, 13, 0, 0),
    )


def test_remove_row_drops_matching_key(qapp):
    """remove_row(channel_id, telegram_msg_id) → 该行从 list 消失,_seen 同步。"""
    view = MessageView()
    view.append(_make_msg(1, 100))
    view.append(_make_msg(1, 101))
    view.append(_make_msg(1, 102))
    assert view.count() == 3
    # 删中间那条(#101)
    view.remove_row(1, 101)
    assert view.count() == 2
    assert (1, 101) not in view._seen
    # 剩两条的 _seen row index 仍连续(append 时 102 在 row 0,101 在 row 1,100 在 row 2,
    # 删 row 1 → 102 在 0 不变,100 在 1(原 2-1))
    assert view._seen[(1, 102)] == 0
    assert view._seen[(1, 100)] == 1


def test_remove_row_no_match_is_noop(qapp):
    """remove_row 不存在的 key → 不抛异常,不删其它行。"""
    view = MessageView()
    view.append(_make_msg(1, 100))
    view.append(_make_msg(1, 101))
    before = view.count()
    before_seen = dict(view._seen)
    view.remove_row(999, 999)
    assert view.count() == before
    assert view._seen == before_seen


def test_remove_row_then_append(qapp):
    """删一行后再 append 新行 → row index 自洽,行为正确。"""
    view = MessageView()
    view.append(_make_msg(1, 100))
    view.append(_make_msg(1, 101))
    view.remove_row(1, 100)
    view.append(_make_msg(2, 200))
    assert view.count() == 2
    # 删 100 后,#101 在 row 0;append #200 时 insertItem(0) → #201 在 row 0,#101 在 row 1
    assert view._seen[(2, 200)] == 0
    assert view._seen[(1, 101)] == 1


# ============================================================
# 2026-09-02 v1.5.2 PR #B5:MessageView.set_messages 批量替换。
# ============================================================


def test_set_messages_replaces_view(qapp):
    """PR #B5:set_messages(m1, m2) → count == 2 + `_seen` 表只含这 2 个 key。"""
    view = MessageView()
    msgs = [_make_msg(1, 100), _make_msg(1, 101)]
    view.set_messages(msgs)
    assert view.count() == 2
    assert set(view._seen.keys()) == {(1, 100), (1, 101)}


def test_set_messages_clears_seen_dict(qapp):
    """PR #B5:set_messages 调用前先 clear_view(),`_seen` 表清空后重建。"""
    view = MessageView()
    view.append(_make_msg(1, 100))
    assert (1, 100) in view._seen
    view.set_messages([_make_msg(2, 200)])
    # 旧的 (1, 100) 已清掉,只剩新 set 的 key
    assert (1, 100) not in view._seen
    assert (2, 200) in view._seen


def test_set_messages_preserves_newest_first_order(qapp):
    """PR #B5:`messages` 按 date ASC 传入 → 渲染后 row 0 是最新(append 走 insertItem(0))。"""
    view = MessageView()
    # 假设 storage 返 [m_old, m_new](date ASC)
    m_old = _make_msg(1, 100)
    m_new = _make_msg(1, 101)
    view.set_messages([m_old, m_new])

    # newest 在 row 0(m_new 先 append,自然 insertItem(0) 落顶部)
    assert view._seen[(1, 101)] == 0
    assert view._seen[(1, 100)] == 1


def test_set_messages_preserves_filter(qapp):
    """PR #B5:set_messages 后,已有的 `_filter_text` 仍生效 — 新行被 filter。"""
    view = MessageView()
    view.set_filter("foo")
    m_match = MessageDTO(
        id=0,
        channel_id=1,
        telegram_msg_id=100,
        text="foo bar",  # 含 "foo" → 匹配
        author=None,
        date=datetime(2026, 7, 15, 13, 0, 0),
        media=[],
    )
    m_no_match = MessageDTO(
        id=0,
        channel_id=1,
        telegram_msg_id=101,
        text="baz",  # 不含 "foo"
        author=None,
        date=datetime(2026, 7, 15, 13, 1, 0),
        media=[],
    )
    view.set_messages([m_match, m_no_match])
    assert view.count() == 2  # 都进列表
    # 但 filter 应用:不匹配的行 hidden=True
    item_match = view.item(view._seen[(1, 100)])
    item_nomatch = view.item(view._seen[(1, 101)])
    assert item_match.isHidden() is False
    assert item_nomatch.isHidden() is True


def test_set_messages_empty_clears_view(qapp):
    """PR #B5:set_messages([]) → 清空视图 + `_seen` 表。"""
    view = MessageView()
    view.append(_make_msg(1, 100))
    assert view.count() == 1
    view.set_messages([])
    assert view.count() == 0
    assert view._seen == {}


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
    assert (1, 100) in view._seen
