"""共用 fixtures:InMemoryRepository + LocalObjectStore + FakeTelegramClient。"""
from __future__ import annotations

from datetime import datetime
from typing import AsyncIterator

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
from tgmonitor.core.telegram.fake_client import FakeTelegramClient


class InMemoryRepository(StorageRepository):
    """用于单测的内存仓储(等价语义)。"""

    def __init__(self) -> None:
        self.channels: dict[int, ChannelDTO] = {}
        self.messages: dict[tuple[int, int], MessageDTO] = {}
        self._msg_pk = 0

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def init_schema(self) -> None: ...

    async def upsert_channel(self, channel: ChannelDTO) -> None:
        self.channels[channel.id] = channel

    async def list_channels(self) -> list[ChannelDTO]:
        return list(self.channels.values())

    async def get_channel(self, channel_id: int) -> ChannelDTO | None:
        return self.channels.get(channel_id)

    async def delete_channel(self, channel_id: int) -> None:
        self.channels.pop(channel_id, None)
        for k in [k for k in self.messages if k[0] == channel_id]:
            self.messages.pop(k)

    async def save_message(self, message: MessageDTO) -> int:
        key = (message.channel_id, message.telegram_msg_id)
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
        out.sort(key=lambda m: (m.date, m.id))
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
