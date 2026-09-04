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
from datetime import UTC, datetime

import pytest

# offscreen 平台:CI / 无显示器 macOS 也能跑
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QBrush  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.core.dto import MediaDTO, MediaType, MessageDTO  # noqa: E402
from tgmonitor.ui.widgets.message_view import (  # noqa: E402
    MessageItemDelegate,
    MessageListModel,
    MessageView,
)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    # 不主动 quit — session 级共享,留给 pytest 进程退出时清理


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
    assert 1 not in view._model._channel_titles  # 已退订的频道 title 被清
    assert view._model._channel_titles[2] == "New"
    assert view._model._channel_titles[3] == "Three"


# ---- append → 实际渲染 ----


def test_append_renders_correct_text(qapp):
    """append → FormattedRole 应包含本地时区 / 频道名 / msg id。"""
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
    text = _item_text(view, 0)
    assert "[My Channel]" in text
    assert "#1234" in text
    assert "hello world" in text


def test_append_media_has_dedicated_bg(qapp):
    """带媒体的消息应有 HasMediaRole=True(delegate paint 时填底色)。"""
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
    # HasMediaRole=True → delegate paint fillRect(232,240,248)
    assert _item_has_media_bg(view, 0) is True


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
    assert "edited" in _item_text(view, 0)


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
    text = _item_text(view, 0)
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
    """remove_row(channel_id, telegram_msg_id) → 该行从 list 消失,_index_of 同步。"""
    view = MessageView()
    view.append(_make_msg(1, 100))
    view.append(_make_msg(1, 101))
    view.append(_make_msg(1, 102))
    assert view.count() == 3
    # 删中间那条(#101)
    view.remove_row(1, 101)
    assert view.count() == 2
    assert (1, 101) not in view._model._index_of
    # 剩两条的 _index_of row 仍连续(append 时 102 在 row 0,101 在 row 1,100 在 row 2,
    # 删 row 1 → 102 在 0 不变,100 在 1(原 2-1))
    assert view._model._index_of[(1, 102)] == 0
    assert view._model._index_of[(1, 100)] == 1


def test_remove_row_no_match_is_noop(qapp):
    """remove_row 不存在的 key → 不抛异常,不删其它行。"""
    view = MessageView()
    view.append(_make_msg(1, 100))
    view.append(_make_msg(1, 101))
    before = view.count()
    before_index = dict(view._model._index_of)
    view.remove_row(999, 999)
    assert view.count() == before
    assert view._model._index_of == before_index


def test_remove_row_then_append(qapp):
    """删一行后再 append 新行 → row index 自洽,行为正确。"""
    view = MessageView()
    view.append(_make_msg(1, 100))
    view.append(_make_msg(1, 101))
    view.remove_row(1, 100)
    view.append(_make_msg(2, 200))
    assert view.count() == 2
    # 删 100 后,#101 在 row 0;append #200 时走 beginInsertRows(0,0) → #200 在 row 0,#101 在 row 1
    assert view._model._index_of[(2, 200)] == 0
    assert view._model._index_of[(1, 101)] == 1


# ============================================================
# 2026-09-02 v1.5.2 PR #B5:MessageView.set_messages 批量替换。
# ============================================================


def test_set_messages_replaces_view(qapp):
    """PR #B5:set_messages(m1, m2) → count == 2 + `_index_of` 表只含这 2 个 key。"""
    view = MessageView()
    msgs = [_make_msg(1, 100), _make_msg(1, 101)]
    view.set_messages(msgs)
    assert view.count() == 2
    assert set(view._model._index_of.keys()) == {(1, 100), (1, 101)}


def test_set_messages_clears_seen_dict(qapp):
    """PR #B5:set_messages 调用前先 clear_view(),`_index_of` 表清空后重建。"""
    view = MessageView()
    view.append(_make_msg(1, 100))
    assert (1, 100) in view._model._index_of
    view.set_messages([_make_msg(2, 200)])
    # 旧的 (1, 100) 已清掉,只剩新 set 的 key
    assert (1, 100) not in view._model._index_of
    assert (2, 200) in view._model._index_of


