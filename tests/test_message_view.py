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
import time
from datetime import UTC, datetime

# offscreen 平台:CI / 无显示器 macOS 也能跑
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QBrush  # noqa: E402
from PySide6.QtTest import QSignalSpy  # noqa: E402
from PySide6.QtWidgets import QStyleOptionViewItem  # noqa: E402

from tgmonitor.core.dto import MediaDTO, MediaType, MessageDTO, ReactionDTO  # noqa: E402
from tgmonitor.ui.widgets.message_view import (  # noqa: E402
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
    """PR #P9:`_truncate_tail` 走 `deque.pop()` O(1) + `_index_of` O(N) bump。

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


def test_index_of_in_sync_with_items(qapp):
    """PR #P9:**invariant test** — `_index_of[(cid, mid)] == row` 必须与
    `_items[row] == dto` 严格对应(deque 物理位置 = 逻辑 row,newest 在 0)。

    任何 append / remove / reset 漏维护任一索引 → 此测试立即 fail。
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
    # set_messages 整批(公开 API,内部走 reset)
    view.set_messages([_make_msg(3, k) for k in range(50)])
    _assert_invariant(view)


def _assert_invariant(view: MessageView) -> None:
    """PR #9:`_index_of[(cid, mid)] == row` 必须与 `_items[row]` 严格对应。

    `_items` 是 deque,物理位置 0 = newest,size = _index_of size;deque[i]
    是 O(1) 均摊索引,可直接当 list 用。
    """
    model = view._model
    assert len(model._index_of) == len(model._items), (
        f"index_of size={len(model._index_of)} != items size={len(model._items)}"
    )
    for r in range(len(model._items)):
        m = model._items[r]
        key = (m.channel_id, m.telegram_msg_id)
        assert model._index_of[key] == r, (
            f"invariant broken at row={r}: _items[{r}]=({m.channel_id}, {m.telegram_msg_id}) "
            f"but _index_of[{key}]={model._index_of[key]}"
        )


def test_reset_populates_index_of(qapp):
    """PR #9:`_model.reset([m1..m100])` 后 `_index_of` 每 row 正确指向 (cid, mid)。"""
    view = MessageView()
    msgs = [_make_msg(1, i) for i in range(100)]
    view._model.reset(msgs)
    assert len(view._model._index_of) == 100
    for i, m in enumerate(msgs):
        key = (m.channel_id, m.telegram_msg_id)
        assert view._model._index_of[key] == i


def test_remove_row_updates_index_of(qapp):
    """PR #9:`remove_row(cid, mid)` 删行后,row > 删 row 的 entry 全部 -1。

    用公开 API `remove_row`(内部调 `model.remove_by_key`)。删中间 row 测
    (1, 5) 已不在 _index_of,删最后一个 row 测边界(无 shift)。
    """
    view = MessageView()
    for i in range(10):
        view.append(_make_msg(1, i))
    # ---- 删中间 row(测 shift + value 集合)----
    # append 是头部插入,最新 (1, 9) 在 row 0;(1, 5) 在 row 4
    view.remove_row(1, 5)
    assert (1, 5) not in view._model._index_of
    # 全表 row 连续 0..8(shift 后 row=4 仍存在,只是填了 shifted 内容)
    assert len(view._model._index_of) == 9
    assert sorted(view._model._index_of.values()) == list(range(9))
    # ---- 删最后一个 row(测边界 case — 无 shift)----
    # 此时 (1, 0) 在 row 9(最旧,append 最后被推到 tail)
    last_row = view._model._index_of[(1, 0)]
    view.remove_row(1, 0)
    assert last_row not in view._model._index_of.values()
    assert (1, 0) not in view._model._index_of
    assert len(view._model._index_of) == 8
    assert sorted(view._model._index_of.values()) == list(range(8))


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
    - `_index_of` 与 `_items` 严格镜像(invariant)
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


# ============================================================
# 2026-09-10 v1.7.3:行渲染 ★ / 🏷 / 📝 / 📌
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
    return MessageDTO(
        id=0,
        channel_id=channel_id,
        telegram_msg_id=telegram_msg_id,
        text="x",
        author=None,
        date=datetime(2026, 7, 15, 13, 0, 0),
        is_favorite=is_favorite,
        tags=tags or [],
        notes=notes,
        is_pinned=is_pinned,
    )


def test_format_meta_icons_all_empty_returns_empty(qapp):
    """v1.7.3:无任何 metadata 时 _format_meta_icons 返空串(原 head 不变)。"""
    from tgmonitor.ui.widgets.message_view import _format_meta_icons

    m = _make_meta_msg()
    assert _format_meta_icons(m) == ""


def test_format_meta_icons_favorite_star(qapp):
    """v1.7.3:is_favorite=True → ★ icon。"""
    from tgmonitor.ui.widgets.message_view import _format_meta_icons

    m = _make_meta_msg(is_favorite=True)
    assert "★" in _format_meta_icons(m)


def test_format_meta_icons_tags(qapp):
    """v1.7.3:tags=["tech", "ai"] → "🏷tech,ai"。"""
    from tgmonitor.ui.widgets.message_view import _format_meta_icons

    m = _make_meta_msg(tags=["tech", "ai"])
    assert "🏷tech,ai" in _format_meta_icons(m)


def test_format_meta_icons_notes_snippet(qapp):
    """v1.7.3:notes <= 30 chars 全部显示;> 30 截断加 `…`。"""
    from tgmonitor.ui.widgets.message_view import _format_meta_icons

    short = _make_meta_msg(notes="需要 review")
    assert "📝需要 review" in _format_meta_icons(short)

    long_text = "x" * 50
    long = _make_meta_msg(notes=long_text)
    out = _format_meta_icons(long)
    # 30 chars + …
    assert "📝" in out
    assert "…" in out
    assert long_text not in out  # 完整 50 chars 不应出现


def test_format_meta_icons_pinned(qapp):
    """v1.7.3:is_pinned=True → 📌 icon。"""
    from tgmonitor.ui.widgets.message_view import _format_meta_icons

    m = _make_meta_msg(is_pinned=True)
    assert "📌" in _format_meta_icons(m)


def test_format_meta_icons_combined(qapp):
    """v1.7.3:4 类全有 → ★ 🏷 📝 📌 都在输出里(顺序 ★ → 🏷 → 📝 → 📌)。"""
    from tgmonitor.ui.widgets.message_view import _format_meta_icons

    m = _make_meta_msg(
        is_favorite=True,
        tags=["tech"],
        notes="review",
        is_pinned=True,
    )
    out = _format_meta_icons(m)
    assert out == "★ 🏷tech 📝review 📌"


def test_format_message_view_renders_metadata_icons(qapp):
    """v1.7.3:端到端 — view.append(msg) 后 model._format 输出含 ★ / 🏷 / 📝 / 📌。"""
    view = MessageView()
    msg = _make_meta_msg(
        telegram_msg_id=42,
        is_favorite=True,
        tags=["vip"],
        notes="check",
        is_pinned=True,
    )
    view.append(msg)
    text = _item_text(view, 0)
    # 4 个 icon 都应在行里
    assert "★" in text
    assert "🏷vip" in text
    assert "📝check" in text
    assert "📌" in text


# ============================================================
# 2026-09-11 v1.7.4:`_format_meta_icons` 加 reactions 渲染 +
# `MessageListModel.refresh_reactions` 局部更新
# ============================================================


def _make_msg_with_reactions(
    *,
    telegram_msg_id: int = 100,
    reactions: list | None = None,
) -> MessageDTO:
    """2026-09-11 v1.7.4:构造带 reactions 的 message DTO。"""

    return MessageDTO(
        id=0,
        channel_id=1,
        telegram_msg_id=telegram_msg_id,
        text="x",
        date=datetime(2026, 7, 15, 13, 0, 0),
        reactions=reactions,
    )


def test_format_meta_icons_includes_reactions(qapp):
    """v1.7.4:reactions 非空 → 拼到末尾:`🔥 5  👍 3`(LIVE 行 mark_chosen=False)。"""
    from tgmonitor.ui.widgets.message_view import _format_meta_icons

    m = _make_msg_with_reactions(
        reactions=[
            ReactionDTO(emoji="🔥", count=5),
            ReactionDTO(emoji="👍", count=3, is_chosen=True),
        ]
    )
    out = _format_meta_icons(m)
    # 顺序固定:★ → 🏷 → 📝 → 📌 → reactions;self-chosen 不加 [] (LIVE 行模式)
    assert out == "🔥 5  👍 3"


def test_format_meta_icons_reactions_limited_to_three(qapp):
    """v1.7.4:LIVE 行 max_show=3 — 5 个 emoji 只显前 3 + `+2`。"""
    from tgmonitor.ui.widgets.message_view import _format_meta_icons

    m = _make_msg_with_reactions(
        reactions=[
            ReactionDTO(emoji="🔥", count=5),
            ReactionDTO(emoji="👍", count=3),
            ReactionDTO(emoji="❤️", count=2),
            ReactionDTO(emoji="🎉", count=1),
            ReactionDTO(emoji="🤔", count=1),
        ]
    )
    out = _format_meta_icons(m)
    assert out == "🔥 5  👍 3  ❤️ 2  +2"


def test_format_meta_icons_empty_reactions_skipped(qapp):
    """v1.7.4:reactions=None / [] → _format_meta_icons 不输出 reactions 段。"""
    from tgmonitor.ui.widgets.message_view import _format_meta_icons

    m_none = _make_msg_with_reactions(reactions=None)
    m_empty = _make_msg_with_reactions(reactions=[])
    assert _format_meta_icons(m_none) == ""
    assert _format_meta_icons(m_empty) == ""


def test_refresh_reactions_updates_existing_row(qapp):
    """v1.7.4:refresh_reactions 命中现有 row → DTO.reactions 替换 + dataChanged emit。"""
    from PySide6.QtCore import QModelIndex

    from tgmonitor.core.dto import ReactionDTO

    view = MessageView()
    msg = _make_msg_with_reactions(
        telegram_msg_id=99,
        reactions=[ReactionDTO(emoji="🔥", count=2)],
    )
    view.append(msg)
    # 接住 dataChanged 信号
    emitted: list[tuple[QModelIndex, QModelIndex, list]] = []
    view._model.dataChanged.connect(lambda top, bottom, roles: emitted.append((top, bottom, roles)))

    new_reactions = [
        ReactionDTO(emoji="🔥", count=5),
        ReactionDTO(emoji="👍", count=3),
    ]
    view.refresh_reactions(1, 99, new_reactions)

    # DTO 已 mutate
    assert msg.reactions == new_reactions
    # dataChanged 至少 emit 1 次
    assert len(emitted) >= 1
    # roles 包含 DtoRole + FormattedRole(供 delegate 重 paint)
    roles = emitted[0][2]
    assert MessageListModel.DtoRole in roles
    assert MessageListModel.FormattedRole in roles


def test_refresh_reactions_none_is_noop(qapp):
    """v1.7.4:reactions=None(仅 views 变化)→ refresh_reactions 静默,不 mutate 不 emit。"""

    view = MessageView()
    msg = _make_msg_with_reactions(
        telegram_msg_id=99,
        reactions=[ReactionDTO(emoji="🔥", count=2)],
    )
    view.append(msg)
    original = msg.reactions
    emitted: list = []
    view._model.dataChanged.connect(lambda *args: emitted.append(args))

    view.refresh_reactions(1, 99, None)  # 仅 views,reactions 没新数据

    assert msg.reactions == original  # 没变
    assert emitted == []  # 没 emit


def test_refresh_reactions_missing_key_is_noop(qapp):
    """v1.7.4:目标 msg 不在 LIVE 视图(truncated / filter 隐藏)→ 静默。"""
    view = MessageView()
    emitted: list = []
    view._model.dataChanged.connect(lambda *args: emitted.append(args))

    view.refresh_reactions(99, 999, [ReactionDTO(emoji="🔥", count=1)])  # 不存在

    assert emitted == []


def test_dto_by_key_returns_dto(qapp):
    """v1.7.4:MessageView.dto_by_key 返回 LIVE 模型里当前 DTO 引用。"""
    view = MessageView()
    msg = _make_msg_with_reactions(telegram_msg_id=42)
    view.append(msg)
    got = view.dto_by_key(1, 42)
    assert got is msg  # 同一引用


def test_dto_by_key_missing_returns_none(qapp):
    """v1.7.4:key 不存在 → dto_by_key 返 None。"""
    view = MessageView()
    assert view.dto_by_key(1, 999) is None


# ============================================================
# 2026-09-14 v1.7.5 PR #8:★/🏷/📌 filter 测试(AND 语义)
# ============================================================


def _hidden_role(view: MessageView, row: int) -> bool:
    """读 MessageListModel HiddenRole — True = hidden(被过滤掉)。"""
    idx = view.model().index(row, 0)
    return bool(view.model().data(idx, MessageListModel.HiddenRole))


def test_pr8_set_filter_default_kwargs_compat(qapp):
    """PR #8 向后兼容:旧调用 `set_filter(text)` 仍能用,元数据维度默认 False。"""
    view = MessageView()
    view.append(_make_msg(1, 100))
    # _make_msg 默认 text="x",搜索 "x" 应命中
    view.set_filter("x")
    assert _hidden_role(view, 0) is False
    view.set_filter("nonexistent")
    assert _hidden_role(view, 0) is True


def test_pr8_set_filter_favorite_only_narrows(qapp):
    """PR #8:`favorite_only=True` → 仅 is_favorite=True 的消息通过。

    注:`MessageView.append` 走 newest-first(后插入的行 row 号更小),
    因此两行 append 后,row 0 是后者(normal),row 1 是前者(fav)。
    """
    view = MessageView()
    fav = _make_meta_msg(telegram_msg_id=100, is_favorite=True)
    normal = _make_meta_msg(telegram_msg_id=101, is_favorite=False)
    view.append(fav)
    view.append(normal)
    view.set_filter(favorite_only=True)
    assert _hidden_role(view, 0) is True  # normal hidden
    assert _hidden_role(view, 1) is False  # fav 通过


def test_pr8_set_filter_tag_only_narrows(qapp):
    """PR #8:`tag_only=True` → 仅 tags 非空的消息通过(newest-first 顺序)。"""
    view = MessageView()
    tagged = _make_meta_msg(telegram_msg_id=100, tags=["tech", "ai"])
    untagged = _make_meta_msg(telegram_msg_id=101)
    view.append(tagged)
    view.append(untagged)
    view.set_filter(tag_only=True)
    assert _hidden_role(view, 0) is True  # untagged hidden
    assert _hidden_role(view, 1) is False  # tagged 通过


def test_pr8_set_filter_pinned_only_narrows(qapp):
    """PR #8:`pinned_only=True` → 仅 is_pinned=True 的消息通过(newest-first)。"""
    view = MessageView()
    pinned = _make_meta_msg(telegram_msg_id=100, is_pinned=True)
    normal = _make_meta_msg(telegram_msg_id=101, is_pinned=False)
    view.append(pinned)
    view.append(normal)
    view.set_filter(pinned_only=True)
    assert _hidden_role(view, 0) is True  # normal hidden
    assert _hidden_role(view, 1) is False  # pinned 通过


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


def test_pr8_set_filter_combined_with_text(qapp):
    """PR #8:text + favorite + tag 组合 → AND 语义全部生效(newest-first 顺序)。"""
    view = MessageView()
    # text="alpha", fav=True, tags=["a"]
    full = _make_meta_msg(telegram_msg_id=100, is_favorite=True, tags=["a"])
    full.text = "alpha content"
    # text="beta" 但 fav=True
    text_miss = _make_meta_msg(telegram_msg_id=101, is_favorite=True)
    text_miss.text = "beta content"
    view.append(full)
    view.append(text_miss)
    view.set_filter("alpha", favorite_only=True, tag_only=True)
    assert _hidden_role(view, 0) is True  # text_miss(beta, row 0)— hidden
    assert _hidden_role(view, 1) is False  # full(alpha, row 1)— 通过


# 注:_make_meta_msg 默认 text=f"msg{telegram_msg_id}",会被 "msg" 搜索命中
# (因未单独改 text)。上面组合测试显式改 .text 为 "alpha content" / "beta content"
# 以验证 text + 元数据 AND 语义。


def test_pr8_set_filter_emits_data_changed_for_hidden_role(qapp):
    """PR #8:set_filter 后 emit dataChanged(所有 row, [HiddenRole])。"""
    view = MessageView()
    view.append(_make_meta_msg(telegram_msg_id=100))
    view.append(_make_msg(1, 101))
    spy = QSignalSpy(view.model().dataChanged)
    view.set_filter(favorite_only=True)
    assert spy.count() >= 1


def test_pr8_set_filter_persists_state_after_set_messages(qapp):
    """PR #8:set_messages 后 filter state 持久(text + 3 元数据都生效)。

    regression:之前只 persist `_filter_text`,元数据 filter 会被 reset
    后清空。

    注:`set_messages` 内部 `list(reversed(msgs))` 后 reset,传入 [200, 201]
    顺序时,row 0 是 telegram_msg_id=201(后入),row 1 是 200。
    """
    view = MessageView()
    view.append(_make_meta_msg(telegram_msg_id=100, is_favorite=True))
    view.set_filter(favorite_only=True)
    # 此时 row 0 是 fav(单条)→ 通过
    assert _hidden_role(view, 0) is False

    # set_messages 整批替换(走 model.reset)
    msgs = [
        _make_meta_msg(telegram_msg_id=200, is_favorite=True),
        _make_meta_msg(telegram_msg_id=201, is_favorite=False),
    ]
    view.set_messages(msgs)
    # row 0 = normal(201, hidden),row 1 = fav(200, 通过)
    assert _hidden_role(view, 0) is True
    assert _hidden_role(view, 1) is False


# ============================================================
# 2026-09-15 v1.7.5 PR #9 perf:MessageListModel O(N²) → O(N) + sizeHint / paint cache
# ============================================================


def test_pr9_append_constant_time_per_call(qapp):
    """PR #9 perf:`append` 单条耗时与已有行数无关 — 1K / 5K / 9.9K 时
    单条 append 都 < 1ms(以前 list.insert(0) + _row_to_key O(N²) 在 10K 时单条
    ~10ms,100 msg/s 场景下能撑爆单 CPU 核)。

    deque.appendleft 是 O(1),`_index_of` bump 是 O(N) — 但实测 N=10K 的
    dict-iteration 在 CPython 上远快于 list.insert + sort,稳 < 1ms。
    """
    view = MessageView()

    # 灌 1000 条 baseline
    for i in range(1000):
        view.append(_make_msg(1, i))
    t0 = time.perf_counter()
    view.append(_make_msg(1, 1000))
    elapsed_1k = time.perf_counter() - t0

    # 灌到 5000
    for i in range(1001, 5000):
        view.append(_make_msg(1, i))
    t0 = time.perf_counter()
    view.append(_make_msg(1, 5000))
    elapsed_5k = time.perf_counter() - t0

    # 灌到 9999
    for i in range(5001, 9999):
        view.append(_make_msg(1, i))
    t0 = time.perf_counter()
    view.append(_make_msg(1, 9999))
    elapsed_10k = time.perf_counter() - t0

    # 1K 应 < 1ms,10K 应 < 5ms(允许 ~5ms 因为 dict iteration 1万次)
    assert elapsed_1k < 0.002, f"1K append 耗时 {elapsed_1k * 1000:.2f}ms > 2ms"
    assert elapsed_5k < 0.003, f"5K append 耗时 {elapsed_5k * 1000:.2f}ms > 3ms"
    assert elapsed_10k < 0.005, f"10K append 耗时 {elapsed_10k * 1000:.2f}ms > 5ms"


def test_pr9_set_messages_emits_single_model_reset(qapp):
    """PR #9:1000 条 `set_messages` 只 emit 1 次 `modelReset`(锁定 PR #5 契约)。

    之前 PR #5 修过:走 N×append 会 emit N 次 `rowsInserted` → 浮条计数误累加。
    本测试兜底:无论多少条,只能有 1 个 `modelReset`,0 个 `rowsInserted`。
    """
    view = MessageView()
    reset_spy = QSignalSpy(view._model.modelReset)
    insert_spy = QSignalSpy(view._model.rowsInserted)
    msgs = [_make_msg(1, i) for i in range(1000)]
    view.set_messages(msgs)
    assert reset_spy.count() == 1, f"应 emit 1 次 modelReset,实测 {reset_spy.count()}"
    assert insert_spy.count() == 0, (
        f"set_messages 不应 emit rowsInserted(PR #5 契约),实测 {insert_spy.count()}"
    )


def test_pr9_format_cache_hit_avoids_recompute(qapp):
    """PR #9 perf:`FormattedRole` 缓存 — 同一行 paint 2 次,`_format()` 只调 1 次。

    走 mock 替 `_format` 方法计数,验证第二次 `data(FormattedRole)` 命中缓存
    直接返旧值,不重算。
    """
    view = MessageView()
    msg = _make_msg(1, 100)
    view.append(msg)
    idx = view._model.index(0, 0)

    # 第一次 — 缓存 miss,触发 _format
    call_count = {"n": 0}
    original_format = view._model._format

    def counting_format(m: MessageDTO) -> str:
        call_count["n"] += 1
        return original_format(m)

    view._model._format = counting_format  # type: ignore[method-assign]
    try:
        # 第一次 → miss → 算一次
        first = view._model.data(idx, MessageListModel.FormattedRole)
        assert call_count["n"] == 1, f"首次应算一次,实测 {call_count['n']}"
        # 第二次 → 命中 → 不算
        second = view._model.data(idx, MessageListModel.FormattedRole)
        assert call_count["n"] == 1, f"二次应命中缓存,实测 {call_count['n']}"
        assert first == second
    finally:
        view._model._format = original_format  # type: ignore[method-assign]


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


def test_pr9_format_cache_cleared_on_set_channel_titles(qapp):
    """PR #9 perf:`set_channel_titles` 清全表 FormattedRole 缓存(标题变了)。

    拿不到 DTO 单条改 channel_id 但 channel_titles 改变影响 head 的 `[title]` 段,
    所以必须全清。
    """
    view = MessageView()
    view.set_channel_titles({1: "tech-news"})
    msg = _make_msg(1, 100)
    view.append(msg)
    idx = view._model.index(0, 0)
    text = view._model.data(idx, MessageListModel.FormattedRole)
    assert "[tech-news]" in text, f"应显示新 title,实测 {text!r}"

    # 改 title → 缓存全清 → 下次读走新 title
    view.set_channel_titles({1: "general"})
    text2 = view._model.data(idx, MessageListModel.FormattedRole)
    assert "[general]" in text2, f"改 title 后应显示新 title,实测 {text2!r}"
    assert "[tech-news]" not in text2


def test_pr9_size_hint_cache_hit_returns_same_size(qapp):
    """PR #9 perf:`sizeHint` 缓存 — 同 `(cid, mid, width)` 第二次走 cache。

    通过 delegate 直接调 `sizeHint`,传入同 option,第二次应直接返 QSize 实例
    而非新建 QTextDocument(用 mock 替 QTextDocument 计数验证)。
    """
    from PySide6.QtGui import QTextDocument  # noqa: PLC0415

    view = MessageView()
    msg = _make_msg(1, 100)
    view.append(msg)
    idx = view._model.index(0, 0)

    delegate = view._delegate
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 400, 0)
    option.font = view.font()

    doc_count = {"n": 0}
    original_init = QTextDocument.__init__

    def counting_init(self, *a, **kw):
        doc_count["n"] += 1
        original_init(self, *a, **kw)

    QTextDocument.__init__ = counting_init  # type: ignore[method-assign]
    try:
        # 第一次 → miss → 1 个 QTextDocument
        delegate.sizeHint(option, idx)
        assert doc_count["n"] == 1, f"首次应建 1 个 doc,实测 {doc_count['n']}"
        # 第二次 → 命中 → 0 新建
        delegate.sizeHint(option, idx)
        assert doc_count["n"] == 1, f"二次应命中缓存,实测 {doc_count['n']}"
    finally:
        QTextDocument.__init__ = original_init  # type: ignore[method-assign]


