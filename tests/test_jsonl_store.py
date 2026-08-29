"""JsonlFileStore 单测 — 验证文件后端与抽象语义对齐。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tgmonitor.core.dto import ChannelDTO, MediaDownloadStatus, MediaDTO, MediaType, MessageDTO
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


async def test_list_messages_limit_keeps_most_recent(tmp_path: Path):
    """# 回归:limit 语义 = 最近 N 条(UI 启动加载「最近 200 条」),仍按升序返回。"""
    store = JsonlFileStore(root=tmp_path)
    await store.connect()
    base = datetime(2026, 1, 1, 12, 0, 0)
    for j in range(5):
        await store.save_message(
            MessageDTO(
                id=0,
                channel_id=1,
                telegram_msg_id=j,
                text=f"m{j}",
                date=base + timedelta(minutes=j),
            )
        )
    out = await store.list_messages([1], limit=2)
    assert [m.telegram_msg_id for m in out] == [3, 4]  # 最近 2 条,升序
    assert [m.text for m in out] == ["m3", "m4"]


async def test_delete_message_and_channel(tmp_path: Path):
    store = JsonlFileStore(root=tmp_path)
    await store.connect()
    await store.upsert_channel(ChannelDTO(id=7, title="x"))
    await store.save_message(
        MessageDTO(id=0, channel_id=7, telegram_msg_id=1, text="a", date=datetime.now(UTC))
    )
    await store.save_message(
        MessageDTO(id=0, channel_id=7, telegram_msg_id=2, text="b", date=datetime.now(UTC))
    )
    await store.delete_message(7, 1)
    assert await store.count_messages(7) == 1
    await store.delete_channel(7)
    assert await store.count_messages(7) == 0
    assert (tmp_path / "messages" / "7.jsonl").exists() is False


# ============================================================
# 2026-08-24:_media_by_fid 索引 re-evaluate(retry 路径需要 — DONE→PENDING 时清理 stale entry)
# ============================================================


def _photo_with_fid(file_id: str, **kw) -> MediaDTO:
    """构造带 telegram_file_id + DONE + object_key 的 photo media。"""
    base: dict = {
        "type": MediaType.PHOTO,
        "mime_type": "image/jpeg",
        "file_name": "p.jpg",
        "file_size": 1024,
        "telegram_file_id": file_id,
        "object_key": "media/abc.jpg",
        "object_backend": "local",
        "download_status": MediaDownloadStatus.DONE,
    }
    base.update(kw)
    return MediaDTO(**base)  # type: ignore[arg-type]


async def test_save_message_resets_status_cleans_media_index(tmp_path: Path):
    """DONE→PENDING 重置后,fid 不再有 DONE 引用 → 索引清掉。

    旧 bug:索引只 ADD 不 REMOVE,DONE→PENDING 后 `find_media_by_file_id`
    仍返旧 DONE DTO,retry 路径 skip #1 误命中(以为已下载),不去下。
    """
    store = JsonlFileStore(root=tmp_path)
    await store.connect()
    fid = "fid-A"
    # 第 1 次 save:DONE
    m = MessageDTO(
        id=0,
        channel_id=100,
        telegram_msg_id=1,
        text="v1",
        date=datetime.now(UTC),
        media=[_photo_with_fid(fid)],
    )
    await store.save_message(m)
    assert await store.find_media_by_file_id(fid) is not None
    # 第 2 次 save:同 (channel_id, telegram_msg_id) 覆盖,media 改 PENDING
    # (模拟 retry 路径:AppService.retry_media 摘掉 DONE 标 PENDING 再 force 重下)
    m_v2 = MessageDTO(
        id=0,
        channel_id=100,
        telegram_msg_id=1,
        text="v2",
        date=datetime.now(UTC),
        media=[
            MediaDTO(
                type=MediaType.PHOTO,
                mime_type="image/jpeg",
                file_name="p.jpg",
                file_size=1024,
                telegram_file_id=fid,
                download_status=MediaDownloadStatus.PENDING,
            )
        ],
    )
    await store.save_message(m_v2)
    # 现在 storage 里该 fid 状态是 PENDING,索引不该再返 DONE entry
    assert await store.find_media_by_file_id(fid) is None


async def test_delete_message_cleans_media_index(tmp_path: Path):
    """删唯一引用 fid 的 message → 索引清;另一 message 仍引用 → 索引留。"""
    store = JsonlFileStore(root=tmp_path)
    await store.connect()
    fid = "fid-X"
    # 频道 1:DONE,有 fid
    await store.save_message(
        MessageDTO(
            id=0,
            channel_id=1,
            telegram_msg_id=10,
            text="a",
            date=datetime.now(UTC),
            media=[_photo_with_fid(fid)],
        )
    )
    # 频道 2:同 fid 另一 message
    await store.save_message(
        MessageDTO(
            id=0,
            channel_id=2,
            telegram_msg_id=20,
            text="b",
            date=datetime.now(UTC),
            media=[_photo_with_fid(fid)],
        )
    )
    assert await store.find_media_by_file_id(fid) is not None
    # 删频道 1 的引用 → 频道 2 还在,索引留
    await store.delete_message(1, 10)
    assert await store.find_media_by_file_id(fid) is not None
    # 删频道 2 的引用 → 索引清
    await store.delete_message(2, 20)
    assert await store.find_media_by_file_id(fid) is None


async def test_retry_path_finds_no_prior_after_reset(tmp_path: Path):
    """完整 retry 序列:DONE → PENDING(索引清)→ DONE(索引再填)。

    端到端模拟:AppService.retry_media 重置后,后续 download_one 不该走 skip #1。
    """
    store = JsonlFileStore(root=tmp_path)
    await store.connect()
    fid = "fid-R"
    # DONE
    await store.save_message(
        MessageDTO(
            id=0,
            channel_id=100,
            telegram_msg_id=1,
            text="",
            date=datetime.now(UTC),
            media=[_photo_with_fid(fid)],
        )
    )
    # 重置:在 retry 路径里,storage.update_message 把 media 改成 PENDING,
    # 后续 download_one(force=True) 会跳过 skip #1 重下
    await store.save_message(
        MessageDTO(
            id=0,
            channel_id=100,
            telegram_msg_id=1,
            text="",
            date=datetime.now(UTC),
            media=[
                MediaDTO(
                    type=MediaType.PHOTO,
                    mime_type="image/jpeg",
                    file_name="p.jpg",
                    telegram_file_id=fid,
                    download_status=MediaDownloadStatus.PENDING,
                )
            ],
        )
    )
    assert await store.find_media_by_file_id(fid) is None
    # 再 DONE:重新落库(模拟 download_one 重下完回写 storage)
    await store.save_message(
        MessageDTO(
            id=0,
            channel_id=100,
            telegram_msg_id=1,
            text="",
            date=datetime.now(UTC),
            media=[_photo_with_fid(fid)],
        )
    )
    assert await store.find_media_by_file_id(fid) is not None
