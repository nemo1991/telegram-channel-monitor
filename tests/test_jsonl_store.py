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


# ---- list_media tie-break parity(Jsonl vs PG/Mongo)----


async def test_list_media_tie_break_idx_asc_within_message(tmp_path: Path) -> None:
    """2026-09-26 fix(parity-tiebreak):单 message 含 N media 时,
    list_media 必须按 media_idx ASC 返回(与 PG / Mongo tie-break 对齐)。

    之前 `_sort_media_rows` 整组 `reverse=True` 把 tie-break 也反转了,
    导致 1 message + 3 media 返 `[2, 1, 0]` 而 PG 返 `[0, 1, 2]`,
    集成测试 `test_list_media_consistent_with_jsonl` 失败。

    tie-break 方向是**固定的**(`msg.id DESC, idx ASC`),不随 primary 反转。
    """
    from tgmonitor.core.dto import SortDir, SortKey

    store = JsonlFileStore(root=tmp_path)
    await store.connect()
    await store.init_schema()
    await store.upsert_channel(ChannelDTO(id=42, title="T"))
    await store.save_message(
        MessageDTO(
            id=0,
            channel_id=42,
            telegram_msg_id=1,
            text="hi",
            date=datetime(2026, 1, 1, tzinfo=UTC),
            media=[
                _photo_with_fid("a"),
                _photo_with_fid("b"),
                _photo_with_fid("c"),
            ],
        )
    )

    # DATE DESC(默认)—— tie-break 应仍是 idx ASC(不变向)
    rows = await store.list_media()
    assert [r[1] for r in rows] == [0, 1, 2], (
        f"DATE DESC 下 idx 必须 ASC(等效 PG `ORDER BY m.id DESC, media_idx ASC`),"
        f" 实际 {[r[1] for r in rows]}"
    )

    # DATE ASC—— 同样 tie-break 仍是 idx ASC(不变向)
    rows_asc = await store.list_media(sort=SortKey.DATE, sort_dir=SortDir.ASC)
    assert [r[1] for r in rows_asc] == [0, 1, 2], (
        f"DATE ASC 下 idx 也必须 ASC:实际 {[r[1] for r in rows_asc]}"
    )


async def test_list_media_tie_break_msg_id_desc_across_messages(tmp_path: Path) -> None:
    """跨 message 时 secondary tie-break 必须是 `msg.id DESC`(不随 primary 反转)。

    之前 `reverse=True` 把 secondary 也反转,跨 message 时新插入的 message
    排在前面 — 但 PG 是稳定的(新插入的 message id 较大 → DESC 时在前),
    反向结果偶然一致。但同 date 多 message 跨方向就分叉了 — 此测试守住
    这条边界。
    """
    from tgmonitor.core.dto import SortDir, SortKey

    store = JsonlFileStore(root=tmp_path)
    await store.connect()
    await store.init_schema()
    await store.upsert_channel(ChannelDTO(id=99, title="T"))

    # 两条 message,同日;save_message 先 m1 后 m2 → m2.id > m1.id
    same_date = datetime(2026, 6, 1, tzinfo=UTC)
    await store.save_message(
        MessageDTO(
            id=0,
            channel_id=99,
            telegram_msg_id=1,
            text="m1",
            date=same_date,
            media=[_photo_with_fid("m1a")],
        )
    )
    await store.save_message(
        MessageDTO(
            id=0,
            channel_id=99,
            telegram_msg_id=2,
            text="m2",
            date=same_date,
            media=[_photo_with_fid("m2a"), _photo_with_fid("m2b")],
        )
    )

    # DATE DESC(默认):m2 应在前(m2.id 较大,DESC 时在前),内 media_idx ASC
    rows = await store.list_media()
    file_ids = [r[2].telegram_file_id for r in rows]
    assert file_ids == ["m2a", "m2b", "m1a"], (
        f"DATE DESC 应先 m2(后插入,id 大)再 m1,内部 idx ASC:实际 {file_ids}"
    )

    # DATE ASC:跨 msg secondary tie-break 仍是 m.id DESC(PG 一致)— 所以 m2 仍在前
    rows_asc = await store.list_media(sort=SortKey.DATE, sort_dir=SortDir.ASC)
    file_ids_asc = [r[2].telegram_file_id for r in rows_asc]
    assert file_ids_asc == ["m2a", "m2b", "m1a"], (
        f"DATE ASC 下 secondary `m.id DESC` 不变向,仍 m2 先 m1 后, 内部 idx ASC:实际 {file_ids_asc}"
    )
