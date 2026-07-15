"""JsonlFileStore 单测 — 验证文件后端与抽象语义对齐。"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from tgmonitor.core.dto import ChannelDTO, MediaDTO, MediaType, MessageDTO
from tgmonitor.core.storage.jsonl_store import JsonlFileStore


async def test_upsert_channel_and_list(tmp_path: Path):
    store = JsonlFileStore(root=tmp_path)
    await store.connect()
    await store.init_schema()
    await store.upsert_channel(ChannelDTO(id=1, title="a"))
    await store.upsert_channel(ChannelDTO(id=2, title="b", username="b"))
    chs = await store.list_channels()
    assert {c.id for c in chs} == {1, 2}
    assert (tmp_path / "channels.json").exists()


async def test_save_and_idempotent(tmp_path: Path):
    store = JsonlFileStore(root=tmp_path)
    await store.connect()
    m = MessageDTO(
        id=0,
        channel_id=100,
        telegram_msg_id=1,
        date=datetime(2026, 1, 1, 12, 0, 0),
        text="hello",
    )
    pk1 = await store.save_message(m)
    pk2 = await store.save_message(m)
    assert pk1 == pk2  # upsert
    assert await store.count_messages(100) == 1


async def test_message_with_media_roundtrip(tmp_path: Path):
    store = JsonlFileStore(root=tmp_path)
    await store.connect()
    m = MessageDTO(
        id=0,
        channel_id=5,
        telegram_msg_id=1,
        date=datetime(2026, 5, 1, 12, 0, 0),
        text="photo!",
        media=[
            MediaDTO(
                type=MediaType.PHOTO,
                mime_type="image/jpeg",
                file_name="x.jpg",
                file_size=1234,
                width=800,
                height=600,
                object_key="media/abc.jpg",
                object_backend="local",
                thumb_key="media/abc.thumb",
                thumb_backend="local",
            )
        ],
    )
    await store.save_message(m)
    # 重新连接 → 应从文件恢复
    await store.close()
    store2 = JsonlFileStore(root=tmp_path)
    await store2.connect()
    out = await store2.get_message(5, 1)
    assert out is not None
    assert out.text == "photo!"
    assert len(out.media) == 1
    assert out.media[0].type == MediaType.PHOTO
    assert out.media[0].object_key == "media/abc.jpg"


async def test_list_messages_sorted_and_filtered(tmp_path: Path):
    store = JsonlFileStore(root=tmp_path)
    await store.connect()
    base = datetime(2026, 1, 1, 12, 0, 0)
    for i, cid in enumerate((1, 2)):
        for j in range(3):
            await store.save_message(
                MessageDTO(
                    id=0,
                    channel_id=cid,
                    telegram_msg_id=j,
                    date=base + timedelta(minutes=i * 10 + j),
                    text=f"c{cid} m{j}",
                )
            )
    out = await store.list_messages([1, 2])
    texts = [m.text for m in out]
    # 每频道内按时间升序;频道间按 id 升序
    assert texts == ["c1 m0", "c1 m1", "c1 m2", "c2 m0", "c2 m1", "c2 m2"]


async def test_delete_message_and_channel(tmp_path: Path):
    store = JsonlFileStore(root=tmp_path)
    await store.connect()
    await store.upsert_channel(ChannelDTO(id=7, title="x"))
    await store.save_message(
        MessageDTO(id=0, channel_id=7, telegram_msg_id=1, text="a", date=datetime.utcnow())
    )
    await store.save_message(
        MessageDTO(id=0, channel_id=7, telegram_msg_id=2, text="b", date=datetime.utcnow())
    )
    await store.delete_message(7, 1)
    assert await store.count_messages(7) == 1
    await store.delete_channel(7)
    assert await store.count_messages(7) == 0
    assert (tmp_path / "messages" / "7.jsonl").exists() is False
