"""Exporter 快照测试 — JSON / CSV / Markdown / HTML。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tests.conftest import make_message, make_photo
from tgmonitor.core.dto import ChannelDTO, ExportFormat, ExportRequest, MessageDTO
from tgmonitor.core.events import EventBus

# 注册到全局 EXPORTERS 注册表。即使模块里没有直接用类名,也得 import。
from tgmonitor.core.export.service import ExportService


async def _setup(tmp_path):
    bus = EventBus()
    from tests.conftest import InMemoryRepository
    from tgmonitor.core.objectstore.local_store import LocalObjectStore

    storage = InMemoryRepository()
    objects = LocalObjectStore(root=tmp_path / "media")
    await objects.connect()
    # 一张缩略图入 ObjectStore
    await objects.put("media/abc.jpg.thumb", b"\xff\xd8\xff\xd9fake-jpeg", None)

    ch1 = ChannelDTO(id=100, title="新闻频道", username="news")
    ch2 = ChannelDTO(id=200, title="Tech", username="tech")
    await storage.upsert_channel(ch1)
    await storage.upsert_channel(ch2)

    base = datetime(2026, 1, 1, 12, 0, 0)
    await storage.save_message(make_message(channel_id=100, msg_id=1, text="第一条", date=base))
    await storage.save_message(make_photo(channel_id=200, msg_id=1))
    await storage.save_message(make_message(channel_id=200, msg_id=2, text="再见", date=base))

    return storage, objects, bus, [ch1, ch2]


def _req(fmt: ExportFormat, out_path: Path) -> ExportRequest:
    return ExportRequest(
        channel_ids=[100, 200],
        date_from=None,
        date_to=None,
        format=fmt,
        out_path=str(out_path),
    )


@pytest.mark.parametrize(
    "fmt,ext",
    [
        (ExportFormat.JSON, ".json"),
        (ExportFormat.CSV, ".csv"),
        (ExportFormat.MARKDOWN, ".md"),
        (ExportFormat.HTML, ".html"),
    ],
)
async def test_export_each_format(tmp_path, fmt, ext):
    storage, objects, bus, _ = await _setup(tmp_path)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / f"out{ext}"
    req = _req(fmt, out)
    async for _ in svc.run(req):
        pass
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    if fmt == ExportFormat.JSON:
        import json

        d = json.loads(text)
        assert d["schema"] == "tgmonitor.export/v1"
        assert len(d["messages"]) == 3
    elif fmt == ExportFormat.CSV:
        assert "新闻频道" in text
        assert "photo" in text  # media_types 列
    elif fmt == ExportFormat.MARKDOWN:
        assert "## 新闻频道" in text
        assert "photo" in text
    elif fmt == ExportFormat.HTML:
        assert "<html" in text
        assert "新闻频道" in text


# ---- 2026-08-25 v1.3.0 PR #7:per-media CSV exporter + dispatcher ----------


# ---------------------------------------------------------------------------
# 2026-08-27 v1.4.0 PR #12:导出分页 bug(>500 静默截断)— 加 4 个回归测试
# ---------------------------------------------------------------------------


async def _bulk_seed_messages(storage, channel_id: int, count: int) -> None:
    """批量塞 count 条 message 到 storage;时间从 2026-01-01 开始,id 1..count。

    用来触发 ExportService 的 PAGE_SIZE=500 分页边界。
    """
    from tests.conftest import make_message

    base = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(1, count + 1):
        await storage.save_message(
            make_message(
                channel_id=channel_id,
                msg_id=i,
                text=f"m{i}",
                date=base,
            )
        )


# ---------------------------------------------------------------------------
# 2026-08-27 v1.4.0 PR #17:导出器注入修复 — 单元级 + 端到端测试
# ---------------------------------------------------------------------------


def test_guard_csv_cell_formula_prefix():
    """PR #17:`=+-@` / Tab / CR 开头 → 加 `'` 前缀。"""
    from tgmonitor.core.export.guards import _guard_csv_cell

    # 公式注入前缀都加单引号
    for bad in ("=cmd|'/c calc'!A1", "+sum(a1:a2)", "-1+1", "@SUM(1+1)*cmd|'/c calc'!A0"):
        out = _guard_csv_cell(bad)
        assert out.startswith("'"), f"prefix not guarded: {bad!r} → {out!r}"
        assert out == "'" + bad
    # Tab / CR 也是 Excel 4.0 宏触发链
    for bad in ("\tcmd", "\rcmd"):
        out = _guard_csv_cell(bad)
        assert out.startswith("'")
    # 正常内容原样
    assert _guard_csv_cell("hello.jpg") == "hello.jpg"
    assert _guard_csv_cell("照片_2026.jpg") == "照片_2026.jpg"
    assert _guard_csv_cell(None) == ""
    assert _guard_csv_cell("") == ""


