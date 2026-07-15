"""File-based message store — 每频道一个 .jsonl 文件 + channels.json 频道注册表。

- 文件:`<root>/channels.json` 存所有频道元信息(便于快速列出 / 校验)
- 文件:`<root>/messages/<channel_id>.jsonl` 每行一条消息(append + 内存索引)
- 写策略:追加 + 内存去重,首次访问某频道文件时一次性 load 进内存(`{telegram_msg_id: line_no}`)
- 适用:单机、轻量、可读、git 友好;不适用:TB 级

幂等:`save_message` 用 `(channel_id, telegram_msg_id)` upsert,
实现方式:append 行,内存索引覆盖旧位置(下次落盘时全文件重写 — 见 `_flush`)。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from tgmonitor.core.dto import ChannelDTO, MessageDTO
from tgmonitor.core.storage.repository import StorageRepository

REGISTRY_FILE = "channels.json"
MESSAGES_DIR = "messages"


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
        "edited": m.edited,
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
            }
            for med in m.media
        ],
    }
    if m.raw is not None:
        d["raw"] = m.raw
    return d


def _dict_to_message(d: dict[str, Any]) -> MessageDTO:
    from tgmonitor.core.dto import MediaDTO, MediaType

    media = []
    for md in d.get("media", []):
        try:
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
                )
            )
        except (KeyError, ValueError):
            continue
    return MessageDTO(
        id=int(d.get("id", 0)),
        channel_id=int(d["channel_id"]),
        telegram_msg_id=int(d["telegram_msg_id"]),
        author=d.get("author"),
        date=datetime.fromisoformat(d["date"]) if d.get("date") else datetime.utcnow(),
        text=d.get("text", ""),
        views=d.get("views"),
        forwards=d.get("forwards"),
        reply_to_msg_id=d.get("reply_to_msg_id"),
        edited=bool(d.get("edited", False)),
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
    }


def _dict_to_channel(d: dict[str, Any]) -> ChannelDTO:
    return ChannelDTO(
        id=int(d["id"]),
        title=d["title"],
        username=d.get("username"),
        kind=d.get("kind", "channel"),
        member_count=d.get("member_count"),
        created_at=datetime.fromisoformat(d["created_at"]) if d.get("created_at") else None,
    )


class _ChannelFile:
    """单频道 jsonl 文件的内存索引 + 锁。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        # telegram_msg_id -> 内存行号(0-based)
        self.index: dict[int, int] = {}
        # 内存行:list[dict]
        self.rows: list[dict] = []
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        if not self.path.exists():
            return
        # 文件可能极大,目前一次性 load;后续可改为 mmap
        text = self.path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.rows.append(d)
            mid = int(d.get("telegram_msg_id", 0))
            if mid:
                self.index[mid] = i

    async def upsert(self, msg_dict: dict) -> int:
        async with self._lock:
            mid = int(msg_dict["telegram_msg_id"])
            if mid in self.index:
                # 原地覆盖(行号不变);行长度可能变,后续 flush 全文件重写
                self.rows[self.index[mid]] = msg_dict
            else:
                self.index[mid] = len(self.rows)
                self.rows.append(msg_dict)
            # 同步 id(若调用方分配)
            return int(msg_dict.get("id", mid))

    async def delete(self, telegram_msg_id: int) -> None:
        async with self._lock:
            if telegram_msg_id not in self.index:
                return
            idx = self.index.pop(telegram_msg_id)
            self.rows.pop(idx)
            # 重建 index(行号位移)
            for k, v in list(self.index.items()):
                if v > idx:
                    self.index[k] = v - 1

    async def flush(self) -> None:
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".part")
            with tmp.open("w", encoding="utf-8") as f:
                for r in self.rows:
                    f.write(json.dumps(r, ensure_ascii=False, default=str))
                    f.write("\n")
            tmp.replace(self.path)


class JsonlFileStore(StorageRepository):
    """轻量文件后端,适用于单机与中小数据量。"""

    backend_name = "jsonl"

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._msg_dir = self._root / MESSAGES_DIR
        self._registry = self._root / REGISTRY_FILE
        self._channels: dict[int, ChannelDTO] = {}
        self._files: dict[int, _ChannelFile] = {}
        # 跨 save/delete 串行化(同频道并发安全,跨频道亦有序)
        self._write_lock = asyncio.Lock()
        # 全局自增 message id
        self._next_msg_pk = 1

    # ---- 生命周期 ----

    async def connect(self) -> None:
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
        # 预扫描已有 message id,初始化 _next_msg_pk
        for f in self._msg_dir.glob("*.jsonl"):
            try:
                cid = int(f.stem)
            except ValueError:
                continue
            cf = _ChannelFile(f)
            await cf.load()
            for r in cf.rows:
                if int(r.get("id", 0)) >= self._next_msg_pk:
                    self._next_msg_pk = int(r["id"]) + 1
            self._files[cid] = cf

    async def close(self) -> None:
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
        self._channels[channel.id] = channel
        self._flush_registry()

    async def list_channels(self) -> list[ChannelDTO]:
        return list(self._channels.values())

    async def get_channel(self, channel_id: int) -> ChannelDTO | None:
        return self._channels.get(channel_id)

    async def delete_channel(self, channel_id: int) -> None:
        self._channels.pop(channel_id, None)
        self._flush_registry()
        # 删消息文件
        path = self._msg_dir / f"{channel_id}.jsonl"
        if path.exists():
            path.unlink()
        self._files.pop(channel_id, None)

    # ---- 消息 ----

    async def _file_for(self, channel_id: int) -> _ChannelFile:
        if channel_id not in self._files:
            cf = _ChannelFile(self._msg_dir / f"{channel_id}.jsonl")
            await cf.load()
            self._files[channel_id] = cf
        return self._files[channel_id]

    async def save_message(self, message: MessageDTO) -> int:
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
            d = _message_to_dict(message)
            await cf.upsert(d)
            await cf.flush()
            return message.id  # type: ignore[return-value]

    async def update_message(self, message: MessageDTO) -> None:
        await self.save_message(message)

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        async with self._write_lock:
            cf = await self._file_for(channel_id)
            await cf.delete(telegram_msg_id)
            await cf.flush()

    async def get_message(
        self, channel_id: int, telegram_msg_id: int
    ) -> MessageDTO | None:
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
    ) -> list[MessageDTO]:
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
        return out[:limit] if limit else out

    async def count_messages(self, channel_id: int) -> int:
        cf = await self._file_for(channel_id)
        return len(cf.rows)
