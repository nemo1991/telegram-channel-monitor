"""File-based message store — 每频道一个 .jsonl 文件 + channels.json 频道注册表。

- 文件:`<root>/channels.json` 存所有频道元信息(便于快速列出 / 校验)
- 文件:`<root>/messages/<channel_id>.jsonl` 每行一条消息(append + 内存索引)
- 写策略:追加 + 内存去重,首次访问某频道文件时一次性 load 进内存(`{telegram_msg_id: line_no}`)
- 适用:单机、轻量、可读、git 友好;不适用:TB 级

幂等:`save_message` 用 `(channel_id, telegram_msg_id)` upsert,
实现方式:append 行,内存索引覆盖旧位置(下次落盘时全文件重写 — 见 `_flush`)。

# 子模块切分(2026-08-02)

单频道文件视图抽到 `tgmonitor.core.storage.channel_file.ChannelFile`,
本文件只保留:
- 文件级常量(`REGISTRY_FILE` / `MESSAGES_DIR` / `META_FILE`)
- DTO ↔ dict 转换 helper(`_message_to_dict` / `_dict_to_message` /
  `_channel_to_dict` / `_dict_to_channel`)
- `JsonlFileStore` Repository 主体
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tgmonitor.core.dto import (
    ChannelDTO,
    ChannelStats,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    SortDir,
    SortKey,
)
from tgmonitor.core.storage.channel_file import ChannelFile
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.storage.schema_report import ColumnDrift, SchemaReport

REGISTRY_FILE = "channels.json"
MESSAGES_DIR = "messages"
META_FILE = "meta.json"


def _message_to_dict(m: MessageDTO) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": m.id,
        "channel_id": m.channel_id,
        "telegram_msg_id": m.telegram_msg_id,
        "author": m.author,
        "date": m.date.isoformat() if m.date else None,
        "text": m.text,
        "views": m.views,
        "forwards": m.forwards,
        "reply_to_msg_id": m.reply_to_msg_id,
        # 2026-08-27 v1.4.0 PR #9:4 个新字段(老 jsonl 文件无这些 key,
        # _dict_to_message 默认 None / False 兜底)。
        "forward_origin": m.forward_origin,
        "via_bot_user_id": m.via_bot_user_id,
        "media_album_id": m.media_album_id,
        "is_pinned": m.is_pinned,
        "edited": m.edited,
        # 2026-08-27 v1.4.0 PR #10:reactions 列表 → dict 列表;
        # None 不写 key(老 jsonl 兼容),[] 写空 list(语义:已推送过但当前空)。
        "reactions": ([r.to_dict() for r in m.reactions] if m.reactions is not None else None),
        # 2026-09-09 v1.7.2:用户元数据(老 jsonl 文件无这些 key,读时兜底)。
        "is_favorite": m.is_favorite,
        "tags": m.tags,
        "notes": m.notes,
        "media": [
            {
                "type": med.type.value,
                "mime_type": med.mime_type,
                "file_name": med.file_name,
                "file_size": med.file_size,
                "width": med.width,
                "height": med.height,
                "duration": med.duration,
                "telegram_file_id": med.telegram_file_id,
                "object_key": med.object_key,
                "object_backend": med.object_backend,
                "thumb_key": med.thumb_key,
                "thumb_backend": med.thumb_backend,
                "emoji": med.emoji,
                "download_status": med.download_status.value,
                "download_error": med.download_error,
            }
            for med in m.media
        ],
    }
    if m.raw is not None:
        d["raw"] = m.raw
    return d


def _dict_to_message(d: dict[str, Any]) -> MessageDTO:
    from tgmonitor.core.dto import MediaDTO, MediaType, ReactionDTO

    media = []
    for md in d.get("media", []):
        try:
            try:
                dl_status = MediaDownloadStatus(str(md.get("download_status", "pending")))
            except ValueError:
                # 旧数据 / 非法值回退 pending,不丢整条 media
                dl_status = MediaDownloadStatus.PENDING
            media.append(
                MediaDTO(
                    type=MediaType(md["type"]),
                    mime_type=md.get("mime_type"),
                    file_name=md.get("file_name"),
                    file_size=md.get("file_size"),
                    width=md.get("width"),
                    height=md.get("height"),
                    duration=md.get("duration"),
                    telegram_file_id=md.get("telegram_file_id"),
                    object_key=md.get("object_key"),
                    object_backend=md.get("object_backend"),
                    thumb_key=md.get("thumb_key"),
                    thumb_backend=md.get("thumb_backend"),
                    emoji=md.get("emoji"),
                    download_status=dl_status,
                    download_error=md.get("download_error"),
                )
            )
        except (KeyError, ValueError):
            continue
    return MessageDTO(
        id=int(d.get("id", 0)),
        channel_id=int(d["channel_id"]),
        telegram_msg_id=int(d["telegram_msg_id"]),
        author=d.get("author"),
        date=datetime.fromisoformat(d["date"]) if d.get("date") else datetime.now(UTC),
        text=d.get("text", ""),
        views=d.get("views"),
        forwards=d.get("forwards"),
        reply_to_msg_id=d.get("reply_to_msg_id"),
        edited=bool(d.get("edited", False)),
        # 2026-08-27 v1.4.0 PR #9:4 个新字段 — 老 jsonl 文件没这些 key,
        # 默认 None / False 兜底。
        forward_origin=d.get("forward_origin"),
        via_bot_user_id=d.get("via_bot_user_id"),
        media_album_id=d.get("media_album_id"),
        is_pinned=bool(d.get("is_pinned", False)),
        # 2026-08-27 v1.4.0 PR #10:reactions 读时 None → None(从未推送);
        # list → [ReactionDTO.from_dict(...)]。
        reactions=(
            [ReactionDTO.from_dict(r) for r in d["reactions"]]
            if d.get("reactions") is not None
            else None
        ),
        media=media,
        raw=d.get("raw"),
        # 2026-09-09 v1.7.2:用户元数据 — 旧 jsonl 文件 `.get` 兜底。
        is_favorite=bool(d.get("is_favorite", False)),
        tags=list(d.get("tags") or []),
        notes=d.get("notes") or "",
    )


def _channel_to_dict(c: ChannelDTO) -> dict[str, Any]:
    return {
        "id": c.id,
        "title": c.title,
        "username": c.username,
        "kind": c.kind,
        "member_count": c.member_count,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "is_subscribed": c.is_subscribed,
        "last_synced_at": c.last_synced_at.isoformat() if c.last_synced_at else None,
        # 2026-09-03 v1.6.0 PR #Q2:头像本地路径,None 时省略(老库 roundtrip 干净)
        "photo_local_key": c.photo_local_key,
        # 2026-09-04 v1.6.4:spammer 过滤字段 — 始终写(即使 False 也要显式存,
        # 避免 channel widget 反复触发 partial update)
        "is_verified": c.is_verified,
        "is_scam": c.is_scam,
        "is_fake": c.is_fake,
        "has_protected_content": c.has_protected_content,
    }


def _dict_to_channel(d: dict[str, Any]) -> ChannelDTO:
    # 旧 channels.json 缺 is_subscribed / last_synced_at 字段 →
    # 旧库 migration:is_subscribed 默认 True(保留"存即订"语义),
    #               last_synced_at 留空。
    # 2026-09-03 v1.6.0 PR #Q2:photo_local_key 同样模式 — 旧库无此字段
    # → 默认 None(从未设过头像或被删)。
    # 2026-09-04 v1.6.4:4 个 spammer 过滤字段老库同样无此字段 → 默认 False,
    # UI 端不显示徽标(graceful)。
    return ChannelDTO(
        id=int(d["id"]),
        title=d["title"],
        username=d.get("username"),
        kind=d.get("kind", "channel"),
        member_count=d.get("member_count"),
        created_at=datetime.fromisoformat(d["created_at"]) if d.get("created_at") else None,
        is_subscribed=bool(d.get("is_subscribed", True)),
        last_synced_at=(
            datetime.fromisoformat(d["last_synced_at"]) if d.get("last_synced_at") else None
        ),
        photo_local_key=d.get("photo_local_key"),
        is_verified=bool(d.get("is_verified", False)),
        is_scam=bool(d.get("is_scam", False)),
        is_fake=bool(d.get("is_fake", False)),
        has_protected_content=bool(d.get("has_protected_content", False)),
    )


def _sort_media_rows(
    rows: list[tuple[MessageDTO, int, MediaDTO]],
    sort: SortKey,
    sort_dir: SortDir,
) -> list[tuple[MessageDTO, int, MediaDTO]]:
    """2026-09-26 fix(parity-tiebreak):与 Postgres / Mongo `list_media` tie-break 对齐。

    Postgres / Mongo 的复合 ORDER BY 形如
      ORDER BY <primary> <dir>, m.id DESC, media_idx ASC
    即 primary 方向由 `sort_dir` 决定,但 tie-break 方向是**固定的**
    (msg.id DESC, idx ASC)— 不随 primary 反转。

    Python `sorted` 是稳定排序,所以「先排 tertiary(idx ASC)→ 再排 secondary
    (msg.id DESC)→ 最后排 primary」就等价于 PG 的复合 ORDER BY:稳定排序
    保证先排的次序在后续 tie 中被保留,等同 SQL 的多键 ORDER BY。

    反例(2026-09-26 前):原实现 `sorted(rows, key=_key, reverse=is_desc)`
    把整组方向一并反转,导致 tie-break 也被反转 → Jsonl 1 message + N media
    返 `[2, 1, 0]`,PG 返 `[0, 1, 2]`,集成测试
    `test_list_media_consistent_with_jsonl` 失败。

    - DATE → `msg.date`(主方向由 sort_dir 决定)
    - SIZE → `med.file_size or 0`(None 视为 0)
    - STATUS → `med.download_status.value`(枚举字符串字典序)
    """
    is_desc = sort_dir == SortDir.DESC

    # 1) tertiary:idx ASC(固定方向)— 保证同 msg 内 media 顺序 = 数组顺序
    rows = sorted(rows, key=lambda r: r[1])
    # 2) secondary:msg.id DESC(固定方向)— 跨 msg 时优先新插入的 message
    rows = sorted(rows, key=lambda r: int(r[0].id), reverse=True)
    # 3) primary:方向由 sort_dir 决定;tie 落到 secondary / tertiary
    if sort == SortKey.DATE:
        rows = sorted(rows, key=lambda r: r[0].date, reverse=is_desc)
    elif sort == SortKey.SIZE:
        rows = sorted(rows, key=lambda r: r[2].file_size or 0, reverse=is_desc)
    elif sort == SortKey.STATUS:
        rows = sorted(rows, key=lambda r: r[2].download_status.value, reverse=is_desc)
    else:
        rows = sorted(rows, key=lambda r: r[0].date, reverse=is_desc)
    return rows


class JsonlFileStore(StorageRepository):
    """轻量文件后端,适用于单机与中小数据量。"""

    backend_name = "jsonl"

    def __init__(self, root: Path) -> None:
        """`root` = 仓库根目录(必须由 Settings 算好后传入)。"""
        self._root = Path(root)
        self._msg_dir = self._root / MESSAGES_DIR
        self._registry = self._root / REGISTRY_FILE
        self._meta_path = self._root / META_FILE
        self._channels: dict[int, ChannelDTO] = {}
        self._files: dict[int, ChannelFile] = {}
        # 跨 save/delete 串行化(同频道并发安全,跨频道亦有序)
        self._write_lock = asyncio.Lock()
        # 全局自增 message id
        self._next_msg_pk = 1
        # 全局 meta(key -> str)
        self._meta: dict[str, str] = {}
        # telegram_file_id -> MediaDTO(已 DONE 且 object_key 非 None)— 用于
        # find_media_by_file_id 跨频道去重。ChannelFile 加载时一次性构建。
        self._media_by_fid: dict[str, MediaDTO] = {}

    # ---- 生命周期 ----

    async def connect(self) -> None:
        """建目录 + 加载 channels.json registry + meta + 预扫描 message id 起点。

        损坏的 registry 行 skip(不抛);meta JSON 损坏等同空 meta。
        """
        self._root.mkdir(parents=True, exist_ok=True)
        self._msg_dir.mkdir(parents=True, exist_ok=True)
        # 加载 registry
        if self._registry.exists():
            for line in self._registry.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    c = _dict_to_channel(d)
                    self._channels[c.id] = c
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
        # 加载 meta
        if self._meta_path.exists():
            try:
                self._meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._meta = {}
        # 预扫描已有 message id,初始化 _next_msg_pk
        for f in self._msg_dir.glob("*.jsonl"):
            try:
                cid = int(f.stem)
            except ValueError:
                continue
            cf = ChannelFile(f)
            await cf.load()
            for r in cf.rows:
                if int(r.get("id", 0)) >= self._next_msg_pk:
                    self._next_msg_pk = int(r["id"]) + 1
                # 同步构建 media_by_fid 索引(任一 media 已 DONE 才记)
                for md in r.get("media", []):
                    fid = md.get("telegram_file_id")
                    if not fid:
                        continue
                    if md.get("download_status") != MediaDownloadStatus.DONE.value:
                        continue
                    if not md.get("object_key"):
                        continue
                    # 后写入的优先(upsert 路径)
                    self._media_by_fid[fid] = MediaDTO(
                        type=md["type"],
                        telegram_file_id=fid,
                        object_key=md.get("object_key"),
                        object_backend=md.get("object_backend"),
                        file_size=md.get("file_size"),
                        download_status=MediaDownloadStatus.DONE,
                    )
            self._files[cid] = cf

    async def close(self) -> None:
        """逐个 flush 内存里的 ChannelFile(单个 flush 失败吞掉不挡 close)。"""
        # flush 所有文件
        for cf in self._files.values():
            try:
                await cf.flush()
            except Exception:  # noqa: BLE001
                pass
        self._files.clear()

    async def init_schema(self) -> None:
        """文件后端无需显式 schema;connect() 已建好目录。"""
        return None

    # 2026-09-23 v1.8.x:启动 introspect 用的「期望 int 类型」字段列表。
    # _message_to_dict 顺序与字段名对齐;media 子字段同样进同 schema drift 监测。
    _INT_MSG_FIELDS: tuple[str, ...] = (
        "views",
        "forwards",
        "reply_to_msg_id",
        "via_bot_user_id",
        "media_album_id",
    )
    _INT_MEDIA_FIELDS: tuple[str, ...] = (
        "file_size",
        "width",
        "height",
        "duration",
    )

    async def introspect_schema(self) -> SchemaReport:
        """2026-09-23 v1.8.x:JSONL 无显式 schema,扫已加载消息,检测 nullable
        int 字段是否被存成 str(典型:`'0'` 字符串污染 — 2026-09-23 现网
        asyncpg `'0'` 报错)。

        只诊断不修:JSONL 是用户可读的 git-friendly 文本,自动改磁盘风险大,
        repair 只 log warning,污染行清理留给运维脚本(README 段指引)。
        """
        from tgmonitor.core.storage.schema_report import ColumnDrift, SchemaReport

        bad: list[ColumnDrift] = []
        for cf in self._files.values():
            for r in cf.rows:
                for f in self._INT_MSG_FIELDS:
                    v = r.get(f)
                    if isinstance(v, str):
                        bad.append(ColumnDrift("messages", f, "int", f"str({v!r})"))
                for md in r.get("media", []):
                    for f in self._INT_MEDIA_FIELDS:
                        v = md.get(f)
                        if isinstance(v, str):
                            bad.append(ColumnDrift("media", f, "int", f"str({v!r})"))
        return SchemaReport(wrong_types=bad)

    async def repair_schema(self, report: SchemaReport) -> None:
        """2026-09-23 v1.8.x:JSONL repair 只 log warning,不动磁盘。

        内存 dict 视图会被下次 save_message 自然覆盖(写对的 int),
        旧污染行需要用户手动跑一次性脚本清理(参见 README 段)。
        """
        if not report.wrong_types:
            return
        import logging

        logging.getLogger(__name__).warning(
            "jsonl schema drift: %d 个 int 字段被存成 str(典型:媒体相册 ID "
            "'0' 污染)。已加载内存视图下次写时会自然覆盖,旧污染行需人工跑 "
            "一次性清理脚本(参见 README 段 'JSONL 字段污染清理')。",
            len(report.wrong_types),
        )

    async def ping(self) -> bool:
        """轻量探活:仅查 root 目录是否存在。"""
        return self._root.exists()

    # ---- 用户元数据(2026-09-09 v1.7.2) ----

    async def set_favorite(self, channel_id: int, telegram_msg_id: int, value: bool) -> None:
        """2026-09-09 v1.7.2:单条设 `is_favorite`。走 read-modify-write。"""
        async with self._write_lock:
            msg = await self.get_message(channel_id, telegram_msg_id)
            if msg is None:
                return
            msg.is_favorite = value
            cf = await self._file_for(channel_id)
            idx = cf.index.get(telegram_msg_id)
            if idx is None:
                return
            cf.rows[idx] = _message_to_dict(msg)
            await cf.flush()

    async def set_tags(self, channel_id: int, telegram_msg_id: int, tags: list[str]) -> None:
        """2026-09-09 v1.7.2:单条覆盖式设 `tags`。"""
        async with self._write_lock:
            msg = await self.get_message(channel_id, telegram_msg_id)
            if msg is None:
                return
            msg.tags = list(tags)
            cf = await self._file_for(channel_id)
            idx = cf.index.get(telegram_msg_id)
            if idx is None:
                return
            cf.rows[idx] = _message_to_dict(msg)
            await cf.flush()

    async def set_notes(self, channel_id: int, telegram_msg_id: int, notes: str) -> None:
        """2026-09-09 v1.7.2:单条覆盖式设 `notes`。"""
        async with self._write_lock:
            msg = await self.get_message(channel_id, telegram_msg_id)
            if msg is None:
                return
            msg.notes = notes
            cf = await self._file_for(channel_id)
            idx = cf.index.get(telegram_msg_id)
            if idx is None:
                return
            cf.rows[idx] = _message_to_dict(msg)
            await cf.flush()

    async def list_favorites(self) -> list[MessageDTO]:
        """2026-09-09 v1.7.2:列所有 `is_favorite=True` 消息,按 date DESC。"""
        result: list[MessageDTO] = []
        for _cid, cf in self._files.items():
            for r in cf.rows:
                try:
                    d = _dict_to_message(r)
                except Exception:  # noqa: BLE001
                    continue
                if d.is_favorite:
                    result.append(d)
        result.sort(key=lambda m: m.date, reverse=True)
        return result

    async def list_by_tag(self, tag: str) -> list[MessageDTO]:
        """2026-09-09 v1.7.2:按 `tag` 精确匹配查询,按 date DESC。"""
        result: list[MessageDTO] = []
        for _cid, cf in self._files.items():
            for r in cf.rows:
                try:
                    d = _dict_to_message(r)
                except Exception:  # noqa: BLE001
                    continue
                if tag in d.tags:
                    result.append(d)
        result.sort(key=lambda m: m.date, reverse=True)
        return result

    # ---- 频道 ----

    def _flush_registry(self) -> None:
        tmp = self._registry.with_suffix(".part")
        with tmp.open("w", encoding="utf-8") as f:
            for c in self._channels.values():
                f.write(json.dumps(_channel_to_dict(c), ensure_ascii=False, default=str))
                f.write("\n")
        tmp.replace(self._registry)

    async def upsert_channel(self, channel: ChannelDTO) -> None:
        """全字段覆盖(含 is_subscribed) — 兼容老调用;**新代码走 upsert_channel_metadata**。"""
        self._channels[channel.id] = channel
        self._flush_registry()

    async def upsert_channel_metadata(self, channel: ChannelDTO) -> None:
        """只更元数据字段;is_subscribed 保持旧值。"""
        existing = self._channels.get(channel.id)
        merged = ChannelDTO(
            id=channel.id,
            title=channel.title,
            username=channel.username,
            kind=channel.kind,
            member_count=channel.member_count,
            created_at=channel.created_at,
            is_subscribed=(existing.is_subscribed if existing else False),
            last_synced_at=channel.last_synced_at,
            # 2026-09-03 v1.6.0 PR #Q2:photo_local_key — sync 路径(无 update
            # event 单独推头像)若 caller 给了就走 caller 的,否则保留旧值。
            photo_local_key=(
                channel.photo_local_key
                if channel.photo_local_key is not None
                else (existing.photo_local_key if existing else None)
            ),
            # 2026-09-04 v1.6.4:4 个 spammer 过滤字段 — caller 传了就用,
            # 否则保留旧值(老 sync 路径默认 False)。
            is_verified=(
                channel.is_verified
                if channel.is_verified is not False or existing is None
                else (existing.is_verified if existing else False)
            ),
            is_scam=(
                channel.is_scam
                if channel.is_scam is not False or existing is None
                else (existing.is_scam if existing else False)
            ),
            is_fake=(
                channel.is_fake
                if channel.is_fake is not False or existing is None
                else (existing.is_fake if existing else False)
            ),
            has_protected_content=(
                channel.has_protected_content
                if channel.has_protected_content is not False or existing is None
                else (existing.has_protected_content if existing else False)
            ),
        )
        self._channels[channel.id] = merged
        self._flush_registry()

    async def update_channel_metadata(
        self,
        channel_id: int,
        *,
        title: str | None = None,
        username: str | None = None,
        member_count: int | None = None,
        photo_local_key: str | None = None,
        is_verified: bool | None = None,  # 2026-09-04 v1.6.4
        is_scam: bool | None = None,
        is_fake: bool | None = None,
        has_protected_content: bool | None = None,
    ) -> None:
        """2026-08-27 v1.4.0 PR #14:Jsonl 部分更新 — 只动非 None 字段,
        其余保留旧值。is_subscribed 不动(本方法是「真元数据」更新)。

        2026-09-03 v1.6.0 PR #Q2:加 `photo_local_key` 字段 — TDLib
        `updateChatPhoto` 推本地路径时落库。
        2026-09-04 v1.6.4:加 4 个 spammer 过滤字段。
        """
        existing = self._channels.get(channel_id)
        if existing is None:
            return  # 不存在 idempotent 不抛
        merged = ChannelDTO(
            id=existing.id,
            title=title if title is not None else existing.title,
            username=username if username is not None else existing.username,
            kind=existing.kind,
            member_count=(member_count if member_count is not None else existing.member_count),
            created_at=existing.created_at,
            is_subscribed=existing.is_subscribed,
            last_synced_at=existing.last_synced_at,
            photo_local_key=(
                photo_local_key if photo_local_key is not None else existing.photo_local_key
            ),
            is_verified=is_verified if is_verified is not None else existing.is_verified,
            is_scam=is_scam if is_scam is not None else existing.is_scam,
            is_fake=is_fake if is_fake is not None else existing.is_fake,
            has_protected_content=(
                has_protected_content
                if has_protected_content is not None
                else existing.has_protected_content
            ),
        )
        self._channels[channel_id] = merged
        self._flush_registry()

    async def set_channel_subscribed(self, channel_id: int, subscribed: bool) -> None:
        """只设订阅标志,不动其它字段;频道未建档时用 id 做个 stub(后续会被 sync 补全)。"""
        existing = self._channels.get(channel_id)
        if existing is None:
            # 还没建档 — 用 id 做个 stub,subscribe 路径会很快 upsert 完整信息
            self._channels[channel_id] = ChannelDTO(
                id=channel_id, title=f"#{channel_id}", is_subscribed=subscribed
            )
        else:
            self._channels[channel_id] = ChannelDTO(
                id=existing.id,
                title=existing.title,
                username=existing.username,
                kind=existing.kind,
                member_count=existing.member_count,
                created_at=existing.created_at,
                is_subscribed=subscribed,
                last_synced_at=existing.last_synced_at,
                photo_local_key=existing.photo_local_key,  # 2026-09-03 PR #Q2
                # 2026-09-04 v1.6.4:4 字段透传(原值不动,只切订阅)
                is_verified=existing.is_verified,
                is_scam=existing.is_scam,
                is_fake=existing.is_fake,
                has_protected_content=existing.has_protected_content,
            )
        self._flush_registry()

    async def list_channels(self) -> list[ChannelDTO]:
        """所有频道(含未订阅的);顺序 = 内存 dict 插入序。"""
        return list(self._channels.values())

    async def list_subscribed_channels(self) -> list[ChannelDTO]:
        """只返 is_subscribed=True 的频道;供 MonitorService 喂白名单。"""
        return [c for c in self._channels.values() if c.is_subscribed]

    async def get_channel(self, channel_id: int) -> ChannelDTO | None:
        """单频道;不存在返 None。"""
        return self._channels.get(channel_id)

    async def get_max_telegram_msg_id(self, channel_id: int) -> int | None:
        """续拉历史用 — 该频道已落库的最大 telegram_msg_id;无历史返 None。"""
        cf = self._files.get(channel_id) or await self._file_for(channel_id)
        if not cf.index:
            return None
        return max(cf.index.keys()) if cf.index else None

    async def get_meta(self, key: str) -> str | None:
        """全局单值元数据(sync checkpoint / 上次同步时间等);不存在返 None。"""
        return self._meta.get(key)

    async def set_meta(self, key: str, value: str) -> None:
        """upsert 语义:同步落盘(OSError 吞,内存值仍更新,下次 connect 重读会丢)。"""
        self._meta[key] = value
        # 同步落盘 — meta 量很小(几 KB),每次写都全量 flush。
        try:
            self._meta_path.write_text(json.dumps(self._meta, ensure_ascii=False), encoding="utf-8")
        except OSError:  # noqa: BLE001
            pass  # 内存值已更新,下次 connect() 重读会丢,不致命

    async def delete_channel(self, channel_id: int) -> None:
        """删频道及其 messages/<id>.jsonl;不删对象存储里的二进制。

        注:用户退订**不**应调这个 — 退订走 set_channel_subscribed(False)。
        """
        self._channels.pop(channel_id, None)
        self._flush_registry()
        # 删消息文件
        path = self._msg_dir / f"{channel_id}.jsonl"
        if path.exists():
            path.unlink()
        self._files.pop(channel_id, None)

    # ---- 消息 ----

    async def _file_for(self, channel_id: int) -> ChannelFile:
        if channel_id not in self._files:
            cf = ChannelFile(self._msg_dir / f"{channel_id}.jsonl")
            await cf.load()
            self._files[channel_id] = cf
        return self._files[channel_id]

    async def save_message(self, message: MessageDTO) -> int:
        """幂等 upsert;自动分配 message.id(若未传);返回 DB 内部 id。

        跨 save 串行化(同 / 跨频道),保证 _next_msg_pk 不撞 + flush 顺序。

        2026-08-24:`_media_by_fid` 索引维护改为 re-evaluate 模式(只在 upsert
        路径覆盖 fid 不再 OK,旧实现只 ADD 没 REMOVE — DONE→PENDING 切换时旧
        fid 留在索引里,`find_media_by_file_id` 返 stale entry 让 retry 路径
        的 skip #1 误命中)。新实现:update 前记录 old.media,update 后对
        `old ∪ new` 的所有 fid 扫所有 messages 看是否还有 DONE+object_key 引用;
        没有 → 从 `_media_by_fid` 删;有 → 用最新。MVP 数据规模 O(fids × msgs)
        可接受。
        """
        async with self._write_lock:
            # 确保频道存在
            if message.channel_id not in self._channels:
                self._channels[message.channel_id] = ChannelDTO(
                    id=message.channel_id,
                    title=f"#{message.channel_id}",
                    photo_local_key=None,  # 2026-09-03 PR #Q2:占位 stub 无头像
                )
                self._flush_registry()
            cf = await self._file_for(message.channel_id)
            # 分配 id(若未分配)
            if not message.id:
                message.id = self._next_msg_pk
                self._next_msg_pk += 1
            # 取旧 media(可能有同 fid 的 DONE 项)用于 re-evaluate 索引
            old_msg = await self.get_message(message.channel_id, message.telegram_msg_id)
            old_fids = {
                m.telegram_file_id for m in (old_msg.media if old_msg else []) if m.telegram_file_id
            }
            d = _message_to_dict(message)
            await cf.upsert(d)
            await cf.flush()
            # re-evaluate 索引:对 (old_fids ∪ new_fids) 每个 fid 看 storage
            # 是否还有任何 DONE+object_key 的引用。
            new_fids = {m.telegram_file_id for m in message.media if m.telegram_file_id}
            affected = old_fids | new_fids
            for fid in affected:
                best = self._find_done_by_fid(fid)
                if best is None:
                    self._media_by_fid.pop(fid, None)
                else:
                    self._media_by_fid[fid] = best
            return message.id

    def _find_done_by_fid(self, telegram_file_id: str) -> MediaDTO | None:
        """扫所有 messages(内存中)找第一个同 fid 且 DONE+object_key 的 media。

        多个 message 引用同一 fid(跨消息去重场景)时返最新(DB 中已写入的
        顺序)。MVP 复杂度 O(total messages);后续可下沉到按 channel 索引。
        """
        for cf in self._files.values():
            for row in cf.rows:
                for md in row.get("media", []):
                    if md.get("telegram_file_id") != telegram_file_id:
                        continue
                    if md.get("download_status") != MediaDownloadStatus.DONE.value:
                        continue
                    if not md.get("object_key"):
                        continue
                    return MediaDTO(
                        type=MediaType(md["type"]),
                        telegram_file_id=telegram_file_id,
                        object_key=md.get("object_key"),
                        object_backend=md.get("object_backend"),
                        file_size=md.get("file_size"),
                        download_status=MediaDownloadStatus.DONE,
                    )
        return None

    async def update_message(self, message: MessageDTO) -> None:
        """按 (channel_id, telegram_msg_id) 覆盖式更新(代理到 save_message)。"""
        await self.save_message(message)

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        """删单条消息;不存在不抛。2026-08-24:同步清理 `_media_by_fid` 索引 —
        若该 message 含 fid,删后 storage 无引用,索引条目该清。
        """
        async with self._write_lock:
            old_msg = await self.get_message(channel_id, telegram_msg_id)
            cf = await self._file_for(channel_id)
            await cf.delete(telegram_msg_id)
            await cf.flush()
            if old_msg:
                for med in old_msg.media:
                    fid = med.telegram_file_id
                    if not fid or fid not in self._media_by_fid:
                        continue
                    # 还有其它 message 引用同 fid → 留;否则删
                    best = self._find_done_by_fid(fid)
                    if best is None:
                        self._media_by_fid.pop(fid, None)
                    else:
                        self._media_by_fid[fid] = best

    async def delete_messages(self, channel_id: int, msg_ids: list[int]) -> None:
        """2026-09-08 v1.7.0:批量删单频道 N 条消息。

        一次拿旧 messages(并发 `get_message`),flush 一次,`_media_by_fid`
        在循环外统一收敛 — 比 N 次 delete_message 节省 N-1 次 flush + lock。
        """
        if not msg_ids:
            return
        async with self._write_lock:
            cf = await self._file_for(channel_id)
            # 先取要删的 messages 用来后续清理 _media_by_fid
            old_messages = []
            for mid in msg_ids:
                m = await self.get_message(channel_id, mid)
                if m is not None:
                    old_messages.append(m)
                    await cf.delete(mid)
            await cf.flush()
            # 收集所有要清理的 fid(去重,保留 first occurrences)
            fids: set[str] = set()
            for old_msg in old_messages:
                for med in old_msg.media:
                    if med.telegram_file_id:
                        fids.add(med.telegram_file_id)
            # 一次性收敛 _media_by_fid
            for fid in fids:
                if fid not in self._media_by_fid:
                    continue
                best = self._find_done_by_fid(fid)
                if best is None:
                    self._media_by_fid.pop(fid, None)
                else:
                    self._media_by_fid[fid] = best

    async def update_message_interactions(
        self,
        channel_id: int,
        telegram_msg_id: int,
        *,
        views: int | None = None,
        reactions: list[Any] | None = None,
    ) -> None:
        """2026-08-27 v1.4.0 PR #10:Jsonl 走读 → 改 → 写磁盘更新模式。

        高频 reactions 路径用 read-modify-write 比 SQL UPSERT 慢,但 Jsonl
        文件数与 channel 等量级,顺序读单 channel 极快(几十条消息级)。
        写盘 flush 仍走 `_write_lock` 防并发覆盖。
        """
        from tgmonitor.core.dto import ReactionDTO

        async with self._write_lock:
            msg = await self.get_message(channel_id, telegram_msg_id)
            if msg is None:
                return
            if views is not None:
                msg.views = views
            if reactions is not None:
                msg.reactions = [
                    r if isinstance(r, ReactionDTO) else ReactionDTO.from_dict(r) for r in reactions
                ]
            cf = await self._file_for(channel_id)
            await cf.upsert(_message_to_dict(msg))
            await cf.flush()

    async def update_message_pin(
        self,
        channel_id: int,
        telegram_msg_id: int,
        is_pinned: bool,
    ) -> None:
        """2026-09-10 v1.7.3:Jsonl 读 → 改 → 写磁盘更新模式。

        pin 推送频率不高(用户手动 pin / unpin 或服务端批量事件),
        read-modify-write 成本可接受。`_write_lock` 防并发覆盖。
        """
        async with self._write_lock:
            msg = await self.get_message(channel_id, telegram_msg_id)
            if msg is None:
                return
            msg.is_pinned = is_pinned
            cf = await self._file_for(channel_id)
            await cf.upsert(_message_to_dict(msg))
            await cf.flush()

    async def get_message(self, channel_id: int, telegram_msg_id: int) -> MessageDTO | None:
        """单条消息;不存在返 None。"""
        cf = await self._file_for(channel_id)
        idx = cf.index.get(telegram_msg_id)
        if idx is None:
            return None
        return _dict_to_message(cf.rows[idx])

    async def list_messages(
        self,
        channel_ids: list[int],
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        search: str = "",
        favorite_only: bool = False,
        tag_only: bool = False,
        pinned_only: bool = False,
    ) -> list[MessageDTO]:
        """按时间升序;两实现必须排序一致(date asc, id asc 兜底)。

        `limit` = 只返回**最近** N 条(取排序尾部,仍按时间升序);损坏行 skip 不抛。
        `offset` (v1.4.0 PR #12):从尾部往前数 offset 条再开始取 limit
        — 例 limit=2 offset=2 倒数 [3,4] 条;offset=0 等同原行为。

        `search` (v1.5.1 PR #B2):子串过滤(大小写不敏感),匹配 text 或
        media.file_name 任一;空 = 不过滤。

        2026-09-14 v1.7.5 PR #8:3 个用户元数据过滤 — favorite / tag /
        pinned。AND 语义:任一 True 必须命中。
        """
        out: list[MessageDTO] = []
        search_lo = search.lower() if search else ""
        for cid in channel_ids:
            cf = await self._file_for(cid)
            for r in cf.rows:
                try:
                    d = _dict_to_message(r)
                except Exception:  # noqa: BLE001
                    continue
                if date_from and d.date and d.date < date_from:
                    continue
                if date_to and d.date and d.date > date_to:
                    continue
                if search_lo and not self._matches_search(d, search_lo):
                    continue
                if favorite_only and not d.is_favorite:
                    continue
                if tag_only and not d.tags:
                    continue
                if pinned_only and not d.is_pinned:
                    continue
                out.append(d)
        out.sort(key=lambda m: (m.date or datetime.min, m.id or 0))
        # `limit` = 最近 N 条:取排序尾部(仍按时间升序)。
        if limit is not None and limit > 0:
            if offset > 0:
                # offset 超过数据长度 → 整页空;否则取 [end-limit, end) 区间。
                if offset >= len(out):
                    out = []
                else:
                    end = len(out) - offset
                    start = max(0, end - limit)
                    out = out[start:end]
            else:
                out = out[-limit:]
        return out

    async def count_messages(self, channel_id: int) -> int:
        """该频道已落库消息数;不应用 date 过滤。"""
        cf = await self._file_for(channel_id)
        return len(cf.rows)

    @staticmethod
    def _matches_search(msg: MessageDTO, search_lo: str) -> bool:
        """v1.5.1 PR #B2:消息子串过滤 — text OR 任一 media.file_name。

        `search_lo` 预先 lower(),内层不再重复;`file_name` 可能 None
        (PENDING/FAILED 媒体),`or ""` 兜底避免 AttributeError。
        """
        if search_lo in (msg.text or "").lower():
            return True
        return any(search_lo in (med.file_name or "").lower() for med in msg.media)

    async def aggregate_per_channel(self, channel_ids: list[int]) -> dict[int, ChannelStats]:
        """2026-08-27 v1.4.0 PR #15:Jsonl 实现 — 单轮扫每个 channel 的 jsonl
        文件,聚合 4 字段。N+1 → 1,实际就是 file 维度的 1 次读取。

        与 InMemory 实现区别:不需要 `set_subscribed_channel` 守卫 — Jsonl
        实现直接按 channel_id 扫文件,与 subscription 无关。

        缺失 channel(文件不存在 / 0 行)在返 dict 里**不**包含 — 与 InMemory
        行为一致(契约统一)。
        """
        bucket: dict[int, ChannelStats] = {}
        for cid in channel_ids:
            cf = await self._file_for(cid)
            if not cf.rows:
                continue
            last_date = None
            n_msgs = 0
            n_media = 0
            n_done = 0
            for row in cf.rows:
                n_msgs += 1
                md = row.get("media", [])
                n_media += len(md)
                n_done += sum(
                    1 for x in md if x.get("download_status") == MediaDownloadStatus.DONE.value
                )
                # `row["date"]` 是 ISO str → 解析
                d_raw = row.get("date")
                if d_raw:
                    try:
                        d = datetime.fromisoformat(d_raw)
                        last_date = max(last_date, d) if last_date else d
                    except (ValueError, TypeError):
                        pass
            bucket[cid] = ChannelStats(
                messages=n_msgs,
                media=n_media,
                done_media=n_done,
                last_date=last_date,
            )
        return bucket

    async def find_media_by_file_id(self, telegram_file_id: str) -> MediaDTO | None:
        """跨频道去重:任一先前已 DONE 的同 file_id media → 拷字段复用。

        索引在 `connect()` 加载时 + 每次 `save_message` 时增量更新,O(1) 查。
        """
        return self._media_by_fid.get(telegram_file_id)

    async def list_media(
        self,
        *,
        channel_ids: list[int] | None = None,
        status: MediaDownloadStatus | None = None,
        media_type: MediaType | None = None,
        search: str = "",
        limit: int = 1000,
        offset: int = 0,
        sort: SortKey = SortKey.DATE,
        sort_dir: SortDir = SortDir.DESC,
    ) -> list[tuple[MessageDTO, int, MediaDTO]]:
        """2026-08-25 PR #3:Jsonl 后端的 list_media。

        MVP 不加 per-channel 之外的索引,扫订阅 channel 的 jsonl 文件顺序读
        + 过滤;数据规模万级消息内 < 100ms。Postgres / Mongo 不需要这条路径。

        排序(2026-08-25 v1.3.0 PR #6 新增):filter 后整体 sort,key 由
        `sort`/`sort_dir` 决定;tie-breaker 走 `(msg_id DESC, media_idx ASC)`
        保证稳定。DATE 走 msg.date;SIZE 走 med.file_size(无 file_size 视为 0);
        STATUS 走 med.download_status.value(枚举字符串字典序)。
        """
        rows = await self._filter_media_rows(
            channel_ids=channel_ids,
            status=status,
            media_type=media_type,
            search=search,
        )
        rows = _sort_media_rows(rows, sort, sort_dir)
        if offset:
            rows = rows[offset:]
        return rows[:limit]

    async def count_media(
        self,
        *,
        channel_ids: list[int] | None = None,
        status: MediaDownloadStatus | None = None,
        media_type: MediaType | None = None,
        search: str = "",
    ) -> int:
        """2026-08-25 v1.3.0 PR #6:与 list_media 用同一组 filter 但不带 sort/limit/offset。

        复用 `_filter_media_rows` helper(只过滤不取,Python len 即可)。
        """
        rows = await self._filter_media_rows(
            channel_ids=channel_ids,
            status=status,
            media_type=media_type,
            search=search,
        )
        return len(rows)

    async def _filter_media_rows(
        self,
        *,
        channel_ids: list[int] | None,
        status: MediaDownloadStatus | None,
        media_type: MediaType | None,
        search: str,
    ) -> list[tuple[MessageDTO, int, MediaDTO]]:
        """Jsonl 后端 list_media / count_media 共用的 filter helper(2026-08-25 PR #6)。

        返回未排序、未分页的 `(msg, idx, med)` 列表 — 排序 / 切片由 caller
        处理(`list_media` 走 sort + slice;`count_media` 只数)。

        v1.8.0 patch(2026-09-22):**不再对 `channel_ids=None` 隐式过滤
        订阅状态**。旧逻辑 fallback 到 `list_subscribed_channels()`,但
        Postgres / Mongo 后端的等价实现在 `channel_ids=None` 时不加
        channel 过滤 — Jsonl 与其它后端行为不一致,导致:
        - 用户退订某 channel 后,Media Manager 看不到该 channel 的 media
        - `count_media(channel_ids=None)` 与 `list_media(channel_ids=None)`
          数字对不上(一个看全部,一个只看订阅)
        修法:`channel_ids=None` → 扫 `self._channels` 全部值,与 caller
        显式传 `channel_ids` 才过滤的契约一致。`_filter_media_rows` 的
        contract 本就是「`channel_ids` 是 caller 的过滤,None = 不过滤」。
        """
        ch_ids = [c.id for c in self._channels.values()] if channel_ids is None else channel_ids
        msgs: list[MessageDTO] = []
        for cid in ch_ids:
            cf = await self._file_for(cid)
            for row in cf.rows:
                msgs.append(_dict_to_message(row))
        search_lo = search.lower()
        rows: list[tuple[MessageDTO, int, MediaDTO]] = []
        for msg in msgs:
            for idx, med in enumerate(msg.media):
                if status is not None and med.download_status != status:
                    continue
                if media_type is not None and med.type != media_type:
                    continue
                if search_lo and search_lo not in (med.file_name or "").lower():
                    continue
                rows.append((msg, idx, med))
        return rows

    async def count_media_by_object_key(self, object_key: str) -> int:
        """2026-08-25 PR #3:refcount — 扫所有频道 jsonl,数同 object_key。

        v1.8.0 patch(2026-09-22):**不再过滤订阅状态**。旧实现走
        `list_subscribed_channels()` 只数 `is_subscribed=True` 的频道,
        但 Postgres / Mongo 后端的等价实现(`SELECT count(*) FROM media
        WHERE object_key = $1` / `$unwind + $match`)是**全表**扫,与订阅
        无关 — Jsonl 与其它后端行为不一致。

        实际后果(媒体下载/上传走对象存储的批处理):用户退订某 channel 后,
        该 channel 的 `message.media[i].object_key` 仍在库中,但
        `count_media_by_object_key` 把它当返 0;后续任何一处 `delete_media`
        命中同 key,误判 refcount=0 → `objects.delete(key)` 删 bytes →
        原 channel 的 message.media 仍引用该 key → 数据丢失 + 重新订阅
        看到空媒体。`reconcile_orphans` 同样路径,会把"仍被引用"的 bytes
        误纳为孤儿,prune 时一并删。

        修法:扫 `self._channels` 的**全部**值(订阅 + 退订 + 卸载),与
        `_filter_media_rows` 用 `channel_ids` 透传的契约一致 — caller
        显式传 `channel_ids` 才过滤,不传 = 不过滤。
        """
        n = 0
        for c in self._channels.values():
            cf = await self._file_for(c.id)
            for row in cf.rows:
                # `cf.rows` 是 dict;先转 MessageDTO 再扫 media 数组,
                # 与 `list_media` 路径一致。
                msg = _dict_to_message(row)
                for med in msg.media:
                    if med.object_key == object_key:
                        n += 1
        return n

    async def count_media_by_channel(self, channel_id: int) -> int:
        """2026-08-25 v1.3.0 PR #8:该频道全部 media 数(含 PENDING/FAILED)。

        直接扫该 channel 的 jsonl 文件,累加每条 message 的 `media` 长度;
        不需订阅标志(预览不区分订阅与否)。
        """
        cf = await self._file_for(channel_id)
        return sum(len(row.get("media", [])) for row in cf.rows)
