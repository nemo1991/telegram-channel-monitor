"""Exporter 快照测试 — JSON / CSV / Markdown / HTML。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tests.conftest import make_message, make_photo
from tgmonitor.core.dto import ChannelDTO, ExportFormat, ExportRequest, MessageDTO
from tgmonitor.core.events import EventBus

# noqa: F401 — 这些 import 触发 `@exporter(...)` 类装饰器,把各 Exporter
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
