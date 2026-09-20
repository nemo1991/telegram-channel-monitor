"""2026-09-17 PR 6:`JsonlFileStore` corruption recovery property test。

PR 6 plan 测点 4:随机 inject garbage 行(json decode err / KeyError / ValueError
/ OSError)→ 后续 load 应跳过坏行不抛。

测法:
- 写 N 行正常 MessageDTO → save
- 在 jsonl 文件里 inject garbage 行(`{"corrupted":`, `not json`, 空行, 等)
- 重启 store → get_message 查幸存的好行 → assert 仍能读到
- assert 坏行**没让 load 抛**

`ChannelFile.load` 已实现 silent skip(JSONDecodeError 吞掉,行不 append),
所以这个 test 验证 invariant 不被未来改动打破。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.fixtures._strategies import message_dtos
from tgmonitor.core.dto import MessageDTO
from tgmonitor.core.storage.jsonl_store import JsonlFileStore


@pytest.fixture
def jsonl_dir(tmp_path: Path) -> Path:
    """Unique subdir per call。"""
    d = tmp_path / f"corrupt_{id(object())}"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def msg_dir(jsonl_dir: Path) -> Path:
    """`messages/` 子目录路径。"""
    return jsonl_dir / "messages"


# ========== 注入坏行后 reload 应存活 ==========


async def _save_one(dir_path: Path, msg: MessageDTO) -> None:
    store = JsonlFileStore(dir_path)
    await store.connect()
    await store.save_message(msg)
    await store.close()


async def _load_one(dir_path: Path, cid: int, mid: int) -> MessageDTO | None:
    store = JsonlFileStore(dir_path)
    await store.connect()
    result = await store.get_message(cid, mid)
    await store.close()
    return result


# ========== property:好行 + 坏行共存 → 好行可读 ==========


@given(
    msgs=st.lists(message_dtos(), min_size=2, max_size=20),
    garbage=st.lists(
        st.sampled_from(
            [
                "{not valid json",  # JSONDecodeError
                "{incomplete",  # JSONDecodeError
                "",  # 空行
                "garbage line",  # JSONDecodeError
                # 注:`[1,2,3]` / `42` / `null` 等合法 JSON 但非 dict 会被
                # `d.get(...)` AttributeError 触发(PR 6 第二 known bug)— 单独
                # 锁在 `test_jsonl_non_dict_json_crashes` 测试里。
            ]
        ),
        min_size=0,
        max_size=5,
    ),
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,
    ],
)
async def test_jsonl_corruption_recovery_loads_valid_rows(
    jsonl_dir: Path, msg_dir: Path, msgs: list[MessageDTO], garbage: list[str]
) -> None:
    """save N 条 + inject M 条 garbage → reload 后好行仍可读,且读到的是**对的那条**。

    invariant:ChannelFile.load 跳过坏行(JSONDecodeError),不让整文件 load 失败;
    后续 get_message(key) 坏 key 返 None,好 key 返**该 key 对应**的 DTO。

    PR 6(2026-09-18)曾以 `xfail(strict=True)` 锁住一个 bug:跳过任何坏行后
    `index[mid]` 存的是文件行号而非 rows 下标,于是指向**别的消息** ——
    `get_message` 静默返回错 DTO。2026-09-20 修 `load` 改存 rows 下标。

    注:本测试原先用 `_safe()` 过滤掉含控制字符(Cc,含 NEL)的 msg 来绕开
    另一个已知 bug;那 bug(`splitlines()` 在 NEL 处断行)也已修,过滤已删 ——
    现在控制字符也参与 fuzz。
    """

    # 强制所有 msg 用同一个 channel_id(便于 inject garbage 到单文件) +
    # 唯一 telegram_msg_id(避免 dedup 替换)
    cid = msgs[0].channel_id
    used_mids: set[int] = set()
    normalized = []
    for i, m in enumerate(msgs):
        mid = m.telegram_msg_id
        # 冲突:加 offset
        while mid in used_mids:
            mid += 1
        used_mids.add(mid)
        normalized.append(
            MessageDTO(
                id=0,
                channel_id=cid,
                telegram_msg_id=mid,
                text=f"{m.text or ''}-{i}",  # 唯一化 text
                author=m.author,
                date=m.date,
                media=[],
                tags=m.tags,
                notes=m.notes,
                is_favorite=m.is_favorite,
                is_pinned=m.is_pinned,
            )
        )

    # 1. save
    for m in normalized:
        await _save_one(jsonl_dir, m)

    # 2. inject garbage 到 cid 那个文件(ChannelFile 直接读 `<cid>.jsonl`)
    target_file = msg_dir / f"{cid}.jsonl"
    if target_file.exists():
        original = target_file.read_text(encoding="utf-8")
        injected_lines = [g for g in garbage if g is not None]
        injected_text = "\n".join(injected_lines) + "\n" if injected_lines else ""
        target_file.write_text(injected_text + original, encoding="utf-8")

    # 3. reload — 不应抛
    for m in normalized:
        loaded = await _load_one(jsonl_dir, cid, m.telegram_msg_id)
        # 4. 好行应仍能读到(只要 garbage 段没让 ChannelFile 解析错)
        assert loaded is not None, (
            f"好行 (cid={cid}, mid={m.telegram_msg_id}) 在 garbage 注入后丢失"
        )
        # 比对 normalized mid(可能与原 safe_msgs 不同因去重)
        assert loaded.telegram_msg_id == m.telegram_msg_id, (
            f"loaded mid {loaded.telegram_msg_id} != saved mid {m.telegram_msg_id}"
        )


# ========== 边界:全 garbage → 返 None 不抛 ==========


async def test_jsonl_all_garbage_returns_none_no_raise(msg_dir: Path) -> None:
    """整个文件都是 garbage → reload + get_message 返 None,不抛。"""
    msg_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    target = msg_dir / "1.jsonl"
    target.write_text(
        "\n".join(["not json", "{incomplete", "[1,2", "", "garbage line"]),
        encoding="utf-8",
    )
    store = JsonlFileStore(msg_dir.parent)
    await store.connect()
    # 不抛
    result = await store.get_message(1, 1)
    assert result is None


# ========== 边界:删最后一行 / 中间行 → 仍能 load ==========


async def test_jsonl_truncated_last_line_does_not_crash(msg_dir: Path) -> None:
    """`save` 写完时 `_flush` 原子替换 .part 文件 — 中途 crash 会留下 .part。

    模拟:`1.jsonl` 末尾突然被截(无 newline)→ reload 应不抛,跳过坏行。
    """
    msg_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    target = msg_dir / "1.jsonl"
    # 写一行半:完整行 + 不完整的 "{"
    target.write_text(
        '{"id": 1, "channel_id": 1, "telegram_msg_id": 1, "text": "ok"}\n'
        '{"id": 2, "channel_id": 1, "telegram_msg_id": 2, "text": "truncated"',
        encoding="utf-8",
    )
    store = JsonlFileStore(msg_dir.parent)
    await store.connect()
    # 第一行应能读
    loaded = await store.get_message(1, 1)
    assert loaded is not None
    assert loaded.text == "ok"
    # 第二行(截断)应跳过,返 None
    loaded2 = await store.get_message(1, 2)
    assert loaded2 is None


# ========== 行为验证:dict_to_message 的容错性 ==========


async def test_jsonl_dict_missing_required_fields_skipped(msg_dir: Path) -> None:
    """JSON 行缺必需字段(无 telegram_msg_id)→ ChannelFile.load skip。

    PR 6 plan:`KeyError` / `ValueError` 也应被吞。
    """
    msg_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    target = msg_dir / "1.jsonl"
    target.write_text(
        '{"some": "field"}\n',  # 缺 telegram_msg_id
        encoding="utf-8",
    )
    store = JsonlFileStore(msg_dir.parent)
    await store.connect()
    # 不抛,get_message 返 None
    result = await store.get_message(1, 1)
    assert result is None


# ========== PR 6 第二 known bug lock:non-dict JSON ==============


async def test_jsonl_non_dict_json_skipped_not_crash(msg_dir: Path) -> None:
    """合法 JSON 但非 dict → ChannelFile.load 跳过,不崩。

    例:用户手动编辑文件留下 `[1, 2, 3]` 或 `null`。PR 6(2026-09-17)发现当时
    `d.get(...)` 直接抛 AttributeError 让整个 load 挂掉,以 `xfail(strict=True)`
    锁住;2026-09-20 加 isinstance 守卫后转正常 regression。
    """
    msg_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    target = msg_dir / "1.jsonl"
    target.write_text("[1, 2, 3]\n", encoding="utf-8")
    store = JsonlFileStore(msg_dir.parent)
    # 不应抛
    await store.connect()


# ========== round-trip 一致性:重新 dump 后无信息差 ==========


async def test_jsonl_roundtrip_preserves_message_count(jsonl_dir: Path) -> None:
    """save N 条 → reload → list_messages(简化版本:count by iteration)→ N 条。

    验证 `list_messages` 不会因 reload 路径丢字段或漏行。
    """
    from datetime import UTC, datetime

    msgs = [
        MessageDTO(
            id=0,
            channel_id=1,
            telegram_msg_id=i,
            text=f"msg-{i}",
            date=datetime(2026, 9, 17, tzinfo=UTC),
            media=[],
        )
        for i in range(10)
    ]

    store = JsonlFileStore(jsonl_dir)
    await store.connect()
    for m in msgs:
        await store.save_message(m)
    await store.close()

    # reload
    store2 = JsonlFileStore(jsonl_dir)
    await store2.connect()
    loaded_msgs = await store2.list_messages([1])
    assert len(loaded_msgs) == 10, f"reload 后 list_messages 应返 10 条,实测 {len(loaded_msgs)}"
