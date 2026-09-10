"""2026-09-09 v1.7.2:MessageDTO 元数据字段(is_favorite / tags / notes)默认值测试。

覆盖:
- 默认构造 → is_favorite=False, tags=[], notes=""
- 显式赋值后字段正确反映
- dataclasses.replace 拷贝 — 修改元数据不影响原对象
- 与其他字段(reactions / is_pinned)互不干扰
- 序列化往返(不依赖具体后端,只校验 dict / 对象互转基本结构)
"""

from __future__ import annotations

from dataclasses import asdict, replace

from tgmonitor.core.dto import MessageDTO


def _base_dto(**overrides: object) -> MessageDTO:
    """构造一条最小可用 MessageDTO,字段可覆盖。"""
    defaults: dict = {
        "id": 1,
        "channel_id": 1,
        "telegram_msg_id": 10,
        "text": "hi",
    }
    defaults.update(overrides)
    return MessageDTO(**defaults)  # type: ignore[arg-type]


# ============== 默认值 ==============


def test_default_is_favorite_is_false() -> None:
    """不传 is_favorite → False(默认收藏=否)。"""
    msg = _base_dto()
    assert msg.is_favorite is False


def test_default_tags_is_empty_list() -> None:
    """不传 tags → [](默认无标签),且不是 None。"""
    msg = _base_dto()
    assert msg.tags == []
    assert msg.tags is not None


def test_default_notes_is_empty_string() -> None:
    """不传 notes → ""(默认无备注),且不是 None。"""
    msg = _base_dto()
    assert msg.notes == ""
    assert msg.notes is not None


def test_default_factory_each_instance_independent_tags() -> None:
    """default_factory=list 关键回归:两个实例 tags 不共享同一 list。"""
    a = _base_dto()
    b = _base_dto()
    a.tags.append("tech")
    assert b.tags == []  # 没被 a 污染


# ============== 显式赋值 ==============


def test_explicit_is_favorite_true() -> None:
    """is_favorite=True 显式赋值生效。"""
    msg = _base_dto(is_favorite=True)
    assert msg.is_favorite is True


def test_explicit_tags_list() -> None:
    """tags=['tech', 'news'] 显式赋值生效。"""
    msg = _base_dto(tags=["tech", "news"])
    assert msg.tags == ["tech", "news"]


def test_explicit_notes_text() -> None:
    """notes='后续 review' 显式赋值生效。"""
    msg = _base_dto(notes="后续 review")
    assert msg.notes == "后续 review"


# ============== 拷贝 / replace ==============


def test_replace_is_favorite_does_not_mutate_original() -> None:
    """dataclasses.replace 改 is_favorite → 原 msg 不变。"""
    original = _base_dto()
    starred = replace(original, is_favorite=True)
    assert original.is_favorite is False
    assert starred.is_favorite is True
    assert starred is not original


def test_replace_tags_does_not_mutate_original() -> None:
    """dataclasses.replace 改 tags → 原 msg.tags 不变(也不共享同一 list)。"""
    original = _base_dto()
    tagged = replace(original, tags=["tech"])
    assert original.tags == []
    assert tagged.tags == ["tech"]
    # 关键:新 list,不是同一引用
    assert tagged.tags is not original.tags


def test_replace_notes_does_not_mutate_original() -> None:
    original = _base_dto()
    noted = replace(original, notes="abc")
    assert original.notes == ""
    assert noted.notes == "abc"


# ============== 与其他字段互不干扰 ==============


def test_metadata_independent_of_reactions() -> None:
    """is_favorite / tags / notes 与 reactions 互不干扰(可同时设置)。"""
    from tgmonitor.core.dto import ReactionDTO

    msg = _base_dto(
        is_favorite=True,
        tags=["vip"],
        notes="priority",
        reactions=[ReactionDTO(type="emoji", emoji="🔥", count=2, is_chosen=True)],
    )
    assert msg.is_favorite is True
    assert msg.tags == ["vip"]
    assert msg.notes == "priority"
    assert len(msg.reactions) == 1
    assert msg.reactions[0].emoji == "🔥"  # type: ignore[union-attr]


def test_metadata_independent_of_is_pinned() -> None:
    """元数据与 is_pinned 字段独立(收藏 ≠ 钉选,语义不同)。"""
    msg = _base_dto(is_pinned=True, is_favorite=True, tags=["pinned-and-starred"])
    assert msg.is_pinned is True
    assert msg.is_favorite is True
    assert msg.tags == ["pinned-and-starred"]


# ============== 序列化兜底 ==============


def test_asdict_contains_metadata_keys() -> None:
    """dataclasses.asdict 输出含 is_favorite / tags / notes — 后端序列化兜底。"""
    msg = _base_dto(is_favorite=True, tags=["t"], notes="n")
    d = asdict(msg)
    assert d["is_favorite"] is True
    assert d["tags"] == ["t"]
    assert d["notes"] == "n"
