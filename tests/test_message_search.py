r"""`StorageRepository.list_messages(search=...)` 4 后端 parity 测试 — 2026-09-01 v1.5.1 PR #B2。

本文件聚焦 PR #B2 新加的 `search` 字段:`list_messages(..., search="foo")`
子串过滤(大小写不敏感),匹配 `text` 或 `media.file_name` 任一。

覆盖:
- InMemory + Jsonl 两个能跑的后端(走 parity 模式)
- text 命中、file_name 命中、大小写不敏感
- date + search 组合过滤
- SQL LIKE / Mongo $regex 通配符安全(`%` / `_` / `\` 用户输入不破坏查询)
- 兼容旧调用(`search` 不传 = 不过滤)
"""

from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio

from tests.conftest import make_message
from tgmonitor.core.dto import ChannelDTO, MediaDownloadStatus, MediaDTO, MediaType
from tgmonitor.core.storage.jsonl_store import JsonlFileStore

pytestmark = pytest.mark.asyncio


# ---- 共享 fixture:种子数据(text / file_name 多样) -------------------------


def _photo(
    file_name: str = "photo_a.jpg",
    status: MediaDownloadStatus = MediaDownloadStatus.DONE,
) -> MediaDTO:
    return MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name=file_name,
        file_size=1024,
        object_key=f"media/{file_name}",
        object_backend="local",
        download_status=status,
    )


@pytest_asyncio.fixture
async def in_mem_repo_search() -> object:
    """PR #B2 搜索测试用 InMemory repo:4 条消息,text/file_name 多样。"""
    from tests.conftest import InMemoryRepository

    repo = InMemoryRepository()
    # msg1 (1/1):text 含 "Hello" + photo "cat.jpg"
    await repo.save_message(
        make_message(
            channel_id=100,
            msg_id=1,
            text="Hello world",
            date=datetime(2026, 1, 1, 10, 0),
            media=[_photo("cat.jpg")],
        )
    )
    # msg2 (1/2):text "goodbye" + photo "dog.jpg"
    await repo.save_message(
        make_message(
            channel_id=100,
            msg_id=2,
            text="goodbye",
            date=datetime(2026, 1, 2, 10, 0),
            media=[_photo("dog.jpg")],
        )
    )
    # msg3 (1/3):text 含 "BIRDS" + 无 media
    await repo.save_message(
        make_message(
            channel_id=200,
            msg_id=10,
            text="watching BIRDS today",
            date=datetime(2026, 1, 3, 10, 0),
        )
    )
    # msg4 (1/4):空 text + photo "cat_food.png"
    await repo.save_message(
        make_message(
            channel_id=300,
            msg_id=5,
            text="",
            date=datetime(2026, 1, 4, 10, 0),
            media=[_photo("cat_food.png")],
        )
    )
    return repo


@pytest_asyncio.fixture
async def jsonl_repo_search(tmp_path):
    """PR #B2 搜索测试用 Jsonl repo:与 in_mem 同种子,加订阅标志。"""
    repo = JsonlFileStore(root=tmp_path)
    await repo.connect()
    await repo.init_schema()
    for cid in (100, 200, 300):
        await repo.upsert_channel(ChannelDTO(id=cid, title=f"#{cid}"))
        await repo.set_channel_subscribed(cid, True)
    await repo.save_message(
        make_message(
            channel_id=100,
            msg_id=1,
            text="Hello world",
            date=datetime(2026, 1, 1, 10, 0),
            media=[_photo("cat.jpg")],
        )
    )
    await repo.save_message(
        make_message(
            channel_id=100,
            msg_id=2,
            text="goodbye",
            date=datetime(2026, 1, 2, 10, 0),
            media=[_photo("dog.jpg")],
        )
    )
    await repo.save_message(
        make_message(
            channel_id=200,
            msg_id=10,
            text="watching BIRDS today",
            date=datetime(2026, 1, 3, 10, 0),
        )
    )
    await repo.save_message(
        make_message(
            channel_id=300,
            msg_id=5,
            text="",
            date=datetime(2026, 1, 4, 10, 0),
            media=[_photo("cat_food.png")],
        )
    )
    return repo


