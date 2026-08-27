"""共用 fixtures:InMemoryRepository + LocalObjectStore + FakeTelegramClient。"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, AsyncIterator, Iterator

import pytest
import pytest_asyncio

from tgmonitor.core.app_service import AppService
from tgmonitor.core.config import MediaPolicy, Settings
from tgmonitor.core.dto import (
    ChannelDTO,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    SortDir,
    SortKey,
)
from tgmonitor.core.events import EventBus
from tgmonitor.core.monitor.service import MonitorService
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.objectstore.local_store import LocalObjectStore
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram import tdlib_client as tdc
from tgmonitor.core.telegram.fake_client import FakeTelegramClient


class InMemoryRepository(StorageRepository):
    """用于单测的内存仓储(等价语义)。"""

    def __init__(self) -> None:
        self.channels: dict[int, ChannelDTO] = {}
        self.messages: dict[tuple[int, int], MessageDTO] = {}
        self._msg_pk = 0
        self._meta: dict[str, str] = {}
        # telegram_file_id -> MediaDTO(已 DONE)— find_media_by_file_id 用
        self._media_by_fid: dict[str, MediaDTO] = {}

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def init_schema(self) -> None: ...

    async def upsert_channel(self, channel: ChannelDTO) -> None:
        self.channels[channel.id] = channel

    async def upsert_channel_metadata(self, channel: ChannelDTO) -> None:
        existing = self.channels.get(channel.id)
        self.channels[channel.id] = ChannelDTO(
            id=channel.id, title=channel.title, username=channel.username,
            kind=channel.kind, member_count=channel.member_count,
            created_at=channel.created_at,
            is_subscribed=(existing.is_subscribed if existing else False),
            last_synced_at=channel.last_synced_at,
        )

    async def set_channel_subscribed(
        self, channel_id: int, subscribed: bool
    ) -> None:
        existing = self.channels.get(channel_id)
        if existing is None:
            self.channels[channel_id] = ChannelDTO(
                id=channel_id, title=f"#{channel_id}", is_subscribed=subscribed
            )
        else:
            self.channels[channel_id] = ChannelDTO(
                id=existing.id, title=existing.title, username=existing.username,
                kind=existing.kind, member_count=existing.member_count,
                created_at=existing.created_at,
                is_subscribed=subscribed,
                last_synced_at=existing.last_synced_at,
            )

    async def list_channels(self) -> list[ChannelDTO]:
        return list(self.channels.values())

    async def list_subscribed_channels(self) -> list[ChannelDTO]:
        return [c for c in self.channels.values() if c.is_subscribed]

    async def get_channel(self, channel_id: int) -> ChannelDTO | None:
        return self.channels.get(channel_id)

    async def delete_channel(self, channel_id: int) -> None:
        self.channels.pop(channel_id, None)
        for k in [k for k in self.messages if k[0] == channel_id]:
            self.messages.pop(k)

    async def get_max_telegram_msg_id(self, channel_id: int) -> int | None:
        ids = [mid for (cid, mid) in self.messages if cid == channel_id]
        return max(ids) if ids else None

    async def get_meta(self, key: str) -> str | None:
        return self._meta.get(key)

    async def set_meta(self, key: str, value: str) -> None:
        self._meta[key] = value

    async def save_message(self, message: MessageDTO) -> int:
        key = (message.channel_id, message.telegram_msg_id)
        # 隐式建频道 — 与 jsonl / postgres 行为一致
        if message.channel_id not in self.channels:
            self.channels[message.channel_id] = ChannelDTO(
                id=message.channel_id, title=f"#{message.channel_id}"
            )
        if key in self.messages:
            message.id = self.messages[key].id
        else:
            self._msg_pk += 1
            message.id = self._msg_pk
        # 2026-08-24:re-evaluate 索引 — DONE→PENDING 切换时旧 fid 索引条目该清。
        old_msg = self.messages.get(key)
        old_fids = {m.telegram_file_id for m in (old_msg.media if old_msg else []) if m.telegram_file_id}
        self.messages[key] = message
        new_fids = {m.telegram_file_id for m in message.media if m.telegram_file_id}
        for fid in old_fids | new_fids:
            best = self._find_done_by_fid(fid)
            if best is None:
                self._media_by_fid.pop(fid, None)
            else:
                self._media_by_fid[fid] = best
        return message.id

    def _find_done_by_fid(self, telegram_file_id: str) -> MediaDTO | None:
        """与 JsonlFileStore 同步(2026-08-24):返第一个同 fid 且 DONE+object_key 的 media。"""
        for m in self.messages.values():
            for med in m.media:
                if (med.telegram_file_id == telegram_file_id
                        and med.download_status == MediaDownloadStatus.DONE
                        and med.object_key):
                    return med
        return None

    async def update_message(self, message: MessageDTO) -> None:
        await self.save_message(message)

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        key = (channel_id, telegram_msg_id)
        old = self.messages.pop(key, None)
        if old:
            for med in old.media:
                fid = med.telegram_file_id
                if fid and fid in self._media_by_fid:
                    best = self._find_done_by_fid(fid)
                    if best is None:
                        self._media_by_fid.pop(fid, None)
                    else:
                        self._media_by_fid[fid] = best

    async def get_message(
        self, channel_id: int, telegram_msg_id: int
    ) -> MessageDTO | None:
        return self.messages.get((channel_id, telegram_msg_id))

    async def list_messages(
        self,
        channel_ids: list[int],
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[MessageDTO]:
        out = []
        for m in self.messages.values():
            if m.channel_id not in channel_ids:
                continue
            if date_from and m.date < date_from:
                continue
            if date_to and m.date > date_to:
                continue
            out.append(m)
        # 归一化为 aware UTC 再排序 — 测试 fixture 默认 datetime() 是 naive,
        # dto.py default_factory / tdlib _map_message 现在是 aware UTC,
        # 直接 < 比较会 TypeError。生产代码走的是 Postgres/Mongo/JSONL,
        # 它们各自处理 tzinfo,不在 conftest 范围。
        out.sort(key=lambda m: (m.date if m.date.tzinfo else m.date.replace(tzinfo=UTC), m.id))
        # `limit` = 最近 N 条:取排序尾部,仍按时间升序返回(与各存储后端对齐)。
        # `offset` (v1.4.0 PR #12):从尾部跳过 offset 再取 limit。
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
        return sum(1 for m in self.messages.values() if m.channel_id == channel_id)

    async def find_media_by_file_id(
        self, telegram_file_id: str
    ) -> MediaDTO | None:
        """跨消息去重:任一 prior 已 DONE 的同 file_id media → 返 DTO。"""
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
        """2026-08-25 PR #3:list_media 下沉 — flatten + filter 全在这里。

        顺序按 message.date 升序 / message.id(同 channel + date);offset/limit
        在 filter 后切片。MVP 数据规模 < 50ms,无索引。

        排序(2026-08-25 v1.3.0 PR #6 新增):filter 后整体 sort,key 由
        `sort`/`sort_dir` 决定;tie-breaker `(msg_id DESC, idx ASC)` 与
        Postgres / Mongo 默认行为对齐。
        """
        # 1) 先按 message.date 排序(与 list_messages 一致)
        msgs = await self.list_messages(
            channel_ids if channel_ids else [c.id for c in self.channels.values()],
            limit=None,
        )
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
        rows = self._sort_media_rows(rows, sort, sort_dir)
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
        """2026-08-25 v1.3.0 PR #6:与 list_media 同 filter,但不带 sort/limit/offset,数总数。"""
        msgs = await self.list_messages(
            channel_ids if channel_ids else [c.id for c in self.channels.values()],
            limit=None,
        )
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
        return len(rows)

    def _sort_media_rows(
        self,
        rows: list[tuple[MessageDTO, int, MediaDTO]],
        sort: SortKey,
        sort_dir: SortDir,
    ) -> list[tuple[MessageDTO, int, MediaDTO]]:
        """2026-08-25 v1.3.0 PR #6:InMemory 与 Jsonl 用同一个排序语义 —
        DATE=`msg.date`;SIZE=`med.file_size or 0`;STATUS=`med.download_status.value`;
        tie-breaker `(msg_id DESC, idx ASC)`。

        这里用 method 不复用 Jsonl 的私有 helper 是因为 conftest 是测试
        fixture,跨模块引用私有名比较脆弱。
        """
        reverse = sort_dir == SortDir.DESC

        def _key(row: tuple[MessageDTO, int, MediaDTO]):
            msg, idx, med = row
            if sort == SortKey.DATE:
                return (msg.date, -int(msg.id), idx)
            if sort == SortKey.SIZE:
                return (med.file_size or 0,)
            if sort == SortKey.STATUS:
                return (med.download_status.value,)
            return (msg.date, -int(msg.id), idx)

        return sorted(rows, key=_key, reverse=reverse)

    async def count_media_by_object_key(self, object_key: str) -> int:
        """2026-08-25 PR #3:refcount — 顺序扫 messages 数同 object_key。"""
        n = 0
        for m in self.messages.values():
            for med in m.media:
                if med.object_key == object_key:
                    n += 1
        return n

    async def count_media_by_channel(self, channel_id: int) -> int:
        """2026-08-25 v1.3.0 PR #8:该频道全部 media 数(含 PENDING/FAILED)。"""
        return sum(
            len(m.media) for m in self.messages.values()
            if m.channel_id == channel_id
        )

    async def update_message_interactions(
        self,
        channel_id: int,
        telegram_msg_id: int,
        *,
        views: int | None = None,
        reactions: list[Any] | None = None,
    ) -> None:
        """2026-08-27 v1.4.0 PR #10:InMemory 直持 DTO,字段直接覆盖式赋值。

        views=None 不动,reactions=None 不动(空 list 视作清空)。
        """
        from tgmonitor.core.dto import ReactionDTO

        key = (channel_id, telegram_msg_id)
        msg = self.messages.get(key)
        if msg is None:
            return  # 不存在 idempotent 不抛
        if views is not None:
            msg.views = views
        if reactions is not None:
            msg.reactions = [
                r if isinstance(r, ReactionDTO) else ReactionDTO.from_dict(r)
                for r in reactions
            ]

    async def ping(self) -> bool:
        return True


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(  # type: ignore[call-arg]
        api_id=1,
        api_hash="x" * 32,
        phone="+10000000000",
        session_dir=tmp_path / "session",
        objectstore_root=tmp_path / "media",
        data_root=tmp_path,
        media_policy=MediaPolicy.METADATA,
    )
    s.ensure_dirs()
    return s


@pytest_asyncio.fixture
async def storage() -> InMemoryRepository:
    return InMemoryRepository()


@pytest_asyncio.fixture
async def objectstore(tmp_path) -> ObjectStore:
    s = LocalObjectStore(root=tmp_path / "media")
    await s.connect()
    return s


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def client() -> FakeTelegramClient:
    return FakeTelegramClient()


@pytest_asyncio.fixture
async def monitor(bus, client, storage, objectstore, settings) -> MonitorService:
    return MonitorService(bus, client, storage, objectstore, settings)


@pytest_asyncio.fixture
async def app(bus, client, storage, objectstore, settings) -> AsyncIterator[AppService]:
    svc = AppService(bus, client, storage, objectstore, settings)
    yield svc


def make_message(
    channel_id: int = 100,
    msg_id: int = 1,
    text: str = "hello",
    media: list[MediaDTO] | None = None,
    date: datetime | None = None,
) -> MessageDTO:
    return MessageDTO(
        id=0,
        channel_id=channel_id,
        telegram_msg_id=msg_id,
        date=date or datetime(2026, 1, 1, 12, 0, 0),
        text=text,
        author="alice",
        media=media or [],
    )


def make_photo(channel_id: int = 100, msg_id: int = 1) -> MessageDTO:
    return make_message(
        channel_id=channel_id,
        msg_id=msg_id,
        text="photo!",
        media=[
            MediaDTO(
                type=MediaType.PHOTO,
                mime_type="image/jpeg",
                file_name="pic.jpg",
                file_size=1234,
                width=800,
                height=600,
                thumb_key="media/abc.jpg.thumb",
                thumb_backend="local",
            )
        ],
    )


# ---- tdlib_json stub -------------------------------------------------
# 背景:`tdlib_json.TdlibJsonClient.__init__` 会调 native `TDJsonClient.create()`
# 加载 libtdjson,stub 仍保留:单元测试无需为此 surface area(也避免本机
# 没编译 libtdjson 时无法构造 client)。任何要构造 `TdlibTelegramClient` 的
# 测试都需要这个 stub —
# 它把父类 __init__ 换成 no-op,只塞一些 TdlibJsonClient 期望的内部属性。
# 之前定义在 test_telegram_lifecycle.py;提到 conftest 后
# test_main_window_channels.py / test_live_updates.py 也能复用。
@pytest.fixture
def stub_tdlib_init() -> Iterator[None]:
    """把 tdlib_json.TdlibJsonClient.__init__ 换成 no-op,跳过 native 加载。"""
    original = tdc._AiClient.__init__

    def _safe_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # 与真实 TdlibJsonClient.__init__ 的属性集合对齐 — 注意**不**设
        # `_state`:子类 TdlibTelegramClient 已在 super() 前设好 "uninit",
        # 这里覆盖会丢状态机初值。
        self.settings = kwargs.get("parameters") or (args[0] if args else None)
        self.proxy = kwargs.get("proxy")
        self.library_path = None
        self.logger = logging.getLogger("stub_tdlib_json")
        self._authorized_event = asyncio.Event()
        self._running = False
        self._update_task = None
        self._last_updates_loop_restart = 0.0
        self._handlers_tasks = set()
        self._pending_requests = {}
        self._pending_messages = {}
        self._updates_handlers = {}
        self._middlewares = []
        self._middlewares_handlers = []
        self.tdjson_client = type(
            "StubTd",
            (),
            {
                "receive": _async_iter([]),
                "send": _noop_send,
                "close": _noop_close,
                "execute": _noop_execute,
            },
        )()

    async def _noop_send(*a, **k):
        return None

    async def _noop_close(*a, **k):
        return None

    async def _noop_execute(*a, **k):
        return None

    async def _async_iter(items):
        for x in items:
            yield x

    tdc._AiClient.__init__ = _safe_init  # type: ignore[assignment]
    try:
        yield
    finally:
        tdc._AiClient.__init__ = original  # type: ignore[assignment]