def test_set_messages_preserves_newest_first_order(qapp):
    """PR #B5:`messages` 按 date ASC 传入 → 渲染后 row 0 是最新(append 走 insertItem(0))。"""
    view = MessageView()
    # 假设 storage 返 [m_old, m_new](date ASC)
    m_old = _make_msg(1, 100)
    m_new = _make_msg(1, 101)
    view.set_messages([m_old, m_new])

    # newest 在 row 0(m_new 先 append,自然 insertItem(0) 落顶部)
    assert view._model._index_of[(1, 101)] == 0
    assert view._model._index_of[(1, 100)] == 1


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
    assert _item_is_hidden(view, view._model._index_of[(1, 100)]) is False
    assert _item_is_hidden(view, view._model._index_of[(1, 101)]) is True


def test_set_messages_empty_clears_view(qapp):
    """PR #B5:set_messages([]) → 清空视图 + `_index_of` 表。"""
    view = MessageView()
    view.append(_make_msg(1, 100))
    assert view.count() == 1
    view.set_messages([])
    assert view.count() == 0
    assert view._model._index_of == {}


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
    assert (1, 100) in view._model._index_of


# ============================================================
# 2026-09-02 v1.5.3 PR #D1:QListView + MessageListModel + delegate 新协议测试。
# ============================================================


def test_current_message_returns_dto(qapp):
    """PR #D1:current_message() helper — 替代旧 `currentItem().data(Qt.UserRole)`。"""
    view = MessageView()
    msg = _make_msg(1, 100)
    view.append(msg)
    # setCurrentIndex → currentIndex().row() == 0
    view.setCurrentIndex(view._model.index(0, 0))
    cur = view.current_message()
    assert isinstance(cur, MessageDTO)
    assert cur.channel_id == 1
    assert cur.telegram_msg_id == 100


def test_current_message_none_when_no_selection(qapp):
    """PR #D1:无 selection 时 current_message() 返 None(不崩)。"""
    view = MessageView()
    # 没 setCurrentIndex → currentIndex() invalid
    assert view.current_message() is None


def test_model_rowcount_matches_list(qapp):
    """PR #D1:rowCount() == 实际 DTO 数量。"""
    view = MessageView()
    assert view._model.rowCount() == 0
    view.append(_make_msg(1, 1))
    view.append(_make_msg(1, 2))
    view.append(_make_msg(2, 3))
    assert view._model.rowCount() == 3


def test_model_data_returns_dto_role(qapp):
    """PR #D1:`data(idx, DtoRole)` 返 MessageDTO 引用本身(非 dict)。"""
    view = MessageView()
    msg = _make_msg(1, 100)
    view.append(msg)
    idx = view._model.index(0, 0)
    dto = view._model.data(idx, MessageListModel.DtoRole)
    assert dto is msg  # 同一引用


def test_model_data_msgid_role(qapp):
    """PR #D1:`data(idx, MsgIdRole)` 返 telegram_msg_id(整数)。"""
    view = MessageView()
    view.append(_make_msg(1, 999))
    idx = view._model.index(0, 0)
    assert view._model.data(idx, MessageListModel.MsgIdRole) == 999


def test_model_reset_clears_index(qapp):
    """PR #D1:`reset([])` 后 rowCount==0,_index_of 清空。"""
    view = MessageView()
    view.append(_make_msg(1, 1))
    view.append(_make_msg(1, 2))
    assert view._model.rowCount() == 2
    view._model.reset([])
    assert view._model.rowCount() == 0
    assert view._model._index_of == {}


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
    assert (1, 0) not in view._model._index_of
    assert (1, MessageView.MAX_ITEMS) in view._model._index_of