def test_scrub_markdown_javascript_link():
    """PR #17:`[text](javascript:...)` → 协议前缀移除,链接变普通文本。"""
    from tgmonitor.core.export.guards import _scrub_markdown

    out = _scrub_markdown("[click](javascript:alert(1))")
    # `javascript:` 协议被剥离,链接变成普通 `[click](alert(1))`
    # Markdown 渲染器不会再把括号内当 JS 协议执行
    assert "javascript:" not in out
    assert out.startswith("[click](")
    # 普通 https 链接保持
    assert _scrub_markdown("[ok](https://example.com)") == "[ok](https://example.com)"


# ---------------------------------------------------------------------------
# 2026-09-01 v1.5.1 PR #B4:ZIP 导出 — 单元级 + dispatcher + Zip Slip 防御
# ---------------------------------------------------------------------------


def test_zip_sanitize_arcname_zipslip():
    """PR #B4:`_sanitize_arcname` 把 `../etc/passwd` 改成 `_/_/etc/passwd` —
    Zip Slip 防御单元测,纯函数。每段 `..` 都替换成 `_`,不会越出 zip 根。
    """
    from tgmonitor.core.export.zip_exporter import _sanitize_arcname

    # ../ 段全部替换成 _;`../../etc/passwd` 4 段 → 4 个 `_/_/_/etc/passwd`
    assert _sanitize_arcname("../../etc/passwd") == "_/_/etc/passwd"
    assert _sanitize_arcname("a/../../b") == "a/_/_/b"
    # \\ 视作 / (Windows 解压工具也认):2 个 `..` 段 → 2 个 `_` 替换
    assert _sanitize_arcname("..\\..\\windows\\system32") == "_/_/windows/system32"
    # 前缀 / 去掉
    assert _sanitize_arcname("/abs/path.jpg") == "abs/path.jpg"
    # 控制字符替换(\x00 / \x07)
    assert _sanitize_arcname("bad\x00name\x07.jpg") == "bad_name_.jpg"
    # 纯 ../ 段(`../` 切出来 = [`..`, `..`, ``])
    assert _sanitize_arcname("../") == "_/_"
    # 单段 .. → _ 兜底
    assert _sanitize_arcname("..") == "_"
    # 正常文件名保持
    assert _sanitize_arcname("media/p.jpg") == "media/p.jpg"


# ============================================================
# 2026-09-02 v1.5.2 PR #B6:ZipExporter 走 `stream_read` 分块流式写入。
# 验证:ZipExporter 内部调 object_store.stream_read 而非 get(内存峰值
# 从 O(file_size) 降到 O(chunk_size))。
# ============================================================