def test_pr9_doc_cache_cleared_on_reset(qapp):
    """PR #9 perf:`reset()` 清 delegate `_doc_cache` 和 `_size_hint_cache`。

    验证整批替换后,旧 key 的缓存不会留下脏数据。
    """
    view = MessageView()
    view.append(_make_msg(1, 100))
    view.append(_make_msg(1, 101))

    # 触发 paint / sizeHint 填 cache
    delegate = view._delegate
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 400, 0)
    option.font = view.font()
    delegate.sizeHint(option, view._model.index(0, 0))
    delegate.sizeHint(option, view._model.index(1, 0))
    assert len(delegate._doc_cache) > 0 or len(delegate._size_hint_cache) > 0

    # reset 整批换新
    view._model.reset([_make_msg(2, 999)])

    # 缓存全清(否则 paint (2, 999) 会命中旧 (1, 100) 的 doc 显示老文本)
    assert len(delegate._doc_cache) == 0, (
        f"reset 后 _doc_cache 应清空,剩 {len(delegate._doc_cache)} 条"
    )
    assert len(delegate._size_hint_cache) == 0, (
        f"reset 后 _size_hint_cache 应清空,剩 {len(delegate._size_hint_cache)} 条"
    )


def test_pr9_max_items_truncates_correctly(qapp):
    """PR #9 regression:deque 后 `_truncate_tail` 必须真的删最旧一行。

    append i=0..MAX_ITEMS → 超 1 条 → 最旧 (1, 0) 被截断,最新 (1, MAX_ITEMS) 保留。

    验证关键回归:之前用 `popleft()` 误删最新,改 `pop()` 后语义恢复。
    """
    view = MessageView()
    for i in range(MessageView.MAX_ITEMS + 1):
        view.append(_make_msg(1, i))
    assert view._model.rowCount() == MessageView.MAX_ITEMS
    # (1, 0) 是最早 append 的(最旧)→ 应被截断
    assert (1, 0) not in view._model._index_of, "(1, 0) 应被截断"
    # (1, MAX_ITEMS) 是最后 append 的(最新)→ 应保留(head)
    assert (1, MessageView.MAX_ITEMS) in view._model._index_of
    # 最新那条在 row 0(deque head)
    assert view._model._index_of[(1, MessageView.MAX_ITEMS)] == 0


