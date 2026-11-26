"""FakeTelegramClient 媒体 / 历史接口单测 — 对齐 P2-23 / P2-27 修复后的契约。

- `download_file_to`:dest 落盘返 dest_path / 失败返 None / max_bytes 超限返 None
- `iter_chat_history`:limit=None 全量、limit=N 取最新 N 条(before_msg_id=0)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tgmonitor.core.telegram.fake_client import FakeTelegramClient

pytestmark = pytest.mark.asyncio

_FID = "tl:-100123:10"


async def test_fake_download_file_to_writes_dest(tmp_path: Path) -> None:
    client = FakeTelegramClient()
    client.set_download(_FID, b"payload")
    dest = tmp_path / "media.mp4"
    out = await client.download_file_to(_FID, dest)
    assert out == dest
    assert dest.read_bytes() == b"payload"


async def test_fake_download_file_to_failure_returns_none(tmp_path: Path) -> None:
    client = FakeTelegramClient()
    dest = tmp_path / "media.mp4"
    assert await client.download_file_to(_FID, dest) is None
    assert not dest.exists()


async def test_fake_download_file_to_max_bytes_returns_none(tmp_path: Path) -> None:
    client = FakeTelegramClient()
    client.set_download(_FID, b"x" * 300)
    dest = tmp_path / "media.mp4"
    assert await client.download_file_to(_FID, dest, max_bytes=200) is None
    assert not dest.exists()


async def test_fake_download_file_to_progress_final(tmp_path: Path) -> None:
    client = FakeTelegramClient()
    client.set_download(_FID, b"abc")
    dest = tmp_path / "media.mp4"
    calls: list[tuple[int, int | None]] = []

    async def cb(downloaded: int, total: int | None) -> None:
        calls.append((downloaded, total))

    await client.download_file_to(_FID, dest, progress_callback=cb)
    assert (3, 3) in calls  # 终值


async def test_fake_iter_chat_history_limit_none_full() -> None:
    client = FakeTelegramClient()
    client.set_history(1, max_id=10, count=10)
    got = [m.telegram_msg_id async for m in client.iter_chat_history(1)]
    assert got == list(range(1, 11))  # 全量


async def test_fake_iter_chat_history_limit_takes_newest_n() -> None:
    """P2-23:limit=N + before_msg_id=0 → 最新 N 条(与真实现语义对齐)。"""
    client = FakeTelegramClient()
    client.set_history(1, max_id=10, count=10)
    got = [m.telegram_msg_id async for m in client.iter_chat_history(1, limit=3)]
    assert got == [8, 9, 10]
