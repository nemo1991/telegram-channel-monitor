"""媒体序列化 codec — SQLite / PostgreSQL 两端**唯一**定义点。

2026-11-25 P1-N2:此前 `_MEDIA_SORT_COLUMN` / `_media_status` /
`_status_to_db` 在 `sqlite_repo.py` 与 `_media_pg.py` 逐字双写,语义漂移
风险高(尤其 `'pending'` 历史兼容 — 枚举已删该值,但 DB 列仍 NOT NULL
写该字面量)。本模块收口三处,两端改为 import;PG 侧 facade 与
`_thumbnails_pg` 经 `_media_pg` re-export 继续可用,无需改调用方。

不依赖任何后端驱动(纯 DTO + 常量),core 内可被两端安全复用。
"""

from __future__ import annotations

from tgmonitor.core.dto import MediaDownloadStatus, SortKey

# SortKey → ORDER BY 列(SIZE 可能 NULL → NULLS LAST,与 PG 对齐)。
# value 是 (column, nulls_last) — direction 在拼 ORDER BY 时单独处理。
_MEDIA_SORT_COLUMN: dict[SortKey, tuple[str, bool]] = {
    SortKey.DATE: ("m.date", False),
    SortKey.SIZE: ("me.file_size", True),
    SortKey.STATUS: ("me.download_status", False),
}


def _media_status(value: object | None) -> MediaDownloadStatus | None:
    """反序列化 download_status(None=未下载)。

    历史 DB 里未下载存的是字符串 `'pending'`,枚举已删该值 → 与非法 /
    缺失值一并读成 None,不迁移 schema(NOT NULL 列仍写 'pending')。
    """
    if value is None or str(value) == "pending":
        return None
    try:
        return MediaDownloadStatus(str(value))
    except ValueError:
        return None


def _status_to_db(value: MediaDownloadStatus | None) -> str:
    """序列化 download_status — None(未下载)沿用历史物理值 'pending'。

    DB 列 NOT NULL,不迁移 schema;此处是唯一允许写出 'pending' 字面量
    的地方,读回时由 `_media_status` 转回 None。
    """
    return value.value if value is not None else "pending"