async def test_zip_streaming_large_media_does_not_buffer_full(tmp_path):
    """PR #B6:用 mock stream_read 模拟大媒体分块 yield,验证 ZipExporter
    真按 chunk 写入(不是一次性 read 全量)。"""
    import zipfile

    from tgmonitor.core.dto import MediaDownloadStatus

    storage, objects, bus, _ = await _setup(tmp_path)
    await objects.put("media/abc.jpg", b"placeholder", None)

    from dataclasses import replace

    msg = await storage.get_message(200, 1)
    assert msg is not None and msg.media
    msg.media[0].download_status = MediaDownloadStatus.DONE
    msg.media[0].object_key = "media/abc.jpg"
    msg.media[0].object_backend = "local"
    await storage.save_message(replace(msg, media=msg.media))

    # mock stream_read 模拟 10 chunks × 100 bytes = 1KB 媒体
    # 关键:是 async generator,每次 yield 一个 chunk
    chunk_count = 10
    chunk_size = 100
    total_size = chunk_count * chunk_size
    chunks_received: list[int] = []

    async def mock_stream_read(key: str, chunk_size_arg: int = 65536):
        # 记录每次 yield 的时机(不记录 size,只记录调用次数)
        for i in range(chunk_count):
            chunk = bytes([i % 256]) * chunk_size
            chunks_received.append(len(chunk))
            yield chunk

    objects.stream_read = mock_stream_read  # type: ignore[assignment]

    svc = ExportService(storage, objects, bus)
    out = tmp_path / "chunked.zip"
    req = ExportRequest(
        channel_ids=[200],
        format=ExportFormat.ZIP,
        out_path=str(out),
    )
    async for _ in svc.run(req):
        pass

    # 验证 stream_read 真按 chunk yield(不是一次性返回大 buffer)
    assert len(chunks_received) == chunk_count, (
        f"预期 {chunk_count} chunks,实际 yield {len(chunks_received)}"
    )
    assert all(c == chunk_size for c in chunks_received)

    # 校验 zip 内容字节数 = chunk_count × chunk_size
    with zipfile.ZipFile(out) as zf:
        for n in zf.namelist():
            if "abc.jpg" in n and "thumb" not in n:
                content = zf.read(n)
                assert len(content) == total_size
                # 前 100 字节 = 0x00,后 100 字节 = 0x09
                assert content[0] == 0 and content[99] == 0
                assert content[900] == (9 % 256) or content[900] == 9


# ============================================================
# 2026-09-10 v1.7.3:导出含用户元数据(★ / 标签: / 备注:)— per-format + exclude 验证
# ============================================================


def _make_favorite_message(channel_id: int = 100, msg_id: int = 1) -> MessageDTO:
    """2026-09-10 v1.7.3:构造一条带 ★ + tags + notes 的 message DTO。

    用 `dataclasses.replace` 在 `make_message` 基础上覆盖 metadata 字段;
    默认 fixture 字段都是 v1.7.2 引入的(is_favorite=False, tags=[], notes="")。
    """
    from dataclasses import replace

    base = make_message(channel_id=channel_id, msg_id=msg_id)
    return replace(
        base,
        is_favorite=True,
        tags=["tech", "ai"],
        notes="需要 review",
    )


async def _seed_favorite(storage) -> None:
    """塞一条带 ★ / 标签: / 备注: 的 message 到 storage。"""
    await storage.save_message(_make_favorite_message(100, 1))


# ============================================================
# 2026-11-25 P2-2.1.4 修复批次回归测试(#8/#9/#10/#11/#12/#13)
# ============================================================


# ---- #8:排序键(naive/aware 混比 TypeError + str(id) 字典序错序)----------


def test_message_sort_key_tz_norm_and_int_tiebreak():
    """P2 #8 单元测:`_message_sort_key` naive 归一 UTC + int tie-break。"""
    from datetime import UTC

    from tgmonitor.core.export.service import _message_sort_key

    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    naive = datetime(2026, 1, 1, 12, 0, 0)
    # naive 与 aware 同一刻 → 键相等(旧实现 naive/aware 混比 TypeError)
    m_aware = make_message(channel_id=100, msg_id=1, date=aware)
    m_naive = make_message(channel_id=100, msg_id=2, date=naive)
    assert _message_sort_key(m_aware)[0] == _message_sort_key(m_naive)[0]
    # int tie-break:id 9 < id 10(旧实现 str(id) 字典序 "10" < "9" 错序)
    lo = make_message(channel_id=100, msg_id=1)
    lo.id = 9
    hi = make_message(channel_id=100, msg_id=2)
    hi.id = 10
    assert _message_sort_key(lo) < _message_sort_key(hi)