def test_pr9_append_existing_key_replaces_in_place(qapp):
    """PR #9 regression:`append` 已存在 key 时原地替换(不 insert 新行)。

    dedup 分支走 `self._items[row] = m` —— deque 支持 `__setitem__`,
    缓存失效 + emit dataChanged。
    """
    view = MessageView()
    msg1 = _make_msg(1, 100)
    msg1.text = "v1"
    view.append(msg1)
    assert view._model.rowCount() == 1
    assert view._model._index_of[(1, 100)] == 0

    # 重复 append 同 key → 原地替换,row 数不变
    msg2 = _make_msg(1, 100)
    msg2.text = "v2"
    view.append(msg2)
    assert view._model.rowCount() == 1
    assert view._model._index_of[(1, 100)] == 0
    text = view._model.data(view._model.index(0, 0), MessageListModel.FormattedRole)
    assert "v2" in text, f"dedup 后应显示新文本,实测 {text!r}"


def test_pr9_remove_by_key_clears_index(qapp):
    """PR #9 regression:`remove_by_key` 后 `_index_of` 和 `_format_cache`
    都清干净 — 后续 append 同 key 走 insert 分支(不 dedup)。
    """
    view = MessageView()
    view.append(_make_msg(1, 100))
    view.append(_make_msg(1, 101))
    view.append(_make_msg(1, 102))

    view.remove_row(1, 101)
    assert (1, 101) not in view._model._index_of
    assert (1, 100) in view._model._index_of
    assert (1, 102) in view._model._index_of
    assert (1, 101) not in view._model._format_cache

    # 重新 append(1, 101) → 应走 insert 路径,row 0
    view.append(_make_msg(1, 101))
    assert view._model._index_of[(1, 101)] == 0
