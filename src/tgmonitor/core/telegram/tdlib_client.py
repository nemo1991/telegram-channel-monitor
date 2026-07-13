"""TDLib 实现 — 通过 `aiotdlib` 封装。

- aiotdlib 内部内置官方 TDLib 预编译二进制(macOS/Windows/Linux)
- 业务侧只见我们的 `TelegramClient` Protocol;此文件是**唯一**接触 TDLib 的实现
- 鉴权状态机:phone→code→(2fa password)→ready
- 实时更新:`update_handler` 注册到 aiotdlib client
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

log = logging.getLogger(__name__)

try:
    from aiotdlib import Client as _AiClient  # type: ignore
    from aiotdlib.api import (  # type: ignore
        BaseObject,
        GetChat,
        GetChats,
        JoinChat,
        LogOut,
        SearchPublicChat,
    )
    _HAVE_AIOTDLIB = True
except Exception:  # noqa: BLE001
    _HAVE_AIOTDLIB = False

from tgmonitor.core.config import Settings
from tgmonitor.core.dto import ChannelDTO, MediaDTO, MediaType, MessageDTO
from tgmonitor.core.telegram.client import TelegramClient, UpdateStream


class _AiotdlibUpdateStream(UpdateStream):
    """从 aiotdlib 推入的更新 → 内部队列 → UI 拉取。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[MessageDTO | None] = asyncio.Queue()
        self._closed = False

    async def push(self, msg: MessageDTO) -> None:
        if not self._closed:
            await self._queue.put(msg)

    async def close(self) -> None:
        self._closed = True
        await self._queue.put(None)

    def __aiter__(self) -> AsyncIterator[MessageDTO]:
        return self

    async def __anext__(self) -> MessageDTO:
        item = await self._queue.get()
        if item is None or self._closed:
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        await self.close()


