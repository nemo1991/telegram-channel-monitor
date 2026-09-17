"""2026-09-17 PR 6:MessageDTO round-trip property test — JsonlFileStore backend。

PR 6 plan:`MessageDTO` round-trip × 4 backend。本文件测 **JsonlFileStore**
(JSON 行文件持久化)— 序列化丢字段 bug 高发区。

预期 bug(plan):
- datetime → JSON 没有原生 datetime,丢失 tzinfo 或变 naive
- `reactions=None` vs `[]` JSON 不区分(None 变 null,[] 变 [])
- 空 str vs None 字段(`notes=""` vs `notes=None`)
- `tags` list 顺序

**PR 6 实测发现 bug**(2026-09-17):
- `ChannelFile.load` 对含 `\x85`-(NEL,Next-Line char)的 str 字段
  (如 `notes` / `media_album_id`)**静默 skip 整行** — `json.loads` strict 模式抛
  `JSONDecodeError`,被 `except json.JSONDecodeError: continue` 吞掉。结果:含
  NEL 的消息落盘后 reload 整条消失。**待开 follow-up PR**(改 `ensure_ascii=False`
  + `default=str` + 自定义 encoder / 加 `strict=False`)。本文件用 `assume()`
  filter 排除带控制字符的 case,继续跑其它 random case;`_test_jsonl_nel_bug_locked`
  单独锁定 1 条破的 case 当 failing reminder。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings

from tests.fixtures._strategies import message_dtos
from tgmonitor.core.storage.jsonl_store import JsonlFileStore


# 控制字符 (Cc 类别):Python `json.loads` strict 模式会拒收(NEL \x85 /
# VT \x0B / FF \x0C 等)。PR 6 已确认 \x85 触发 ChannelFile.load 静默 skip 整行。
# Cc = C0 (0x00-0x1F) + DEL (0x7F) + C1 (0x80-0x9F)。
def _control_chars() -> set[str]:
    import unicodedata

    return {chr(c) for c in range(0x110000) if unicodedata.category(chr(c)) == "Cc"}


_CONTROL_CHARS = _control_chars()


def _has_control_char(s: str | None) -> bool:
    if s is None:
        return False
    return any(c in _CONTROL_CHARS for c in s)


def _msg_safe_for_jsonl(msg) -> bool:
    """所有 str 字段不能含控制字符 — 否则 JsonlFileStore reload 静默丢。"""
    if _has_control_char(msg.text):
        return False
    if _has_control_char(msg.author):
        return False
    if _has_control_char(msg.notes):
        return False
    if msg.media_album_id and _has_control_char(msg.media_album_id):
        return False
    if any(_has_control_char(t) for t in msg.tags):
        return False
    for m in msg.media:
        if _has_control_char(m.file_name):
            return False
        if _has_control_char(m.emoji):
            return False
        if _has_control_char(m.mime_type):
            return False
        if _has_control_char(m.download_error):
            return False
        if m.telegram_file_id and _has_control_char(m.telegram_file_id):
            return False
        if m.object_key and _has_control_char(m.object_key):
            return False
        if m.thumb_key and _has_control_char(m.thumb_key):
            return False
    if msg.reactions:
        for r in msg.reactions:
            if _has_control_char(r.emoji):
                return False
            if _has_control_char(r.type):
                return False
    return True


@pytest.fixture
def jsonl_dir(tmp_path: Path) -> Path:
    """Unique subdir per call — 避免 hypothesis 跨 example 用同一目录。"""
    d = tmp_path / f"store_{id(object())}"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.mark.asyncio
@given(msg=message_dtos())
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,
    ],
)
async def test_message_dto_jsonl_roundtrip(jsonl_dir: Path, msg) -> None:
    """任意 MessageDTO → JsonlFileStore 落盘 → reload → 字段相等。

    JsonlFileStore 是顺序 JSONL 文件 — round-trip 等价于 to_dict/from_dict 无损。
    """
    # PR 6 known bug:str 字段含控制字符会被 ChannelFile.load 静默 skip。
    # 用 assume() 跳过,不阻塞其它 case。`_test_jsonl_nel_bug_locked` 锁定
    # 1 条破的 case 作为 reminder。
    assume(_msg_safe_for_jsonl(msg))

    store = JsonlFileStore(jsonl_dir)
    await store.connect()

    await store.save_message(msg)
    await store.close()

    # 新建第二个 store,指同一目录,读回
    store2 = JsonlFileStore(jsonl_dir)
    await store2.connect()

    loaded = await store2.get_message(msg.channel_id, msg.telegram_msg_id)
    assert loaded is not None, (
        f"JsonlFileStore reload 后 get_message 返 None "
        f"(cid={msg.channel_id}, mid={msg.telegram_msg_id})"
    )
    # 标量字段
    assert loaded.channel_id == msg.channel_id
    assert loaded.telegram_msg_id == msg.telegram_msg_id
    assert loaded.text == msg.text
    assert loaded.author == msg.author
    assert loaded.tags == msg.tags
    assert loaded.notes == msg.notes
    assert loaded.is_pinned == msg.is_pinned
    assert loaded.edited == msg.edited
    assert loaded.is_favorite == msg.is_favorite
    # date aware datetime(若 JSON 序列化为 naive 会失败)
    assert loaded.date == msg.date
    assert loaded.date.tzinfo is not None, "date 丢了 tzinfo"


@pytest.mark.asyncio
@given(msg=message_dtos())
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,
    ],
)
async def test_message_dto_jsonl_reactions_roundtrip(jsonl_dir: Path, msg) -> None:
    """JsonlFileStore reactions round-trip — None 保留 vs [] 不混。"""
    assume(_msg_safe_for_jsonl(msg))

    store = JsonlFileStore(jsonl_dir)
    await store.connect()
    await store.save_message(msg)
    await store.close()

    store2 = JsonlFileStore(jsonl_dir)
    await store2.connect()
    loaded = await store2.get_message(msg.channel_id, msg.telegram_msg_id)
    assert loaded is not None, (
        f"JsonlFileStore reload 后 get_message 返 None "
        f"(cid={msg.channel_id}, mid={msg.telegram_msg_id})"
    )
    # None 应保留 None(plan bug 预警:None → [] 是常见 bug)
    if msg.reactions is None:
        assert loaded.reactions is None, f"reactions=None 应保留 None(实际={loaded.reactions!r})"
        return  # type: ignore[return-value]

    # 有 reactions 的话必须相等
    assert loaded.reactions is not None
    assert len(loaded.reactions) == len(msg.reactions)
    for orig, roundtripped in zip(msg.reactions, loaded.reactions, strict=False):
        assert orig.emoji == roundtripped.emoji
        assert orig.count == roundtripped.count
        assert orig.is_chosen == roundtripped.is_chosen


# ============== PR 6 known bug lock ==============


@pytest.mark.xfail(
    reason="PR 6 known bug 2026-09-17:含 NEL (\\x85) 的 str 字段被 ChannelFile.load 静默 skip;follow-up PR 改 strict=False",
    strict=True,  # 真修了反而 xpass → 测试 fail 提示
)
@pytest.mark.asyncio
async def test_jsonl_nel_bug_locked(tmp_path: Path) -> None:
    """**PR 6 已发现 bug**(2026-09-17):`\x85` (NEL) 让 JsonlFileStore 静默丢行。

    写一行 notes='\x85' → reload → `get_message` 返 None。预期**此测试失败**,
    提醒开 follow-up PR 改 `ChannelFile.load` 的 `json.loads(..., strict=False)`
    或 escape 控制字符。
    """
    from datetime import UTC, datetime

    from tgmonitor.core.dto import MessageDTO

    d = tmp_path / "store"
    msg = MessageDTO(
        id=1,
        channel_id=42,
        telegram_msg_id=7,
        text="hi",
        date=datetime(2026, 9, 17, tzinfo=UTC),
        notes="\x85",  # NEL — Next-Line Control Char
    )
    store = JsonlFileStore(d)
    await store.connect()
    await store.save_message(msg)
    await store.close()

    store2 = JsonlFileStore(d)
    await store2.connect()
    loaded = await store2.get_message(42, 7)

    # **此断言预期失败**,作为 reminder — fix 后会 pass
    assert loaded is not None, "BUG 已修:含 NEL 的消息 reload 不再丢行"
    if loaded is not None:
        assert loaded.notes == "\x85", "BUG 已修:NEL 字符 round-trip 不丢字段值"
