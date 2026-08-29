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
    }


def _dict_to_channel(d: dict[str, Any]) -> ChannelDTO:
    # 旧 channels.json 缺 is_subscribed / last_synced_at 字段 →
    # 旧库 migration:is_subscribed 默认 True(保留"存即订"语义),
    #               last_synced_at 留空。
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
    )


def _sort_media_rows(
    rows: list[tuple[MessageDTO, int, MediaDTO]],
    sort: SortKey,
    sort_dir: SortDir,
) -> list[tuple[MessageDTO, int, MediaDTO]]:
    """2026-08-25 v1.3.0 PR #6:media row 排序 helper(Jsonl / InMemory 共用)。

    - DATE → `msg.date`,同 date 时 tie-breaker 走 `msg.id DESC, idx ASC`
      与 Postgres / Mongo 默认行为对齐
    - SIZE → `med.file_size or 0`(None 视为 0,排在末尾)
    - STATUS → `med.download_status.value`(枚举字符串字典序,
      `done` < `failed` < `pending` < `downloading`)
    """
    reverse = sort_dir == SortDir.DESC

    def _key(row: tuple[MessageDTO, int, MediaDTO]):
        msg, idx, med = row
        if sort == SortKey.DATE:
            primary = msg.date
            # tie-breaker:(msg.id DESC, idx ASC)— 与 SQL ORDER BY m.id DESC, idx ASC 对齐
            return (primary, -int(msg.id), idx)
        if sort == SortKey.SIZE:
            return (med.file_size or 0,)
        if sort == SortKey.STATUS:
            return (med.download_status.value,)
        # 默认 DATE
        return (msg.date, -int(msg.id), idx)

    return sorted(rows, key=_key, reverse=reverse)


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

    async def ping(self) -> bool:
        """轻量探活:仅查 root 目录是否存在。"""
        return self._root.exists()

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
    ) -> None:
        """2026-08-27 v1.4.0 PR #14:Jsonl 部分更新 — 只动非 None 字段,
        其余保留旧值。is_subscribed 不动(本方法是「真元数据」更新)。"""
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
                    id=message.channel_id, title=f"#{message.channel_id}"
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
    ) -> list[MessageDTO]:
        """按时间升序;两实现必须排序一致(date asc, id asc 兜底)。

        `limit` = 只返回**最近** N 条(取排序尾部,仍按时间升序);损坏行 skip 不抛。
        `offset` (v1.4.0 PR #12):从尾部往前数 offset 条再开始取 limit
        — 例 limit=2 offset=2 倒数 [3,4] 条;offset=0 等同原行为。
        """
        out: list[MessageDTO] = []
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
        """
        ch_ids = (
            channel_ids if channel_ids else [c.id for c in await self.list_subscribed_channels()]
        )
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
        """2026-08-25 PR #3:refcount — 扫订阅 channel jsonl,数同 object_key。"""
        chs = await self.list_subscribed_channels()
        n = 0
        for c in chs:
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