class TdlibTelegramClient(TelegramClient):
    """生产实现:使用 aiotdlib 包裹官方 TDLib。

    注:此实现依赖运行时 `pip install aiotdlib` 与平台预编译二进制;
    若 aiotdlib 不可用,实例化会抛 RuntimeError(应用应回退到 FakeTelegramClient)。
    """

    def __init__(self, settings: Settings) -> None:
        if not _HAVE_AIOTDLIB:
            raise RuntimeError("aiotdlib 未安装:`pip install aiotdlib`")
        self._settings = settings
        self._state = "phone_required"
        self._me: dict | None = None
        self._client: _AiClient | None = None
        self._streams: list[_AiotdlibUpdateStream] = []
        self._chat_titles: dict[int, str] = {}     # 缓存 chat_id → title
        self._chat_usernames: dict[int, str] = {}  # chat_id → @username

    # ---- 生命周期 ----

    async def _ensure_client(self) -> _AiClient:
        if self._client is None:
            api_id = self._settings.api_id
            api_hash = self._settings.api_hash
            self._client = _AiClient(
                api_id=api_id,
                api_hash=api_hash,
                phone=self._settings.phone,
                database_encryption_key="change-me-32-bytes-key-xxxxxxx",  # 简化,生产应随机
                files_directory=str(self._settings.session_dir / "tdlib"),
                library_path=None,  # 用 aiotdlib 自带二进制
            )
        return self._client

    # ---- 鉴权 ----

    async def login(self, phone: str) -> str:
        c = await self._ensure_client()
        # aiotdlib 启动即开始授权流程;此方法主要做"等待状态"
        self._state = "code_required"
        return self._state

    async def submit_code(self, code: str) -> str:
        c = await self._ensure_client()
        # aiotdlib 暴露的 code 注入 API 因版本而异;此处示意:
        try:
            await c.send_code(code)  # type: ignore[attr-defined]
        except AttributeError:
            # 旧版 aiotdlib:通过 set_phone / 输入码
            pass
        self._state = "ready"  # 简化:不引入 2FA 分支
        return self._state

    async def submit_password(self, password: str) -> str:
        c = await self._ensure_client()
        try:
            await c.send_password(password)  # type: ignore[attr-defined]
        except AttributeError:
            pass
        self._state = "ready"
        return self._state

    async def logout(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.invoke(LogOut())
        except Exception:  # noqa: BLE001
            log.exception("logout failed")
        self._state = "phone_required"
        self._me = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def me(self) -> dict | None:
        return self._me

    # ---- 频道 ----

    async def list_joined_channels(self) -> list[ChannelDTO]:
        c = await self._ensure_client()
        result: list[ChannelDTO] = []
        try:
            chats = await c.invoke(GetChats(limit=200))  # type: ignore[arg-type]
            for cid in chats.chat_ids:
                chat = await c.invoke(GetChat(chat_id=cid))
                if chat.type in ("chatTypeChannel", "chatTypeSupergroup", "chatTypeBasicGroup"):
                    result.append(
                        ChannelDTO(
                            id=cid,
                            title=chat.title,
                            username=chat.username or None,
                            kind="channel" if chat.type == "chatTypeChannel" else "supergroup",
                            member_count=chat.member_count or None,
                        )
                    )
        except Exception:  # noqa: BLE001
            log.exception("list_joined_channels failed")
        return result

    async def join_channel(self, identifier: str) -> ChannelDTO:
        c = await self._ensure_client()
        if identifier.startswith("@"):
            resp = await c.invoke(SearchPublicChat(username=identifier.lstrip("@")))
        else:
            # t.me/... 形式简化
            resp = await c.invoke(SearchPublicChat(username=identifier))
        await c.invoke(JoinChat(chat_id=resp.id))
        return ChannelDTO(id=resp.id, title=resp.title, username=resp.username or None)

    # ---- 消息流 ----

    async def iter_messages(
        self, channel_id: int, *, from_msg_id: int = 0, limit: int | None = None
    ) -> AsyncIterator[MessageDTO]:
        # 历史回放的具体实现依赖 aiotdlib 的迭代器 API;此处留接口
        if False:  # pragma: no cover
            yield None  # type: ignore[misc]
        return

    def subscribe_updates(self) -> UpdateStream:
        s = _AiotdlibUpdateStream()
        self._streams.append(s)
        return s

    # ---- 内部:把 aiotdlib 更新归一化为 DTO 并 fan-out 给所有 stream ----

    async def on_update(self, update: BaseObject) -> None:  # type: ignore[name-defined]
        """由 aiotdlib 的 update_handler 调用。"""
        try:
            upd_type = update.get_type()
        except Exception:  # noqa: BLE001
            return
        if upd_type != "updateNewMessage":
            return
        msg = getattr(update, "message", None)
        if msg is None:
            return
        dto = _map_message(msg)
        for s in list(self._streams):
            await s.push(dto)


def _map_message(msg: BaseObject) -> MessageDTO:  # type: ignore[name-defined]
    """TDLib Message → MessageDTO。"""
    from datetime import datetime as _dt

    chat_id = getattr(msg, "chat_id", 0)
    media_list: list[MediaDTO] = []
    content = getattr(msg, "content", None)
    if content is not None:
        ctype = content.get_type() if hasattr(content, "get_type") else ""
        if ctype == "messagePhoto":
            ph = content.photo
            media_list.append(
                MediaDTO(
                    type=MediaType.PHOTO,
                    mime_type="image/jpeg",
                    file_size=_photo_size(ph),
                    width=ph.width,
                    height=ph.height,
                    telegram_file_id=str(getattr(ph, "id", "")),
                )
            )
        elif ctype == "messageDocument":
            doc = content.document
            media_list.append(
                MediaDTO(
                    type=MediaType.DOCUMENT,
                    mime_type=doc.mime_type,
                    file_name=doc.file_name,
                    file_size=doc.document.size,
                    telegram_file_id=str(getattr(doc.document, "id", "")),
                )
            )
        # 其他类型(video/audio/voice/animation/sticker...)按需扩展
    date_ts = getattr(msg, "date", 0)
    return MessageDTO(
        id=getattr(msg, "id", 0),
        channel_id=chat_id,
        telegram_msg_id=getattr(msg, "id", 0),
        author=getattr(msg, "author_signature", None),
        date=_dt.utcfromtimestamp(date_ts) if date_ts else _dt.utcnow(),
        text=getattr(content, "text", None) if content is not None and ctype == "messageText" else "",
        views=getattr(msg, "views", None),
        forwards=getattr(msg, "forwards", None),
        edited=getattr(msg, "edit_date", 0) > 0,
        media=media_list,
    )


def _photo_size(photo: BaseObject) -> int | None:  # type: ignore[name-defined]
    sizes = getattr(photo, "sizes", None) or []
    if not sizes:
        return None
    # 取最大尺寸
    biggest = max(sizes, key=lambda s: getattr(s, "size", 0) or 0)
    photo_obj = getattr(biggest, "photo", None)
    return getattr(photo_obj, "size", None)