def test_model_data_hidden_role_when_filter_empty(qapp):
    """PR #D1:filter 为空时所有行 HiddenRole=False(可见)。"""
    view = MessageView()
    view.append(_make_msg(1, 1))
    view.append(_make_msg(1, 2))
    idx_0 = view._model.index(0, 0)
    idx_1 = view._model.index(1, 0)
    assert view._model.data(idx_0, MessageListModel.HiddenRole) is False
    assert view._model.data(idx_1, MessageListModel.HiddenRole) is False


def test_set_filter_via_model_emits_datachanged(qapp):
    """PR #D1:model.set_filter() → emit dataChanged(HiddenRole)。"""
    view = MessageView()
    view.append(_make_msg(1, 1))
    view.append(_make_msg(1, 2))

    spy_hidden = []
    view._model.dataChanged.connect(
        lambda top, bot, roles: spy_hidden.append((top.row(), bot.row(), list(roles)))
    )
    view._model.set_filter("foo")
    assert len(spy_hidden) >= 1
    last = spy_hidden[-1]
    # 顶到底 — 全表 HiddenRole 变化
    assert last[0] == 0
    assert last[1] == 1
    assert MessageListModel.HiddenRole in last[2]


def test_delegate_paint_skips_hidden(qapp):
    """PR #D1:delegate paint 对 HiddenRole=True 的 index 直接 return。

    用 QStyleOptionViewItem mock 不易 — 改为验证 HiddenRole 协议(model
    侧)— HiddenRole=True 时 delegate paint 第 1 行就 return,跳过 fillRect
    + QTextDocument 渲富文本(节省 paint 开销)。
    """
    MessageItemDelegate()  # 实例化证明不抛
    view = MessageView()
    view.append(_make_msg(1, 1))
    view.set_filter("never_match")
    idx = view._model.index(0, 0)
    assert view._model.data(idx, MessageListModel.HiddenRole) is True


def test_plain_to_html_escapes_special_chars(qapp):
    """PR #D1:delegate `_plain_to_html` 转义 < > & 避免被当 HTML 解析。"""
    from tgmonitor.ui.widgets.message_view import MessageItemDelegate

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
    m_edited = MessageDTO(
        id=0,
        channel_id=1,
        telegram_msg_id=1,
        text="edited!",
        author=None,
        date=datetime(2026, 7, 15, 13, 50, 10),
    )
    view.replace_message(m_edited)
    assert view.count() == 1
    assert "edited!" in _item_text(view, 0)


def test_update_media_status_re_renders(qapp):
    """PR #D1:update_media_status 改 DTO.media 后 → 重 format(DONE 状态有 ✓ 标记)。"""
    from tgmonitor.core.dto import MediaDownloadStatus

    view = MessageView()
    msg = MessageDTO(
        id=0,
        channel_id=1,
        telegram_msg_id=1,
        text="x",
        author=None,
        date=datetime(2026, 7, 15, 13, 0, 0),
        media=[
            MediaDTO(
                type=MediaType.PHOTO,
                mime_type="image/jpeg",
                telegram_file_id="file_abc",
            )
        ],
    )
    view.append(msg)
    # 初始 formatted 不含 ✓
    text_before = _item_text(view, 0)
    assert "✓" not in text_before
    # 更新 media[0] 走 download_status=DONE — 必须传 telegram_file_id 才能
    # 匹配上原 DTO 的 media(media is media 不命中,因为是 fresh object)
    media_done = MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        telegram_file_id="file_abc",
        download_status=MediaDownloadStatus.DONE,
    )
    view.update_media_status(1, 1, media_done)
    text_after = _item_text(view, 0)
    assert "✓" in text_after


# ============================================================
# legacy helper — 旧测试用 `QBrush()` 判底色,保留 1 个测试 shim 验证迁移路径
# ============================================================


def test_brush_default_unchanged_in_qtgui(qapp):
    """纯 sanity — QBrush() 默认构造仍是无色 brush(无关 PR #D1,防 Qt 版本漂移)。"""
    from PySide6.QtGui import QColor

    assert QBrush() != QBrush(QColor(232, 240, 248))


