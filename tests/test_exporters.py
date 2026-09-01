"""Exporter 快照测试 — JSON / CSV / Markdown / HTML。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tests.conftest import make_message, make_photo
from tgmonitor.core.dto import ChannelDTO, ExportFormat, ExportRequest
from tgmonitor.core.events import EventBus

# noqa: F401 — 这些 import 触发 `@exporter(...)` 类装饰器,把各 Exporter
# 注册到全局 EXPORTERS 注册表。即使模块里没有直接用类名,也得 import。
from tgmonitor.core.export.base import EXPORTERS
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


async def test_export_htmlembeds_thumbnails(tmp_path):
    storage, objects, bus, _ = await _setup(tmp_path)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "out.html"
    req = ExportRequest(
        channel_ids=[200],
        format=ExportFormat.HTML,
        out_path=str(out),
        include_thumbnails=True,
    )
    async for _ in svc.run(req):
        pass
    html = out.read_text(encoding="utf-8")
    # base64 缩略图应被内嵌
    assert "data:image/jpeg;base64," in html


async def test_registry_has_all_six():
    """v1.5.1 PR #B4:加 ZIP 后,registry 应有 6 个 format(原 5 个 + ZIP)。

    早期 `test_registry_has_all_five`(PR #7)已合并到这个更广的断言里 —
    5-format 集合是 6-format 集合的子集,无需独立测试。
    """
    available = EXPORTERS.available()
    assert set(available) == {
        ExportFormat.JSON,
        ExportFormat.CSV,
        ExportFormat.MARKDOWN,
        ExportFormat.HTML,
        ExportFormat.MEDIA_CSV,
        ExportFormat.ZIP,
    }


# ---- 2026-08-25 v1.3.0 PR #7:per-media CSV exporter + dispatcher ----------


async def test_registry_includes_media_csv():
    """PR #7:新 MEDIA_CSV 注册到 EXPORTERS。"""
    available = EXPORTERS.available()
    assert ExportFormat.MEDIA_CSV in available


async def test_media_csv_exporter_snapshot(tmp_path):
    """PR #7:MediaListCsvExporter 写 13 列 + 每条 media 一行,列顺序固定。"""
    import csv as csv_mod

    from tgmonitor.core.dto import MediaDownloadStatus, MediaType

    storage, objects, bus, _ = await _setup(tmp_path)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "media.csv"

    from tgmonitor.core.dto import MediaExportRequest

    req = MediaExportRequest(
        channel_id=None,
        status=None,
        media_type=None,
        search="",
        out_path=str(out),
    )
    async for _ in svc.run(req):
        pass

    assert out.exists()
    text = out.read_text(encoding="utf-8")
    reader = csv_mod.DictReader(text.splitlines())
    rows = list(reader)
    # fixture: ch100 msg1 + ch200 msg1(photo) + ch200 msg2 = 1 photo
    # 没有 message 带 media 的 fixture → make_photo + 2 plain → 1 row
    # 注:原始 _setup 只有 make_photo 1 条带 media
    assert len(rows) == 1
    row = rows[0]
    # 13 列固定顺序
    expected_cols = [
        "channel_id",
        "channel_title",
        "telegram_msg_id",
        "message_date",
        "media_idx",
        "media_type",
        "file_name",
        "file_size",
        "mime_type",
        "download_status",
        "download_error",
        "object_key",
        "object_backend",
    ]
    assert list(row.keys()) == expected_cols
    assert row["channel_id"] == "200"
    assert row["channel_title"] == "Tech"
    assert row["media_type"] == MediaType.PHOTO.value
    assert row["download_status"] == MediaDownloadStatus.PENDING.value


async def test_export_service_run_media_dispatch(tmp_path):
    """PR #7:ExportService.run(MediaExportRequest) → 走 _run_media 分支 →
    ExportDone 事件 payload.message_count 是 media 行数。
    """
    from tgmonitor.core.dto import MediaExportRequest
    from tgmonitor.core.events import ExportDone

    storage, objects, bus, _ = await _setup(tmp_path)
    received: list[ExportDone] = []

    async def _capture(e):
        received.append(e)

    bus.subscribe(ExportDone, _capture)

    svc = ExportService(storage, objects, bus)
    out = tmp_path / "media.csv"
    req = MediaExportRequest(out_path=str(out))
    async for _ in svc.run(req):
        pass

    assert len(received) == 1
    assert received[0].result is not None
    assert received[0].result.out_path == str(out)
    # fixture 1 photo + 2 plain = 1 media row
    assert received[0].result.message_count == 1


async def test_export_service_run_messages_unchanged(tmp_path):
    """PR #7:ExportRequest(老)走 _run_messages 分支 — 向后兼容。"""
    import csv as csv_mod

    storage, objects, bus, _ = await _setup(tmp_path)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "msg.csv"
    req = ExportRequest(
        channel_ids=[100, 200],
        format=ExportFormat.CSV,
        out_path=str(out),
    )
    async for _ in svc.run(req):
        pass
    assert out.exists()
    # header 仍是 per-message schema:含 media_count 列(per-media CSV 没有)
    reader = csv_mod.DictReader(out.read_text(encoding="utf-8").splitlines())
    assert "media_count" in (reader.fieldnames or [])
    assert "media_types" in (reader.fieldnames or [])
    rows = list(reader)
    # fixture 3 messages(1 photo + 2 plain)→ per-message 行数 = 3
    assert len(rows) == 3


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


async def test_export_pagination_500_messages_complete(tmp_path):
    """PR #12:恰好 500 条 → 一次 PAGE_SIZE 拉完,不丢。"""
    import csv as csv_mod

    storage, objects, bus, _ = await _setup(tmp_path)
    await _bulk_seed_messages(storage, 100, 500)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "msg.csv"
    req = ExportRequest(channel_ids=[100], format=ExportFormat.CSV, out_path=str(out))
    async for _ in svc.run(req):
        pass
    rows = list(csv_mod.DictReader(out.read_text(encoding="utf-8").splitlines()))
    assert len(rows) == 500


async def test_export_pagination_501_messages_full(tmp_path):
    """PR #12:501 条 → 第二页拉 1 条,凑齐不丢(老实现只 500)。"""
    import csv as csv_mod

    storage, objects, bus, _ = await _setup(tmp_path)
    await _bulk_seed_messages(storage, 100, 501)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "msg.csv"
    req = ExportRequest(channel_ids=[100], format=ExportFormat.CSV, out_path=str(out))
    async for _ in svc.run(req):
        pass
    rows = list(csv_mod.DictReader(out.read_text(encoding="utf-8").splitlines()))
    # 老 bug 是「拉完一页 break」 → len == 500;新分页必须 == 501。
    assert len(rows) == 501


async def test_export_pagination_1001_messages_full(tmp_path):
    """PR #12:1001 条跨 3 页,sum == 1001。"""
    import csv as csv_mod

    storage, objects, bus, _ = await _setup(tmp_path)
    await _bulk_seed_messages(storage, 100, 1001)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "msg.csv"
    req = ExportRequest(channel_ids=[100], format=ExportFormat.CSV, out_path=str(out))
    async for _ in svc.run(req):
        pass
    rows = list(csv_mod.DictReader(out.read_text(encoding="utf-8").splitlines()))
    assert len(rows) == 1001


async def test_export_pagination_emits_progress_per_batch(tmp_path):
    """PR #12:1001 条 → ExportProgress 至少发 2 次(written=500, 1001)。"""
    storage, objects, bus, _ = await _setup(tmp_path)
    await _bulk_seed_messages(storage, 100, 1001)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "msg.csv"
    req = ExportRequest(channel_ids=[100], format=ExportFormat.CSV, out_path=str(out))

    progress_written: list[int] = []

    async def on_event(event) -> None:
        from tgmonitor.core.events import ExportProgress

        if isinstance(event, ExportProgress):
            progress_written.append(event.written)

    bus.subscribe_all(on_event)

    async for _ in svc.run(req):
        pass
    # 至少出现 500(第一页)和 1001(终态)两个 written
    assert 500 in progress_written
    assert progress_written[-1] == 1001


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


def test_scrub_markdown_heading_injection():
    """PR #17:行首 `## ` / `> ` / `* ` / ``` 转义为 Markdown 字面文本。"""
    from tgmonitor.core.export.guards import _scrub_markdown

    # heading:行首 N 个 `#` 加 `\` 前缀。Markdown 把 `\##` 视为字面文本。
    assert _scrub_markdown("## 假冒公告") == "\\## 假冒公告"
    # 单个 # 也转义
    assert _scrub_markdown("# title") == "\\# title"
    # 6 个 # 也转义
    assert _scrub_markdown("###### deep") == "\\###### deep"
    # blockquote
    assert _scrub_markdown("> quoted") == "\\> quoted"
    # list item
    assert _scrub_markdown("* item") == "\\* item"
    # 普通文本原样
    assert _scrub_markdown("普通文本") == "普通文本"
    # 空字符串安全
    assert _scrub_markdown("") == ""


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


def test_scrub_markdown_image_tracker():
    """PR #17:`![alt](url)` 完整图片语法 → 转义为字面文本。"""
    from tgmonitor.core.export.guards import _scrub_markdown

    out = _scrub_markdown("![tracker](http://evil.example.com/pixel.gif)")
    # 关键:`!` 被转义,Markdown 不再当图片解析
    assert out.startswith("\\!\\")
    # 也确认 URL 没有「裸」出现在 `[` 之后(即未构成图片语法 `![alt](url)`)
    assert "(http://evil.example.com/pixel.gif)" in out  # URL 仍可见


def test_scrub_markdown_multi_line():
    """PR #17:多行文本中每行行首 `#` 都独立转义。"""
    from tgmonitor.core.export.guards import _scrub_markdown

    text = "## 标题\n\n普通段落\n\n### 子标题"
    out = _scrub_markdown(text)
    # 行首 ## 和 ### 都加了 `\`(以反斜杠 + 连续 # 开头)
    assert "\\## 标题" in out
    assert "\\### 子标题" in out
    assert "普通段落" in out


async def test_csv_exporter_guards_formula_prefix(tmp_path):
    """PR #17 端到端:CsvExporter 写出的 file_name / text 以 `=` 开头会被加 `'`。"""
    import csv as csv_mod
    from dataclasses import replace

    from tgmonitor.core.dto import ExportFormat

    storage, objects, bus, _ = await _setup(tmp_path)
    svc = ExportService(storage, objects, bus)
    # fixture 上追加一条带危险文本的 message(make_message 不接受 author,用 replace)
    from tests.conftest import make_message

    base = make_message(
        channel_id=100,
        msg_id=99,
        date=datetime(2026, 1, 5, 12),
    )
    await storage.save_message(
        replace(
            base,
            text="=cmd|'/c calc'!A1",
            author="@SUM(1+1)",
        )
    )
    out = tmp_path / "msg.csv"
    req = ExportRequest(channel_ids=[100, 200], format=ExportFormat.CSV, out_path=str(out))
    async for _ in svc.run(req):
        pass

    rows = list(csv_mod.DictReader(out.read_text(encoding="utf-8").splitlines()))
    # 找到 msg 99 那行
    msg99 = next(r for r in rows if int(r["telegram_msg_id"]) == 99)
    assert msg99["text"].startswith("'"), f"text not guarded: {msg99['text']!r}"
    assert msg99["author"].startswith("'"), f"author not guarded: {msg99['author']!r}"


async def test_markdown_exporter_scrubs_user_text(tmp_path):
    """PR #17 端到端:MarkdownExporter 把 channel title / text 里的 `## ` 转义。"""
    from tgmonitor.core.dto import ExportFormat

    storage, objects, bus, _ = await _setup(tmp_path)
    svc = ExportService(storage, objects, bus)
    # 加一条带 `## ` 文本的 message
    from tests.conftest import make_message

    await storage.save_message(
        make_message(
            channel_id=100,
            msg_id=99,
            text="## 假冒系统公告:请尽快操作",
            date=datetime(2026, 1, 5, 12),
        )
    )
    out = tmp_path / "msg.md"
    req = ExportRequest(channel_ids=[100, 200], format=ExportFormat.MARKDOWN, out_path=str(out))
    async for _ in svc.run(req):
        pass
    content = out.read_text(encoding="utf-8")
    # 原 `## ` 在 message text 里出现必须被转义(不能直接当 heading 渲染)
    # 行首 ## 加 `\` 后:`\## 假冒系统公告:...`(以 `\##` 开头)
    assert "\\## 假冒系统公告" in content, f"## not escaped:\n{content}"


async def test_html_exporter_skips_oversized_thumb(tmp_path):
    """PR #17:thumb bytes > MAX_THUMB_DATA_URI_BYTES → 不内嵌,thumb_data_uri=None。

    模板 fallback 走 `<span class="ph">` 占位文,避免冻死浏览器。
    """
    from tgmonitor.core.dto import ExportFormat
    from tgmonitor.core.export.guards import MAX_THUMB_DATA_URI_BYTES

    storage, objects, bus, _ = await _setup(tmp_path)
    # 换一张 > 256KB 的假缩略图
    big = b"\xff" * (MAX_THUMB_DATA_URI_BYTES + 1024)
    await objects.put("media/abc.jpg.thumb", big, None)
    svc = ExportService(storage, objects, bus)
    out = tmp_path / "msg.html"
    req = ExportRequest(
        channel_ids=[100, 200],
        format=ExportFormat.HTML,
        out_path=str(out),
        include_thumbnails=True,
    )
    async for _ in svc.run(req):
        pass
    content = out.read_text(encoding="utf-8")
    # data URI 不应该出现在输出里(模板走占位 span)
    assert "data:image/jpeg;base64," not in content
    # 占位文出现在(消息详情 span)
    assert "📎" in content


# ---------------------------------------------------------------------------
# 2026-09-01 v1.5.1 PR #B4:ZIP 导出 — 单元级 + dispatcher + Zip Slip 防御
# ---------------------------------------------------------------------------


async def test_registry_includes_zip():
    """PR #B4:ZIP 注册到 EXPORTERS,registry 应包含 `ExportFormat.ZIP`。
    (更广的 6-format 集合断言见 `test_registry_has_all_six`,109 行)"""
    available = EXPORTERS.available()
    assert ExportFormat.ZIP in available


async def test_zip_basic_skips_failed_media(tmp_path):
    """PR #B4:ZipExporter — DONE media 入包,FAILED / PENDING 跳过,
    `_manifest.json` 顶部写全 metadata。"""
    import json as json_mod
    import zipfile

    from tgmonitor.core.dto import MediaDownloadStatus, MediaType

    storage, objects, bus, _ = await _setup(tmp_path)
    # 把 photo msg 的 object_key / status 填为 DONE 并 put bytes
    await objects.put("media/abc.jpg", b"\xff\xd8FAKE_JPEG_BODY", None)

    from dataclasses import replace

    msg = await storage.get_message(200, 1)
    assert msg is not None and msg.media
    msg.media[0].download_status = MediaDownloadStatus.DONE
    msg.media[0].object_key = "media/abc.jpg"
    msg.media[0].object_backend = "local"
    await storage.save_message(replace(msg, media=msg.media))
    # 加一条 FAILED 的 photo(不应进 zip)
    from tgmonitor.core.dto import MediaDTO

    failed_msg = make_message(
        channel_id=200,
        msg_id=2,
        text="失败 media",
    )
    failed_msg.media = [
        MediaDTO(
            type=MediaType.PHOTO,
            file_name="bad.jpg",
            download_status=MediaDownloadStatus.FAILED,
            download_error="disk full",
            object_key="media/missing.jpg",
            object_backend="local",
        )
    ]
    await storage.save_message(failed_msg)

    svc = ExportService(storage, objects, bus)
    out = tmp_path / "out.zip"
    req = ExportRequest(
        channel_ids=[100, 200],
        format=ExportFormat.ZIP,
        out_path=str(out),
    )
    async for _ in svc.run(req):
        pass

    assert out.exists()
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        # DONE 那条 photo 入包 + _manifest.json;FAILED 那条跳过
        assert any(n.endswith("abc.jpg") or n.endswith("pic.jpg") for n in names), names
        assert "_manifest.json" in names
        # FAILED 不入包
        assert not any("bad.jpg" in n or "missing.jpg" in n for n in names), names
        manifest = json_mod.loads(zf.read("_manifest.json"))
        # manifest 含全部 messages 元数据(包括 FAILED 那条的 metadata)
        assert len(manifest) >= 3  # ch100 msg1 + ch200 msg1 + ch200 msg2 (failed photo)
        # verify zip 包里 photo 文件能读回原 bytes
        for n in names:
            if "abc.jpg" in n and "thumb" not in n:
                assert zf.read(n) == b"\xff\xd8FAKE_JPEG_BODY"


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


async def test_zip_with_thumbnails(tmp_path):
    """PR #B4:`include_thumbnails=True` 时同步打包 thumb_<arcname>。"""
    import zipfile

    from tgmonitor.core.dto import MediaDownloadStatus

    storage, objects, bus, _ = await _setup(tmp_path)
    await objects.put("media/abc.jpg", b"\xff\xd8FAKE_JPEG_BODY", None)

    from dataclasses import replace

    msg = await storage.get_message(200, 1)
    assert msg is not None and msg.media
    msg.media[0].download_status = MediaDownloadStatus.DONE
    msg.media[0].object_key = "media/abc.jpg"
    msg.media[0].object_backend = "local"
    await storage.save_message(replace(msg, media=msg.media))

    svc = ExportService(storage, objects, bus)
    out = tmp_path / "out.zip"
    req = ExportRequest(
        channel_ids=[200],
        format=ExportFormat.ZIP,
        out_path=str(out),
        include_thumbnails=True,
    )
    async for _ in svc.run(req):
        pass

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        # thumb 文件存在(_setup 已 put `media/abc.jpg.thumb`)
        thumb_names = [n for n in names if n.startswith("thumb_")]
        assert len(thumb_names) >= 1, names
        # thumb 内容与 put 的 bytes 一致
        for n in thumb_names:
            assert zf.read(n) == b"\xff\xd8\xff\xd9fake-jpeg"


async def test_zip_single_message_dispatch(tmp_path):
    """PR #B4:`ExportRequest.single_message_id` 非 None → dispatcher 走
    `storage.get_message(...)` 拉单条,跳过 list_messages 分页。"""
    import json as json_mod
    import zipfile

    from tgmonitor.core.dto import MediaDownloadStatus

    storage, objects, bus, _ = await _setup(tmp_path)
    await objects.put("media/abc.jpg", b"\xff\xd8FAKE_JPEG_BODY", None)

    from dataclasses import replace

    msg = await storage.get_message(200, 1)
    assert msg is not None and msg.media
    msg.media[0].download_status = MediaDownloadStatus.DONE
    msg.media[0].object_key = "media/abc.jpg"
    msg.media[0].object_backend = "local"
    await storage.save_message(replace(msg, media=msg.media))

    svc = ExportService(storage, objects, bus)
    out = tmp_path / "single.zip"
    req = ExportRequest(
        channel_ids=[200],
        format=ExportFormat.ZIP,
        out_path=str(out),
        single_message_id=1,  # 只取 ch200 msg1
    )
    async for _ in svc.run(req):
        pass

    with zipfile.ZipFile(out) as zf:
        # 单条消息 zip → 只有 1 条 photo 入包,没有 ch100 msg1 / ch200 msg2
        manifest = json_mod.loads(zf.read("_manifest.json"))
        assert len(manifest) == 1
        assert manifest[0]["channel_id"] == 200
        assert manifest[0]["telegram_msg_id"] == 1