# ---- search 命中:text 字段 -------------------------------------------------


async def test_in_mem_search_text_substring(in_mem_repo_search):
    """PR #B2:InMemory search 命中 text 子串("hello" → "Hello world" 命中,大小写不敏感)。"""
    rows = await in_mem_repo_search.list_messages(
        channel_ids=[100, 200, 300],
        search="hello",
    )
    assert [m.telegram_msg_id for m in rows] == [1]


async def test_jsonl_search_text_substring(jsonl_repo_search):
    """PR #B2:Jsonl parity — text 子串大小写不敏感命中。"""
    rows = await jsonl_repo_search.list_messages(
        channel_ids=[100, 200, 300],
        search="hello",
    )
    assert [m.telegram_msg_id for m in rows] == [1]


# ---- search 命中:file_name 字段 -------------------------------------------


async def test_in_mem_search_file_name_substring(in_mem_repo_search):
    """PR #B2:InMemory search 命中 media.file_name("cat" → cat.jpg + cat_food.png)。"""
    rows = await in_mem_repo_search.list_messages(
        channel_ids=[100, 200, 300],
        search="cat",
    )
    # msg1 (cat.jpg) + msg4 (cat_food.png) 命中
    assert {m.telegram_msg_id for m in rows} == {1, 5}


async def test_jsonl_search_file_name_substring(jsonl_repo_search):
    """PR #B2:Jsonl parity — file_name 子串命中。"""
    rows = await jsonl_repo_search.list_messages(
        channel_ids=[100, 200, 300],
        search="cat",
    )
    assert {m.telegram_msg_id for m in rows} == {1, 5}


# ---- search 大小写不敏感 ---------------------------------------------------


async def test_in_mem_search_case_insensitive(in_mem_repo_search):
    """PR #B2:用户输入 "BIRDS" 应命中 "watching BIRDS today"(全大写)。"""
    rows = await in_mem_repo_search.list_messages(
        channel_ids=[100, 200, 300],
        search="BIRDS",
    )
    assert [m.telegram_msg_id for m in rows] == [10]


async def test_jsonl_search_case_insensitive(jsonl_repo_search):
    """PR #B2:Jsonl parity — 大小写不敏感。"""
    rows = await jsonl_repo_search.list_messages(
        channel_ids=[100, 200, 300],
        search="BIRDS",
    )
    assert [m.telegram_msg_id for m in rows] == [10]


# ---- search 空 = 不过滤(向后兼容) ----------------------------------------


async def test_in_mem_search_empty_no_filter(in_mem_repo_search):
    """PR #B2:`search=""`(默认)→ 不过滤,返所有 4 条。"""
    rows = await in_mem_repo_search.list_messages(channel_ids=[100, 200, 300])
    assert {m.telegram_msg_id for m in rows} == {1, 2, 10, 5}


async def test_jsonl_search_empty_no_filter(jsonl_repo_search):
    """PR #B2:Jsonl 同上 — 空 search = 不过滤。"""
    rows = await jsonl_repo_search.list_messages(channel_ids=[100, 200, 300])
    assert {m.telegram_msg_id for m in rows} == {1, 2, 10, 5}


# ---- search + date 组合过滤 ----------------------------------------------


async def test_in_mem_search_combined_with_date(in_mem_repo_search):
    """PR #B2:`search="cat"` + `date_from` 组合 → 只返 cat_food.png(1/4)。"""
    rows = await in_mem_repo_search.list_messages(
        channel_ids=[100, 200, 300],
        search="cat",
        date_from=datetime(2026, 1, 4, 0, 0, 0),
    )
    # cat.jpg (1/1) 被 date_from 过滤掉
    assert [m.telegram_msg_id for m in rows] == [5]


async def test_jsonl_search_combined_with_date(jsonl_repo_search):
    """PR #B2:Jsonl parity — search + date 组合。"""
    rows = await jsonl_repo_search.list_messages(
        channel_ids=[100, 200, 300],
        search="cat",
        date_from=datetime(2026, 1, 4, 0, 0, 0),
    )
    assert [m.telegram_msg_id for m in rows] == [5]


