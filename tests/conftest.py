"""共用 fixtures:InMemoryRepository + LocalObjectStore + FakeTelegramClient。"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import AsyncIterator, Iterator

import pytest
import pytest_asyncio

from tgmonitor.core.app_service import AppService
from tgmonitor.core.config import MediaPolicy, Settings
from tgmonitor.core.dto import ChannelDTO, MediaDTO, MediaType, MessageDTO
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
        self.messages[key] = message
        return message.id

    async def update_message(self, message: MessageDTO) -> None:
        await self.save_message(message)

    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        self.messages.pop((channel_id, telegram_msg_id), None)

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
        return out[:limit] if limit else out

    async def count_messages(self, channel_id: int) -> int:
        return sum(1 for m in self.messages.values() if m.channel_id == channel_id)

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


# ---- aiotdlib stub ---------------------------------------------------
# 背景:`aiotdlib.Client.__init__` 会调 native `td_json_client_create()`,
# stub 仍保留:本地 pyenv/老 Python 上偶发 native 析构挂死,
# 单元测试无需为此 surface area。任何要构造 `TdlibTelegramClient` 的测试都
# 需要这个 stub —
# 它把父类 __init__ 换成 no-op,只塞一些 aiotdlib 期望的内部属性。
# 之前定义在 test_telegram_lifecycle.py;提到 conftest 后
# test_main_window_channels.py / test_live_updates.py 也能复用。
@pytest.fixture
def stub_aiotdlib_init() -> Iterator[None]:
    """把 aiotdlib.Client.__init__ 换成 no-op,跳过 native 加载。"""
    original = tdc._AiClient.__init__

    def _safe_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self._update_task = None
        self._running = False
        self._handlers_tasks = set()
        self._pending_requests = {}
        self._pending_messages = {}
        self._updates_handlers = {}
        self._authorized_event = asyncio.Event()
        self._state = ""
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
        self.settings = kwargs.get("settings") or (args[0] if args else None)

    def _noop_send(*a, **k):
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