async def test_export_sort_tiebreak_int_id(tmp_path):
    """P2 #8 端到端:同秒多消息按 int id 排序,不出现 "10"<"9" 字典序错序。"""
    storage, objects, bus, _ = await _setup(tmp_path)
    await storage.upsert_channel(ChannelDTO(id=300, title="C3"))
    base = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(1, 13):  # 同一 date,插入序 = DB 自增 id 序
        await storage.save_message(make_message(channel_id=300, msg_id=i, text=f"m{i}", date=base))
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "sort.json"
    req = ExportRequest(channel_ids=[300], format=ExportFormat.JSON, out_path=str(out))
    async for _ in svc.run(req):
        pass
    import json

    d = json.loads(out.read_text(encoding="utf-8"))
    ids = [m["telegram_msg_id"] for m in d["messages"]]
    assert ids == list(range(1, 13)), f"int tie-break 错序: {ids}"


# ---- #9:分页竞态(导出期间新消息入库 → 重复导出)------------------------


async def test_export_snapshot_boundary_no_dup_no_late(tmp_path):
    """P2 #9:第 1 页取完后插入 5 条新消息,导出结果必须是快照一致性视图
    — 600 条各恰好一次,新消息不入导出(旧 offset 翻页会重复 5 条)。"""
    from datetime import timedelta

    storage, objects, bus, _ = await _setup(tmp_path)
    await storage.upsert_channel(ChannelDTO(id=300, title="C3"))
    base = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(1, 601):  # 600 条 > PAGE_SIZE=500,触发两页
        await storage.save_message(
            make_message(channel_id=300, msg_id=i, text=f"m{i}", date=base + timedelta(minutes=i))
        )
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "race.json"
    req = ExportRequest(channel_ids=[300], format=ExportFormat.JSON, out_path=str(out))
    gen = svc.run(req)
    page = 0
    async for _ in gen:
        page += 1
        if page == 1:
            # 模拟并发写入:第 1 页取完、第 2 页取之前,5 条新消息入库
            for k in range(601, 606):
                await storage.save_message(
                    make_message(
                        channel_id=300,
                        msg_id=k,
                        text=f"late{k}",
                        date=base + timedelta(minutes=2000),
                    )
                )
    import json

    d = json.loads(out.read_text(encoding="utf-8"))
    ids = [m["telegram_msg_id"] for m in d["messages"]]
    assert len(ids) == 600, f"重复导出: {len(ids)} 行(旧实现 605 = 600 + 5 重复)"
    assert len(set(ids)) == len(ids), "出现重复 telegram_msg_id"
    assert set(ids) == set(range(1, 601)), "快照外消息混入或既有消息漏导"


# ---- #10:tags 公式注入(CWE-1236)---------------------------------------


async def test_csv_tags_formula_injection_guarded(tmp_path):
    """P2 #10:CSV tags 列 join 前逐 tag guard(与 notes 同攻击面)。"""
    storage, objects, bus, _ = await _setup(tmp_path)
    await storage.save_message(
        make_message(
            channel_id=100,
            msg_id=9,
            tags=['=HYPERLINK("http://evil")', "+cmd|'/c calc'!A0"],
        )
    )
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "tags.csv"
    req = ExportRequest(channel_ids=[100], format=ExportFormat.CSV, out_path=str(out))
    async for _ in svc.run(req):
        pass
    text = out.read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in text, "tag 未加 guard 前缀"
    assert "'+cmd" in text, "tag 未加 guard 前缀"


async def test_media_csv_tags_formula_injection_guarded(tmp_path):
    """P2 #10:per-media CSV tags 列同样逐 tag guard。"""
    from tgmonitor.core.dto import MediaExportRowDTO
    from tgmonitor.core.export.media_list_csv_exporter import MediaListCsvExporter

    photo = make_photo(channel_id=100, msg_id=1)
    row = MediaExportRowDTO(
        id=photo.id,
        channel_id=100,
        telegram_msg_id=1,
        author=photo.author,
        date=photo.date,
        text=photo.text,
        media=photo.media,
        tags=['=HYPERLINK("http://evil")', "@SUM(1)"],
        media_idx=0,
    )
    out = tmp_path / "mtags.csv"
    await MediaListCsvExporter().render(out, {100: ChannelDTO(id=100, title="T")}, [row])
    text = out.read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in text
    assert "'@SUM" in text