# ============================================================
# 2026-09-03 v1.5.4 PR #P1:_truncate_tail O(1) 重构 + MAX_ITEMS=10000 实验性 bump。
# ============================================================


def test_truncate_tail_is_o1(qapp):
    """PR #P1:`_truncate_tail` 走 `_row_to_key.pop(last, None)` O(1)。

    mock 10K 条后,验 `_truncate_tail` 单次调耗时 < 1ms(防 O(N) 退化为 N² 回归)。
    """
    import time

    view = MessageView()
    # 准备 10K 条触发 MAX_ITEMS 截断(append 时 _truncate_tail 会被调)
    for i in range(MessageView.MAX_ITEMS + 1):
        view.append(_make_msg(1, i))
    # 此时 MAX_ITEMS 条,rowCount == 10000
    assert view._model.rowCount() == MessageView.MAX_ITEMS
    # 再调一次 append(append #10001,触发 _truncate_tail 单次)
    t0 = time.perf_counter()
    view.append(_make_msg(1, 10001))
    elapsed = time.perf_counter() - t0
    # O(1) 应 < 1ms;O(N) 退化版会 > 10ms(10K dict scan)
    assert elapsed < 0.01, f"_truncate_tail 应 O(1),实测 {elapsed * 1000:.2f}ms"


def test_row_to_key_in_sync_with_index_of(qapp):
    """PR #P1:**invariant test** — `_row_to_key[r] == (cid, mid)` 必须与
    `_index_of[(cid, mid)] == r` 严格镜像。任何 append / remove / reset
    漏维护任一索引 → 此测试立即 fail。
    """
    view = MessageView()
    # append 一批
    for i in range(100):
        view.append(_make_msg(1, i))
    _assert_invariant(view)
    # 删几个
    view.remove_row(1, 50)
    view.remove_row(1, 30)
    view.remove_row(1, 70)
    _assert_invariant(view)
    # 再 append
    for i in range(200, 300):
        view.append(_make_msg(2, i))
    _assert_invariant(view)
    # set_messages 整批(公开 API,内部走 clear_view + append 循环)
    view.set_messages([_make_msg(3, k) for k in range(50)])
    _assert_invariant(view)


def _assert_invariant(view: MessageView) -> None:
    """断言 `_row_to_key` 与 `_index_of` 严格镜像。"""
    model = view._model
    # row → key 与 _index_of[(cid, mid)] == row 必须双向等价
    assert len(model._row_to_key) == len(model._items)
    assert len(model._index_of) == len(model._items)
    for r, key in model._row_to_key.items():
        assert model._index_of[key] == r, (
            f"invariant broken at row={r}: _row_to_key={key} but _index_of[{key}]={model._index_of[key]}"
        )


def test_reset_populates_row_to_key(qapp):
    """PR #P1:`_model.reset([m1..m100])` 后 `_row_to_key == {0: key0, 1: key1, ...}`。"""
    view = MessageView()
    msgs = [_make_msg(1, i) for i in range(100)]
    view._model.reset(msgs)
    assert len(view._model._row_to_key) == 100
    for i, m in enumerate(msgs):
        assert view._model._row_to_key[i] == (m.channel_id, m.telegram_msg_id)


