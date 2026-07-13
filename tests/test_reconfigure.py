"""AppService.reconfigure() 单测 — 热重载 storage / objects。"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import InMemoryRepository

from tgmonitor.core.app_service import AppService
from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend, Settings
from tgmonitor.core.events import EventBus, SettingsChanged
from tgmonitor.core.objectstore.local_store import LocalObjectStore
from tgmonitor.core.objectstore.folder_store import FolderObjectStore
from tgmonitor.core.storage.jsonl_store import JsonlFileStore
from tgmonitor.core.telegram.fake_client import FakeTelegramClient


def _settings(tmp: Path, **kw) -> Settings:
    base = dict(
        api_id=1, api_hash="h" * 32, phone="+1",
        session_dir=tmp / "s",
        db_backend=DBBackend.JSONL, db_dsn="", db_root=tmp / "m",
        objectstore_backend=ObjectStoreBackend.FOLDER, objectstore_root=tmp / "o",
        media_policy=MediaPolicy.METADATA, data_root=tmp,
    )
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


async def test_reconfigure_storage_jsonl_to_jsonl(tmp_path: Path):
    # 初始:JSONL + folder
    s1 = _settings(tmp_path)
    s1.ensure_dirs()
    bus = EventBus()
    storage = JsonlFileStore(root=s1.db_root)
    await storage.connect()
    objects = FolderObjectStore(root=s1.objectstore_root)
    await objects.connect()
    client = FakeTelegramClient()
    app = AppService(bus, client, storage, objects, s1)

    # 触发:同样 JSONL 但换目录 → 应触发 storage_changed
    s2 = _settings(tmp_path, db_root=tmp_path / "m2")
    s2.ensure_dirs()
    seen: list[SettingsChanged] = []
    bus.subscribe(SettingsChanged, lambda e: seen.append(e))
    await app.reconfigure(s2)
    assert isinstance(app.storage, JsonlFileStore)
    assert app.storage._root == s2.db_root
    assert seen and seen[0].what == "storage"
    assert seen[0].needs_relogin is False


async def test_reconfigure_objectstore_local_to_folder(tmp_path: Path):
    s1 = _settings(tmp_path, objectstore_backend=ObjectStoreBackend.LOCAL)
    s1.ensure_dirs()
    bus = EventBus()
    storage = InMemoryRepository()
    objects = LocalObjectStore(root=s1.objectstore_root)
    await objects.connect()
    client = FakeTelegramClient()
    app = AppService(bus, client, storage, objects, s1)

    s2 = _settings(tmp_path, objectstore_backend=ObjectStoreBackend.FOLDER)
    s2.ensure_dirs()
    await app.reconfigure(s2)
    assert isinstance(app.objects, FolderObjectStore)


async def test_reconfigure_credentials_triggers_relogin(tmp_path: Path):
    s1 = _settings(tmp_path, api_id=1, api_hash="a" * 32, phone="+1")
    s1.ensure_dirs()
    bus = EventBus()
    app = AppService(bus, FakeTelegramClient(), InMemoryRepository(),
                     LocalObjectStore(root=s1.objectstore_root), s1)
    seen: list[SettingsChanged] = []
    bus.subscribe(SettingsChanged, lambda e: seen.append(e))
    s2 = _settings(tmp_path, api_id=2)
    s2.ensure_dirs()
    await app.reconfigure(s2)
    assert any(e.needs_relogin for e in seen)


async def test_reconfigure_noop_when_unchanged(tmp_path: Path):
    s1 = _settings(tmp_path)
    s1.ensure_dirs()
    bus = EventBus()
    storage = InMemoryRepository()
    objects = LocalObjectStore(root=s1.objectstore_root)
    await objects.connect()
    client = FakeTelegramClient()
    app = AppService(bus, client, storage, objects, s1)
    seen: list[SettingsChanged] = []
    bus.subscribe(SettingsChanged, lambda e: seen.append(e))
    await app.reconfigure(s1)  # 同一份
    assert seen == []