# ---- #11:CSV BOM(Windows Excel 中文乱码)-------------------------------


async def test_csv_exports_have_utf8_bom(tmp_path):
    """P2 #11:两种 CSV 导出文件头都带 UTF-8 BOM。"""
    from tgmonitor.core.dto import MediaExportRequest

    storage, objects, bus, _ = await _setup(tmp_path)
    svc = ExportService(storage, objects, bus)
    out1 = tmp_path / "a.csv"
    async for _ in svc.run(
        ExportRequest(channel_ids=[100, 200], format=ExportFormat.CSV, out_path=str(out1))
    ):
        pass
    out2 = tmp_path / "b.csv"
    async for _ in svc.run(MediaExportRequest(channel_id=200, out_path=str(out2))):
        pass
    assert out1.read_bytes().startswith(b"\xef\xbb\xbf"), "per-message CSV 无 BOM"
    assert out2.read_bytes().startswith(b"\xef\xbb\xbf"), "per-media CSV 无 BOM"


# ---- #12:Enum / datetime 显式序列化 -------------------------------------


def test_json_safe_enum_and_datetime():
    """P2 #12 单元测:`_json_safe` Enum → .value、datetime → isoformat,递归生效。"""
    from datetime import UTC

    from tgmonitor.core.dto import MediaDownloadStatus, MediaType
    from tgmonitor.core.export.json_exporter import _json_safe

    assert _json_safe(MediaType.PHOTO) == "photo"
    assert _json_safe(MediaDownloadStatus.DONE) == "done"
    assert _json_safe(datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)) == "2026-01-01T12:00:00+00:00"
    nested: dict = {"a": [MediaType.VIDEO, {"d": datetime(2026, 1, 1, tzinfo=UTC)}]}
    assert _json_safe(nested) == {"a": ["video", {"d": "2026-01-01T00:00:00+00:00"}]}
    # 非枚举/时间原样透传
    assert _json_safe({"n": 1, "s": "x"}) == {"n": 1, "s": "x"}


async def test_json_export_enum_value_and_isoformat_datetimes(tmp_path):
    """P2 #12 端到端:JSON 枚举字段 = DB 内部值;channel datetime 走 isoformat。

    旧代码 datetime 落 `default=str` → "2026-05-01 08:30:00+00:00"(空格分隔)
    — 本测试在旧代码上失败(created_at 断言)。
    """
    from datetime import UTC

    storage, objects, bus, _ = await _setup(tmp_path)
    await storage.upsert_channel(
        ChannelDTO(id=300, title="C3", created_at=datetime(2026, 5, 1, 8, 30, 0, tzinfo=UTC))
    )
    await storage.save_message(
        make_message(channel_id=300, msg_id=1, date=datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC))
    )
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "e.json"
    req = ExportRequest(channel_ids=[300], format=ExportFormat.JSON, out_path=str(out))
    async for _ in svc.run(req):
        pass
    import json

    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["channels"][0]["created_at"] == "2026-05-01T08:30:00+00:00"
    assert d["messages"][0]["date"] == "2026-05-01T09:00:00+00:00"
    # 枚举 = DB 内部值,不是 "MediaType.PHOTO" repr 形式(向后兼容契约)
    raw = out.read_text(encoding="utf-8")
    assert "MediaType." not in raw
    assert "MediaDownloadStatus." not in raw


async def test_zip_manifest_enum_values(tmp_path):
    """P2 #12:ZIP manifest 枚举 = DB 内部值("done"/"photo")。"""
    import json as json_mod
    import zipfile

    from tgmonitor.core.dto import MediaDownloadStatus

    storage, objects, bus, _ = await _setup(tmp_path)
    await objects.put("media/abc.jpg", b"placeholder", None)
    from dataclasses import replace

    msg = await storage.get_message(200, 1)
    assert msg is not None and msg.media
    msg.media[0].download_status = MediaDownloadStatus.DONE
    msg.media[0].object_key = "media/abc.jpg"
    msg.media[0].object_backend = "local"
    await storage.save_message(replace(msg, media=msg.media))

    svc = ExportService(storage, objects, bus)
    out = tmp_path / "m.zip"
    req = ExportRequest(channel_ids=[200], format=ExportFormat.ZIP, out_path=str(out))
    async for _ in svc.run(req):
        pass
    with zipfile.ZipFile(out) as zf:
        manifest = json_mod.loads(zf.read("_manifest.json").decode("utf-8"))
    target = next(m for m in manifest if m["telegram_msg_id"] == 1)
    assert target["media"][0]["type"] == "photo"
    assert target["media"][0]["download_status"] == "done"