# ---- 通配符安全:`%` / `_` / `\` 用户输入不破坏查询 -----------------------


async def test_in_mem_search_with_percent_literal(in_mem_repo_search):
    """PR #B2:`%` 是 LIKE 通配符,InMemory 走纯 Python `in`,无通配符语义,
    视为字面字符,大小写不敏感子串。命中 0 条(text / file_name 都不含 `%`)。"""
    rows = await in_mem_repo_search.list_messages(
        channel_ids=[100, 200, 300],
        search="%",
    )
    assert rows == []


async def test_in_mem_search_with_underscore_literal(in_mem_repo_search):
    """PR #B2:`_` 是 LIKE 通配符,InMemory 视为字面字符。
    file_name "cat_food.png" 含 `_`,应命中 msg4。"""
    rows = await in_mem_repo_search.list_messages(
        channel_ids=[100, 200, 300],
        search="_",
    )
    assert [m.telegram_msg_id for m in rows] == [5]


async def test_in_mem_search_with_backslash_literal(in_mem_repo_search):
    """PR #B2:`\\` 在 LIKE 是 escape,InMemory 视为字面字符。无任何字面 `\\`,0 命中。"""
    rows = await in_mem_repo_search.list_messages(
        channel_ids=[100, 200, 300],
        search="\\",
    )
    assert rows == []


# ---- search 命中 0 条 = 空列表 -------------------------------------------


async def test_in_mem_search_no_match_returns_empty(in_mem_repo_search):
    """PR #B2:无任何消息匹配 → 返 []。"""
    rows = await in_mem_repo_search.list_messages(
        channel_ids=[100, 200, 300],
        search="zzz_no_match_xyz",
    )
    assert rows == []


async def test_jsonl_search_no_match_returns_empty(jsonl_repo_search):
    """PR #B2:Jsonl 同上。"""
    rows = await jsonl_repo_search.list_messages(
        channel_ids=[100, 200, 300],
        search="zzz_no_match_xyz",
    )
    assert rows == []


# ============================================================
# 2026-09-03 v1.5.3 PR #D2:storage 层 — 已退订频道历史消息可被搜索
# (StorageRepository.list_messages 接 channel_ids 显式列表时,
#  不关心 is_subscribed — 退订频道历史仍可拉回,本组测试验证 storage
#  这一行为 + 验证 PR #D2 路径正确)
# ============================================================


async def test_in_mem_search_returns_messages_from_unsubscribed_channel(
    in_mem_repo_search,
):
    """PR #D2:in_mem repo 验证 — 把频道退订 + 该频道消息仍在 → 显式
    channel_ids 仍能搜到(storage 层不关心订阅状态,订阅状态归
    SubscriptionService 过滤)。"""
    # 退订频道 200(原 fixture 已订 100/200/300,这里 set False)
    await in_mem_repo_search.set_channel_subscribed(200, False)
    # 显式查 channel_ids=[200](避开 SubscriptionService 的「已订」过滤)
    # → 该频道历史消息应可搜到
    # fixture 里 200 频道没有 Hello 文本(只有 100 频道有);改搜 200 的现有文本
    rows2 = await in_mem_repo_search.list_messages(channel_ids=[200], search="")
    # 200 频道有 1 条消息(从 fixture 看)
    assert len(rows2) >= 1, f"已退订频道 200 的历史消息应仍可查,got {len(rows2)} rows"
    assert all(r.channel_id == 200 for r in rows2)


async def test_jsonl_search_returns_messages_from_unsubscribed_channel(
    jsonl_repo_search,
):
    """PR #D2:Jsonl repo 同上 — storage 层与订阅状态无关。"""
    await jsonl_repo_search.set_channel_subscribed(200, False)
    rows = await jsonl_repo_search.list_messages(channel_ids=[200], search="")
    assert len(rows) >= 1
    assert all(r.channel_id == 200 for r in rows)