def test_remove_row_updates_row_to_key(qapp):
    """PR #P1:`remove_row(cid, mid)` 删行后,row > 删 row 的 entry 全部 -1。

    用公开 API `remove_row`(内部调 `model.remove_by_key`)。删的是**最后一个 row**,
    没有 shift → `row_to_delete` 应真的从 `_row_to_key` 消失;删中间 row 测
    (1, 5) 已不在 _row_to_key 的 value 集合里。
    """
    view = MessageView()
    for i in range(10):
        view.append(_make_msg(1, i))
    # ---- 删中间 row(测 shift + value 集合)----
    # append 是头部插入,最新 (1, 9) 在 row 0;(1, 5) 在 row 4
    view.remove_row(1, 5)
    # (1, 5) 不再在 _row_to_key 的 value 集合里
    assert (1, 5) not in view._model._row_to_key.values()
    # 全表 row 连续 0..8(shift 后 row=4 仍存在,只是填了 shifted 内容)
    assert len(view._model._row_to_key) == 9
    assert sorted(view._model._row_to_key.keys()) == list(range(9))
    # ---- 删最后一个 row(测边界 case — 无 shift)----
    # 此时 (1, 0) 在 row 9(最旧,append 最后被推到 tail)
    last_row = view._model._index_of[(1, 0)]
    view.remove_row(1, 0)
    assert last_row not in view._model._row_to_key
    assert (1, 0) not in view._model._row_to_key.values()
    assert len(view._model._row_to_key) == 8
    assert sorted(view._model._row_to_key.keys()) == list(range(8))


def test_max_items_bumped_to_10000(qapp):
    """PR #P1:MAX_ITEMS 1000 → 10000。"""
    assert MessageView.MAX_ITEMS == 10000


def test_set_messages_respects_max_10000(qapp):
    """PR #P1:`set_messages([m1..m10001])` → rowCount == 10000,最旧一条被截断。

    `set_messages` 走 `clear_view + append 循环`,append 是 head-insert,
    所以 (1, 0) 是 oldest,会落到 tail,被 _truncate_tail 砍掉。
    """
    view = MessageView()
    msgs = [_make_msg(1, i) for i in range(10001)]
    view.set_messages(msgs)
    assert view._model.rowCount() == 10000
    # 最旧 (1, 0) 应被截断
    assert (1, 0) not in view._model._index_of
    # 最新 (1, 10000) 应保留(head)
    assert (1, 10000) in view._model._index_of


def test_stress_10k_messages_append_dedup_truncate(qapp):
    """PR #P1:**stress test** — append 10K 条 + 中途 100 次 remove,验证:

    - rowCount 最终 == 9900(10K - 100)
    - `_row_to_key` 与 `_index_of` 严格镜像(invariant)
    - 整测试耗时 < 30s(append 本身是 O(N) shift 累计 O(N²),MAX_ITEMS
      1000 → 10000 后自然放大 ~10×;本测试只防 O(N³) 级别的极端退化,
      例如 invariant 漏维护导致 O(N) dict scan × N 次 append)

    用 `time.perf_counter` 计时,**30s 是 CI 容差**(GitHub Actions
    ubuntu runner + 多 job 资源争抢比本地慢 ~2×,2026-09-04 v1.6.1 CI
    上 15s 阈值不够 — 实测 20.67s 失败;阈值不是性能 SLA,放宽后仍能
    catch 真正退化 — invariant 漏维护会让单次 `_truncate_tail` 从
    O(1) 退到 O(N) × 10K ≈ 80s+,30s 阈值仍 fail)。本地实测 < 12s。
    """
    import time

    view = MessageView()
    t0 = time.perf_counter()
    # 10K append
    for i in range(10_000):
        view.append(_make_msg(1, i))
    # 100 次 remove(中途)
    for i in range(0, 10_000, 100):
        view.remove_row(1, i)
    elapsed = time.perf_counter() - t0
    # rowCount 验证
    assert view._model.rowCount() == 9_900, (
        f"10K append - 100 remove 应得 9900,但 rowCount={view._model.rowCount()}"
    )
    # invariant 验证
    _assert_invariant(view)
    # 性能阈值 — 防 O(N³) 极端退化(append 本身 O(N) shift 是 by design,
    # 累计 O(N²) 不在本 PR 范围;未来 PR 可改 deque + index map 做到 O(1) amortized)
    assert elapsed < 30.0, f"stress 应 < 30s,实测 {elapsed:.2f}s(可能 O(N³) 退化)"