# ---- #13:HTML 缩略图(行 dict 承载 data URI + 批量查询)-------------------


async def test_html_thumbs_row_dict_no_dto_mutation_batch_query(tmp_path):
    """P2 #13:缩略图 data URI 走渲染行 dict — 不污染 MediaDTO;缩略图查询
    每消息一次 `list_thumbnails_for_message`,不再逐 media `get_thumbnail`。"""
    from tgmonitor.core.dto import ThumbnailDTO

    storage, objects, bus, _ = await _setup(tmp_path)
    await objects.put("thumb/t1", b"\xff\xd8fake-jpeg", None)
    await storage.save_thumbnail(
        ThumbnailDTO(
            channel_id=200,
            telegram_msg_id=1,
            media_idx=0,
            object_key="thumb/t1",
            mime_type="image/jpeg",
        )
    )
    calls = {"list": 0, "get": 0}
    orig_list = storage.list_thumbnails_for_message
    orig_get = storage.get_thumbnail

    async def counted_list(cid, mid):
        calls["list"] += 1
        return await orig_list(cid, mid)

    async def counted_get(cid, mid, idx):
        calls["get"] += 1
        return await orig_get(cid, mid, idx)

    storage.list_thumbnails_for_message = counted_list  # type: ignore[method-assign]
    storage.get_thumbnail = counted_get  # type: ignore[method-assign]

    svc = ExportService(storage, objects, bus)
    out = tmp_path / "h.html"
    req = ExportRequest(
        channel_ids=[200], format=ExportFormat.HTML, out_path=str(out), include_thumbnails=True
    )
    async for _ in svc.run(req):
        pass
    html = out.read_text(encoding="utf-8")
    assert 'src="data:image/jpeg;base64,' in html, "缩略图未内嵌"
    assert calls["list"] == 1, f"每消息应一次 list_thumbnails_for_message,实际 {calls['list']}"
    assert calls["get"] == 0, "新实现不应再逐 media 调 get_thumbnail"
    # DTO 不被动态属性污染(旧实现会往 MediaDTO 挂 thumb_data_uri)
    msg = await storage.get_message(200, 1)
    assert msg is not None
    assert not any(hasattr(med, "thumb_data_uri") for med in msg.media), "MediaDTO 被动态属性污染"


async def test_html_thumb_read_failure_logs_and_continues(tmp_path):
    """P2 #13:单张缩略图读取失败 → 该张占位文 + warning 日志,不影响整体。"""
    import logging

    from tgmonitor.core.dto import ThumbnailDTO

    storage, objects, bus, _ = await _setup(tmp_path)
    await storage.save_thumbnail(
        ThumbnailDTO(channel_id=200, telegram_msg_id=1, media_idx=0, object_key="thumb/broken")
    )

    async def broken_get(key):
        raise RuntimeError("object store down")

    objects.get = broken_get  # type: ignore[method-assign]

    svc = ExportService(storage, objects, bus)
    out = tmp_path / "h2.html"
    req = ExportRequest(
        channel_ids=[200], format=ExportFormat.HTML, out_path=str(out), include_thumbnails=True
    )
    # 旧实现 except: pass 静默;新实现 log.warning — 记录 handler 验证
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logging.getLogger("tgmonitor.core.export.html_exporter").addHandler(Capture())
    async for _ in svc.run(req):
        pass
    html = out.read_text(encoding="utf-8")
    assert "data:image" not in html, "失败缩略图不应产生 data URI"
    assert any(r.levelno == logging.WARNING for r in records), "读取失败应记 warning 而非静默"
