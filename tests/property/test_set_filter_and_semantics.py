"""2026-09-17 PR 6:`set_filter` HiddenRole AND-semantics property test。

PR 6 plan 测点 3:随机 `set_filter(text, *, favorite, tag, pinned)` 组合 +
随机 MessageDTO → HiddenRole 必须 = NOT(_matches(m))。

`_matches` 规则(set_filter 文档化的契约):
- text 空 → 不参与 AND(只过滤元数据);否则 `text in m.text.lower()`
  OR `text in m.author.lower()` OR `text in channel_titles[cid].lower()`
  OR `text == str(m.telegram_msg_id)`
- favorite_only=True → 仅 `m.is_favorite` 通过
- tag_only=True → 仅 `m.tags` 非空通过
- pinned_only=True → 仅 `m.is_pinned` 通过
- 4 个条件 AND 组合 — 任一不满足就 `_matches=False` → HiddenRole=True

测法:
- 构造随机 MessageDTOs(text/author/tags/is_favorite/is_pinned 随机)
- 构造随机 filter state(4 维随机开/关)
- 应用 filter → 读 HiddenRole → 与手工 ORACLE 对比
- 边界:全空 filter → 0 行 hidden;单独打开 favorite + tag 交集 → AND 生效

注:`MessageView` / `MessageListModel` 是 Qt model,property test 不需要
paint 路径 — 直接调 `set_filter` 后读 `data(idx, HiddenRole)` 即可。
需要 `qapp` fixture 拿 `QCoreApplication`(QObject 信号需要 event loop)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st

from tgmonitor.core.dto import MessageDTO
from tgmonitor.ui.widgets.message_view import MessageListModel, MessageView

# ========== helpers ==========


def _make_msg(
    *,
    cid: int = 1,
    mid: int = 1,
    text: str = "x",
    author: str | None = None,
    tags: list[str] | None = None,
    is_favorite: bool = False,
    is_pinned: bool = False,
) -> MessageDTO:
    return MessageDTO(
        id=0,
        channel_id=cid,
        telegram_msg_id=mid,
        date=datetime(2026, 9, 17, tzinfo=UTC),
        text=text,
        author=author,
        tags=list(tags) if tags else [],
        is_favorite=is_favorite,
        is_pinned=is_pinned,
    )


def _is_hidden(model: MessageListModel, row: int) -> bool:
    """读 row 的 HiddenRole(True = 被过滤掉)。"""
    return bool(model.data(model.index(row, 0), MessageListModel.HiddenRole))


def _matches_oracle(
    msg: MessageDTO,
    *,
    filter_text: str,
    favorite_only: bool,
    tag_only: bool,
    pinned_only: bool,
    channel_titles: dict[int, str],
) -> bool:
    """手算 ORACLE — 模拟 `_matches` 契约(text OR author OR title OR mid exact)。

    跟源码同语义:每个条件独立判断,AND 组合。
    """
    if favorite_only and not msg.is_favorite:
        return False
    if tag_only and not msg.tags:
        return False
    if pinned_only and not msg.is_pinned:
        return False
    text = filter_text.strip().lower()
    if not text:
        return True
    if msg.text and text in msg.text.lower():
        return True
    if msg.author and text in msg.author.lower():
        return True
    title = channel_titles.get(msg.channel_id, "")
    if title and text in title.lower():
        return True
    return str(msg.telegram_msg_id) == text or text.lstrip("#") == str(msg.telegram_msg_id)


# ========== property:fuzz 4 维 filter × 随机 DTO ==========


# text strategy: 限长度 + 避免极端 control 字符(只 ascii 字母数字 + space)
_text_st = st.text(
    alphabet=st.sampled_from(
        list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-")
    ),
    min_size=0,
    max_size=20,
)


@given(
    msgs_data=st.lists(
        st.fixed_dictionaries(
            {
                "text": st.text(
                    alphabet=st.sampled_from(
                        list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-./")
                    ),
                    min_size=1,
                    max_size=30,
                ),
                "author": st.one_of(st.none(), _text_st),
                "tags": st.one_of(st.none(), st.lists(_text_st, min_size=1, max_size=3)),
                "is_favorite": st.booleans(),
                "is_pinned": st.booleans(),
            }
        ),
        min_size=2,
        max_size=15,
    ),
    filter_text=_text_st,
    favorite_only=st.booleans(),
    tag_only=st.booleans(),
    pinned_only=st.booleans(),
    mid_offset=st.integers(min_value=1, max_value=1000),
)
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_set_filter_hidden_role_matches_oracle(
    qapp,
    msgs_data: list[dict],
    filter_text: str,
    favorite_only: bool,
    tag_only: bool,
    pinned_only: bool,
    mid_offset: int,
) -> None:
    """随机 filter state × 随机 DTO 集合 → HiddenRole 必须 = `not oracle_matches`。

    oracle 是基于源码 docstring 文档化契约的手算函数 —
    若 production 行为偏离,property test 应捕获。
    """
    # 跳过 user-input 不能构造的边界 case(text 全空格当作空)
    assume(filter_text.strip() != "")

    # 构造 model:用 message_dtos 数据构造,然后强制唯一 mid(避免 dedup)
    model = MessageListModel()
    cid = 42
    used_mids: set[int] = set()
    for i, d in enumerate(msgs_data):
        mid = mid_offset + i
        while mid in used_mids:
            mid += 1
        used_mids.add(mid)
        m = _make_msg(
            cid=cid,
            mid=mid,
            text=d["text"],
            author=d["author"],
            tags=d["tags"],
            is_favorite=d["is_favorite"],
            is_pinned=d["is_pinned"],
        )
        model.append(m)

    # 关键:不要注入 title 文本,避免 oracle 跟 impl 在 title 分支有 unknown 偏歧
    model.set_channel_titles({})

    # 应用 filter
    model.set_filter(
        filter_text,
        favorite_only=favorite_only,
        tag_only=tag_only,
        pinned_only=pinned_only,
    )

    # 断言:HiddenRole == not oracle_matches
    channel_titles: dict[int, str] = {}
    for row in range(model.rowCount()):
        m = model.data(model.index(row, 0), MessageListModel.DtoRole)
        assert m is not None
        expected_hidden = not _matches_oracle(
            m,
            filter_text=filter_text,
            favorite_only=favorite_only,
            tag_only=tag_only,
            pinned_only=pinned_only,
            channel_titles=channel_titles,
        )
        actual_hidden = _is_hidden(model, row)
        assert actual_hidden == expected_hidden, (
            f"row {row} (text={m.text!r}, author={m.author!r}, tags={m.tags!r}, "
            f"fav={m.is_favorite}, pin={m.is_pinned}): "
            f"filter={filter_text!r} fav={favorite_only} tag={tag_only} pin={pinned_only}, "
            f"expected_hidden={expected_hidden}, actual_hidden={actual_hidden}"
        )


# ========== invariant:空 filter 不 hide 任何东西 ==========


@example(msgs=[("a", None, None, False, False), ("b", None, None, False, False)])
@example(msgs=[("hello", None, None, True, True)])
@given(
    msgs=st.lists(
        st.tuples(
            _text_st,  # text
            st.one_of(st.none(), _text_st),  # author
            st.one_of(st.none(), st.lists(_text_st, max_size=2)),  # tags
            st.booleans(),  # is_favorite
            st.booleans(),  # is_pinned
        ),
        min_size=1,
        max_size=20,
    ),
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_set_filter_empty_filter_hides_nothing(qapp, msgs: list[tuple]) -> None:
    """`set_filter("")` + 全 False flag → 没有任何行 HiddenRole=True。

    这是 search bar "clear search" 状态的契约 — 必须显示所有。
    """
    model = MessageListModel()
    for i, (text, author, tags, fav, pin) in enumerate(msgs):
        model.append(
            _make_msg(
                cid=1,
                mid=i + 1,
                text=text or "x",
                author=author,
                tags=tags,
                is_favorite=fav,
                is_pinned=pin,
            )
        )
    model.set_channel_titles({})

    # 空 filter
    model.set_filter("", favorite_only=False, tag_only=False, pinned_only=False)

    for row in range(model.rowCount()):
        assert _is_hidden(model, row) is False, f"空 filter 下 row {row} 不应被隐藏"


# ========== invariant:打开任一 flag 不会扩大可见集 ==========


@given(
    msgs_data=st.lists(
        st.fixed_dictionaries(
            {
                "text": st.sampled_from(["alpha", "beta", "gamma", "delta"]),
                "author": st.one_of(st.none(), st.sampled_from(["alice", "bob"])),
                "tags": st.one_of(
                    st.none(),
                    st.lists(st.sampled_from(["t", "ai"]), min_size=1, max_size=2),
                ),
                "is_favorite": st.booleans(),
                "is_pinned": st.booleans(),
            }
        ),
        min_size=5,
        max_size=15,
    ),
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_set_filter_combining_flags_never_widens(qapp, msgs_data: list[dict]) -> None:
    """组合多 flag → 可见 row 数 ≤ 任一单独 flag 的可见 row 数。

    AND 语义的关键 invariant:打开更多条件只会收窄,不会扩展。
    """
    model = MessageListModel()
    for i, d in enumerate(msgs_data):
        model.append(
            _make_msg(
                cid=1,
                mid=i + 1,
                text=d["text"],
                author=d["author"],
                tags=d["tags"],
                is_favorite=d["is_favorite"],
                is_pinned=d["is_pinned"],
            )
        )
    model.set_channel_titles({})

    # baseline:空 filter → 全可见
    model.set_filter("")
    baseline_visible = sum(1 for r in range(model.rowCount()) if not _is_hidden(model, r))

    # 单独开 favorite_only
    model.set_filter("", favorite_only=True)
    fav_visible = sum(1 for r in range(model.rowCount()) if not _is_hidden(model, r))

    # 组合:favorite AND pinned
    model.set_filter("", favorite_only=True, pinned_only=True)
    fav_pin_visible = sum(1 for r in range(model.rowCount()) if not _is_hidden(model, r))

    # AND 收窄:组合可见 ≤ 单条件可见 ≤ baseline
    assert fav_pin_visible <= fav_visible, (
        f"组合 AND 应 ≤ 单 fav 可见数,fav_pin={fav_pin_visible} > fav={fav_visible}"
    )
    assert fav_visible <= baseline_visible, (
        f"单 fav 应 ≤ baseline,fav={fav_visible} > baseline={baseline_visible}"
    )


# ========== 边界:Text 在 author / mid 命中 ==========


def test_set_filter_text_matches_in_author_field(qapp) -> None:
    """text 在 author 字段命中 → row 可见。

    `search_messages` 之外的 client-side filter 也支持 author 模糊搜。

    newest-first 顺序:后插入在 row 0。所以 Bob 在 row 0,Alice 在 row 1。
    """
    view = MessageView()
    view.append(_make_msg(mid=1, text="hello", author="Alice"))  # row 1
    view.append(_make_msg(mid=2, text="hello", author="Bob"))  # row 0
    view.set_channel_titles({})

    view.set_filter("alice")
    assert _is_hidden(view._model, 0) is True  # Bob 被过滤
    assert _is_hidden(view._model, 1) is False  # Alice 通过


def test_set_filter_text_matches_exact_msg_id(qapp) -> None:
    """text == str(msg_id) → 命中;`#42` 也命中(chat 链接风格)。"""
    model = MessageListModel()
    model.append(_make_msg(mid=42, text="hello"))  # row 1
    model.append(_make_msg(mid=43, text="hello"))  # row 0
    model.set_channel_titles({})

    model.set_filter("42")
    assert _is_hidden(model, 0) is True  # row 0 = mid=43 被过滤
    assert _is_hidden(model, 1) is False  # row 1 = mid=42 通过

    model.set_filter("#42")
    assert _is_hidden(model, 0) is True  # `#42` 不命中 mid=43
    assert _is_hidden(model, 1) is False  # `#42` 命中 mid=42


def test_set_filter_text_is_case_insensitive(qapp) -> None:
    """text 模糊匹配 case-insensitive(底层 `m.text.lower()`)。"""
    model = MessageListModel()
    model.append(_make_msg(mid=1, text="Hello World"))
    model.set_channel_titles({})

    model.set_filter("WORLD")
    assert _is_hidden(model, 0) is False  # 大写搜索命中 Hello World

    model.set_filter("world")
    assert _is_hidden(model, 0) is False  # 小写也命中


# ========== 边界:channel_titles 影响 text match ==========


def test_set_filter_text_matches_channel_title(qapp) -> None:
    """text 在 channel_titles[cid] 命中 → row 可见(尽管 m.text 不含)。"""
    model = MessageListModel()
    model.append(_make_msg(cid=1, mid=1, text="rss feed body"))  # row 1
    model.append(_make_msg(cid=2, mid=2, text="rss feed body"))  # row 0
    model.set_channel_titles({1: "Tech News Daily", 2: "Sports Updates"})

    model.set_filter("tech")
    assert _is_hidden(model, 0) is True  # row 0 = cid=2 不命中
    assert _is_hidden(model, 1) is False  # row 1 = cid=1 命中 title


# ========== 边界:AND 组合正确性 ==========


def test_set_filter_and_composition_text_and_favorite(qapp) -> None:
    """text + favorite_only AND:既要 text 命中又要 fav=True。

    4 条 append,newest-first 排 row 0 = (mid=4)。
    唯一通过 AND 的是 (mid=1, alpha, fav=True) → row 3(最旧)。
    """
    model = MessageListModel()
    model.append(_make_msg(mid=1, text="alpha", is_favorite=True))  # row 3
    model.append(_make_msg(mid=2, text="alpha", is_favorite=False))  # row 2
    model.append(_make_msg(mid=3, text="beta", is_favorite=True))  # row 1
    model.append(_make_msg(mid=4, text="beta", is_favorite=False))  # row 0
    model.set_channel_titles({})

    model.set_filter("alpha", favorite_only=True)
    # row 3 = mid=1 同时满足 text=alpha + fav → visible
    assert _is_hidden(model, 3) is False, "row 3 (mid=1, alpha, fav=True) 应通过"
    # 其余全 hidden(text 不命中 OR fav=False)
    for row in (0, 1, 2):
        assert _is_hidden(model, row) is True, f"row {row} 不应通过 AND-combo (text=alpha + fav)"


def test_set_filter_empty_text_with_favorite_only_still_narrows(qapp) -> None:
    """`set_filter("")` + `favorite_only=True` → 不参与 text,但 fav 收窄。"""
    model = MessageListModel()
    model.append(_make_msg(mid=1, text="anything", is_favorite=True))  # row 1
    model.append(_make_msg(mid=2, text="anything", is_favorite=False))  # row 0
    model.set_channel_titles({})

    model.set_filter("", favorite_only=True)
    assert _is_hidden(model, 0) is True  # row 0 = mid=2 non-fav → hidden
    assert _is_hidden(model, 1) is False  # row 1 = mid=1 fav → visible


# ========== 锁定 bug:tags=[] with tag_only=True must hide ==========


def test_set_filter_tag_only_hides_untagged(qapp) -> None:
    """Regression lock:tag_only=True + msg.tags=[] → hidden。

    PR #8 实现的 AND 语义 — `_matches` 用 `m.tags`(list) truthiness,
    空 list 是 falsy,所以 untagged 被过滤掉。
    """
    model = MessageListModel()
    model.append(_make_msg(mid=1, tags=["x"]))  # row 1
    model.append(_make_msg(mid=2, tags=[]))  # row 0
    model.set_channel_titles({})

    model.set_filter(tag_only=True)
    assert _is_hidden(model, 0) is True  # row 0 = mid=2 untagged → hidden
    assert _is_hidden(model, 1) is False  # row 1 = mid=1 tagged → visible


# ========== API:backward compat — old text-only call still works ==========


def test_set_filter_legacy_text_only_still_filters(qapp) -> None:
    """旧 `set_filter(text)` 单 arg 调用 — 元数据 flag 默认 False。"""
    model = MessageListModel()
    model.append(_make_msg(mid=1, text="hello", is_favorite=True))  # row 1
    model.append(_make_msg(mid=2, text="hello", is_favorite=True))  # row 0
    model.set_channel_titles({})

    model.set_filter("hello")
    assert _is_hidden(model, 0) is False  # row 0 = mid=2 命中
    assert _is_hidden(model, 1) is False  # row 1 = mid=1 命中

    model.set_filter("nonexistent")
    assert _is_hidden(model, 0) is True
    assert _is_hidden(model, 1) is True
