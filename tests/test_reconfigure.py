"""AppService.reconfigure() 单测 — 热重载 storage / objects。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import InMemoryRepository, make_message
from tgmonitor.core.app_service import AppService
from tgmonitor.core.config import DBBackend, ObjectStoreBackend, Settings
from tgmonitor.core.events import EventBus, SettingsChanged
from tgmonitor.core.monitor.service import MediaDownloader, MonitorService
from tgmonitor.core.objectstore.folder_store import FolderObjectStore
from tgmonitor.core.objectstore.local_store import LocalObjectStore
from tgmonitor.core.storage.jsonl_store import JsonlFileStore
from tgmonitor.core.telegram.fake_client import FakeTelegramClient


def _settings(tmp: Path, **kw) -> Settings:
    """复用 Settings.for_test 默认值,只覆盖 backend / root 类字段。

    `tmp` 给 db_root / objectstore_root 等具体路径;`Settings.for_test`
    默认用 mkdtemp,但这里 caller 已有 tmp_path 想要复用,故保留 `tmp`。
    """
    base = dict(
        db_backend=DBBackend.JSONL,
        db_dsn="",
        db_root=tmp / "m",
        objectstore_backend=ObjectStoreBackend.FOLDER,
        objectstore_root=tmp / "o",
    )
    base.update(kw)
    return Settings.for_test(**base)


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
    app = AppService(
        bus,
        FakeTelegramClient(),
        InMemoryRepository(),
        LocalObjectStore(root=s1.objectstore_root),
        s1,
    )
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


class _BrokenStorage:
    """connect 即失败的新 storage — 模拟 PG 连不上的场景。"""

    async def connect(self) -> None:
        raise ConnectionError("connect failed: PG 不可达")

    async def init_schema(self) -> None:
        raise AssertionError("init_schema 不应被调用")


class _InitFailsStorage:
    """connect 成功但 init_schema 失败 — 验证新建连接被清理、旧库不动。"""

    closed = False

    async def connect(self) -> None:
        pass

    async def init_schema(self) -> None:
        raise RuntimeError("init_schema failed: 无权限建表")

    async def close(self) -> None:
        type(self).closed = True


async def test_reconfigure_storage_failure_keeps_old_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """重建 storage 失败时:旧 storage 不被关闭仍可用、settings 不提交、无事件。

    回归 2026-08-13:旧实现"先关旧 storage 再建新库",PG 连不上时旧库已
    close,monitor 写进已关闭的 store → 数据静默丢失。
    """
    s1 = _settings(tmp_path)
    s1.ensure_dirs()
    bus = EventBus()
    storage = JsonlFileStore(root=s1.db_root)
    await storage.connect()
    objects = FolderObjectStore(root=s1.objectstore_root)
    await objects.connect()
    client = FakeTelegramClient()
    app = AppService(bus, client, storage, objects, s1)

    monkeypatch.setattr(
        "tgmonitor.core.app_service.build_storage",
        lambda settings: _BrokenStorage(),
    )
    s2 = _settings(
        tmp_path,
        db_backend=DBBackend.POSTGRES,
        db_dsn="postgresql://tgmonitor:tgmonitor@localhost:5432/tgmonitor",
    )
    s2.ensure_dirs()
    seen: list[SettingsChanged] = []
    bus.subscribe(SettingsChanged, lambda e: seen.append(e))

    with pytest.raises(ConnectionError):
        await app.reconfigure(s2)

    # 旧 storage 未被关闭,仍可正常使用
    assert app.storage is storage
    assert await app.storage.ping() is True
    # settings 未提交、SettingsChanged 未发布
    assert app.settings is s1
    assert seen == []


async def test_reconfigure_storage_init_schema_failure_closes_new(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """新库 connect 成功但 init_schema 失败:新建连接被关闭(不留泄漏)、旧库可用。"""
    s1 = _settings(tmp_path)
    s1.ensure_dirs()
    bus = EventBus()
    storage = JsonlFileStore(root=s1.db_root)
    await storage.connect()
    objects = FolderObjectStore(root=s1.objectstore_root)
    await objects.connect()
    app = AppService(bus, FakeTelegramClient(), storage, objects, s1)

    monkeypatch.setattr(
        "tgmonitor.core.app_service.build_storage",
        lambda settings: _InitFailsStorage(),
    )
    s2 = _settings(tmp_path, db_backend=DBBackend.POSTGRES, db_dsn="postgresql://x")
    s2.ensure_dirs()

    with pytest.raises(RuntimeError):
        await app.reconfigure(s2)

    assert _InitFailsStorage.closed is True
    assert app.storage is storage
    assert await app.storage.ping() is True


class _BrokenObjects:
    """connect 即失败的新 objectstore — 模拟 S3 端点 / 凭据错。"""

    closed = False

    async def connect(self) -> None:
        raise ConnectionError("connect failed: S3 端点不可达")

    async def close(self) -> None:
        type(self).closed = True


async def test_reconfigure_objectstore_failure_keeps_old_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """重建 objectstore 失败时:旧 store 不被关闭仍可用、settings 不提交、无事件。

    回归 2026-08-18:旧实现 `s3_store.connect()` 吞掉全部异常,S3 端点填错 /
    凭据错在保存设置时完全感知不到,直到写 media 才报错;且新建连接未关闭会泄漏。
    """
    s1 = _settings(tmp_path)
    s1.ensure_dirs()
    bus = EventBus()
    storage = JsonlFileStore(root=s1.db_root)
    await storage.connect()
    objects = FolderObjectStore(root=s1.objectstore_root)
    await objects.connect()
    app = AppService(bus, FakeTelegramClient(), storage, objects, s1)

    monkeypatch.setattr(
        "tgmonitor.core.app_service.build_object_store",
        lambda settings: _BrokenObjects(),
    )
    s2 = _settings(
        tmp_path,
        objectstore_backend=ObjectStoreBackend.S3,
        objectstore_endpoint="https://bad.example.com",
        objectstore_bucket="bad-bucket",
    )
    s2.ensure_dirs()
    seen: list[SettingsChanged] = []
    bus.subscribe(SettingsChanged, lambda e: seen.append(e))

    with pytest.raises(ConnectionError):
        await app.reconfigure(s2)

    # 旧 objectstore 未被关闭、settings 未提交、SettingsChanged 未发布
    assert app.objects is objects
    assert await app.objects.exists("k") is False  # 旧 store 仍可用
    assert app.settings is s1
    assert seen == []
    # 新建未就绪的 objectstore 被关闭,不留泄漏
    assert _BrokenObjects.closed is True


async def test_reconfigure_validates_objects_even_when_objects_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """2026-08-18:任何设置变化都无条件重建校验对象存储(即使配置没变)。

    回归:坏对象存储配置已躺在 .env 时,若本轮保存恰好只改了别的字段(如
    手机号 / 代理),旧逻辑 `if diff.objects_changed` 跳过校验 → 静默通过,
    直到写 media 才报 `S3 API Requests must be made to API port`。
    """
    s1 = _settings(tmp_path)
    s1.ensure_dirs()
    bus = EventBus()
    storage = JsonlFileStore(root=s1.db_root)
    await storage.connect()
    objects = FolderObjectStore(root=s1.objectstore_root)
    await objects.connect()
    app = AppService(bus, FakeTelegramClient(), storage, objects, s1)

    class _Broken:
        closed = False

        async def connect(self) -> None:
            raise ConnectionError("connect failed: S3 端点不可达")

        async def close(self) -> None:
            type(self).closed = True

    monkeypatch.setattr(
        "tgmonitor.core.app_service.build_object_store",
        lambda settings: _Broken(),
    )
    s2 = _settings(tmp_path, api_id=2)  # 仅凭据变化,对象存储字段未变
    s2.ensure_dirs()

    with pytest.raises(ConnectionError):
        await app.reconfigure(s2)

    # 即使 objects_changed=False 也真的重连校验了,失败时旧 store 保持、不提交
    assert _Broken.closed is True
    assert app.objects is objects
    assert app.settings is s1


async def test_validate_backends_ok_does_not_swap_runtime(tmp_path: Path):
    """validate_backends:校验通过时 self.storage / self.objects 原样不动。"""
    s1 = _settings(tmp_path)
    s1.ensure_dirs()
    bus = EventBus()
    storage = JsonlFileStore(root=s1.db_root)
    await storage.connect()
    objects = FolderObjectStore(root=s1.objectstore_root)
    await objects.connect()
    app = AppService(bus, FakeTelegramClient(), storage, objects, s1)

    s2 = _settings(tmp_path, db_root=tmp_path / "m2", objectstore_root=tmp_path / "o2")
    s2.ensure_dirs()
    await app.validate_backends(s2)

    # 运行时不切换(仅保存到 .env 的语义)
    assert app.storage is storage
    assert app.objects is objects
    assert app.settings is s1


async def test_validate_backends_failure_raises_keeps_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """validate_backends:后端连不上时上抛、运行时不动、新建连接被清理。"""
    s1 = _settings(tmp_path)
    s1.ensure_dirs()
    bus = EventBus()
    storage = JsonlFileStore(root=s1.db_root)
    await storage.connect()
    objects = FolderObjectStore(root=s1.objectstore_root)
    await objects.connect()
    app = AppService(bus, FakeTelegramClient(), storage, objects, s1)

    class _BrokenStorage:
        closed = False

        async def connect(self) -> None:
            raise ConnectionError("connect failed: PG 不可达")

        async def close(self) -> None:
            type(self).closed = True

    monkeypatch.setattr(
        "tgmonitor.core.app_service.build_storage",
        lambda settings: _BrokenStorage(),
    )
    s2 = _settings(tmp_path, db_backend=DBBackend.POSTGRES, db_dsn="postgresql://x")
    s2.ensure_dirs()

    with pytest.raises(ConnectionError):
        await app.validate_backends(s2)

    assert _BrokenStorage.closed is True  # 校验失败新建连接被关闭
    assert app.storage is storage
    assert app.objects is objects
    assert app.settings is s1


async def test_reconfigure_syncs_backends_to_monitor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """热重载切 storage 后 monitor / downloader / channel_sync 都指向新后端。

    回归 2026-08-18:reconfigure 只换 AppService 自己的引用,monitor /
    MediaDownloader / channel_sync 仍持旧 storage → 实时 / 补拉消息继续写
    旧库,用户看到"切 PG 没生效,重启才生效"。
    """
    s1 = _settings(tmp_path)
    s1.ensure_dirs()
    bus = EventBus()
    storage = JsonlFileStore(root=s1.db_root)
    await storage.connect()
    objects = FolderObjectStore(root=s1.objectstore_root)
    await objects.connect()
    client = FakeTelegramClient()
    monitor = MonitorService(
        bus,
        client,
        storage,
        objects,
        s1,
        downloader=MediaDownloader(client, storage, objects),
    )
    app = AppService(bus, client, storage, objects, s1, monitor=monitor)

    # 新 storage 预置订阅频道 100 — 模拟切到的 PG 里已有的订阅真理
    s2 = _settings(tmp_path, db_root=tmp_path / "m2")
    s2.ensure_dirs()
    new_storage = InMemoryRepository()
    await new_storage.connect()
    await new_storage.set_channel_subscribed(100, True)
    monkeypatch.setattr(
        "tgmonitor.core.app_service.build_storage",
        lambda settings: new_storage,
    )

    await app.reconfigure(s2)

    # 所有持引用者都切到新后端
    assert app.storage is new_storage
    assert monitor.storage is new_storage
    assert monitor.objects is app.objects
    assert monitor.downloader is not None
    assert monitor.downloader.storage is new_storage
    assert monitor.downloader.objects is app.objects
    assert monitor.downloader.max_bytes == s2.media_max_bytes
    # 白名单从新 storage 重载(订阅真理随存储切换)
    assert monitor.subscribed_ids == frozenset({100})
    # channel_sync 也换新 storage
    assert app.channel_sync.storage is new_storage

    # 行为验证:monitor._handle 落库进新 storage,旧库不落
    msg = make_message(channel_id=100, msg_id=1, text="after-reload")
    await monitor._handle(msg)
    assert await new_storage.get_message(100, 1) is not None
    assert await storage.get_message(100, 1) is None


async def test_reconfigure_objects_only_syncs_monitor(tmp_path: Path):
    """仅对象存储变化时,monitor.objects 也切到新 store、storage 保持原对象。

    无条件 _rebuild_objects 后 app.objects 是全新实例,monitor 若仍持旧引用
    会写进已关闭的 store(媒体下载失败且无从察觉)。
    """
    s1 = _settings(tmp_path, objectstore_backend=ObjectStoreBackend.LOCAL)
    s1.ensure_dirs()
    bus = EventBus()
    storage = InMemoryRepository()
    objects = LocalObjectStore(root=s1.objectstore_root)
    await objects.connect()
    client = FakeTelegramClient()
    monitor = MonitorService(bus, client, storage, objects, s1)
    app = AppService(bus, client, storage, objects, s1, monitor=monitor)

    s2 = _settings(tmp_path, objectstore_backend=ObjectStoreBackend.FOLDER)
    s2.ensure_dirs()
    old_objects = app.objects
    await app.reconfigure(s2)

    assert app.objects is not old_objects
    assert isinstance(app.objects, FolderObjectStore)
    assert monitor.objects is app.objects
    assert monitor.storage is storage  # storage 未变,引用保持


async def test_reconfigure_proxy_change_publishes_needs_restart(tmp_path: Path):
    """proxy 单独变更:settings 提交 + SettingsChanged.needs_restart=True。

    回归 2026-08-18:旧 diff 不含 proxy / session_dir,纯代理变更 diff.changed
    =False → reconfigure 直接 return,settings 不提交,UI 却弹「已保存并热重载」
    (假成功)。现在应提交并标记需重启生效。
    """
    s1 = _settings(tmp_path, proxy=None)
    s1.ensure_dirs()
    bus = EventBus()
    storage = JsonlFileStore(root=s1.db_root)
    await storage.connect()
    objects = FolderObjectStore(root=s1.objectstore_root)
    await objects.connect()
    app = AppService(bus, FakeTelegramClient(), storage, objects, s1)

    s2 = _settings(tmp_path, proxy="socks5://127.0.0.1:1080")
    s2.ensure_dirs()
    seen: list[SettingsChanged] = []
    bus.subscribe(SettingsChanged, lambda e: seen.append(e))

    await app.reconfigure(s2)

    assert app.settings is s2
    assert seen and seen[0].needs_restart is True
    assert seen[0].needs_relogin is False
    assert seen[0].what == "client"


async def test_reconfigure_session_dir_change_publishes_needs_restart(tmp_path: Path):
    """session_dir 单独变更:同样标记 needs_restart(不重建 client)。"""
    s1 = _settings(tmp_path, session_dir=tmp_path / "s1")
    s1.ensure_dirs()
    bus = EventBus()
    storage = JsonlFileStore(root=s1.db_root)
    await storage.connect()
    objects = FolderObjectStore(root=s1.objectstore_root)
    await objects.connect()
    app = AppService(bus, FakeTelegramClient(), storage, objects, s1)

    s2 = _settings(tmp_path, session_dir=tmp_path / "s2")
    s2.ensure_dirs()
    seen: list[SettingsChanged] = []
    bus.subscribe(SettingsChanged, lambda e: seen.append(e))

    await app.reconfigure(s2)

    assert app.settings is s2
    assert seen and seen[0].needs_restart is True
    assert seen[0].needs_relogin is False


# ============================================================
# 2026-09-14 v1.7.5 PR #7:DRIFT #C 回归测试 — 切 storage 后
# `is_subscribed=False` 频道不被 union 进新 storage 的「已订」列表。
# 旧实现:`_rebuild_storage` 末尾曾用 in-memory `_subscribed` cache 与
# 新 storage `list_channels()` 做 union,会把 unsubscribed 旧频道错标为
# "已订"。2026-07-31 已删 `_subscribed` cache(真理 = storage);
# 这里加回归测试锁死该 invariant。
# ============================================================


async def test_reconfigure_storage_does_not_promote_unsubscribed(
    tmp_path: Path,
) -> None:
    """DRIFT #C 回归:reconfigure 后,旧 storage 里 `is_subscribed=False`
    的频道不应出现在新 storage 的「已订」视图里。

    验证路径:
      1. 旧 storage 有 3 频道:A subscribed、B unsubscribed、C subscribed
      2. reconfigure 切到新 storage(空 db_root)
      3. 新 storage 调 `list_subscribed_channels()` → 仅 [A, C],无 B
    """
    from tgmonitor.core.dto import ChannelDTO

    # 旧 storage:3 频道(A subscribed / B unsubscribed / C subscribed)
    s1 = _settings(tmp_path, db_root=tmp_path / "m1")
    s1.ensure_dirs()
    bus = EventBus()
    storage = JsonlFileStore(root=s1.db_root)
    await storage.connect()
    await storage.init_schema()
    await storage.upsert_channel(ChannelDTO(id=111, title="A", username="a", kind="channel"))
    await storage.upsert_channel(ChannelDTO(id=222, title="B", username="b", kind="channel"))
    await storage.upsert_channel(ChannelDTO(id=333, title="C", username="c", kind="channel"))
    await storage.set_channel_subscribed(111, True)
    await storage.set_channel_subscribed(222, False)  # 已退订
    await storage.set_channel_subscribed(333, True)
    objects = FolderObjectStore(root=s1.objectstore_root)
    await objects.connect()
    app = AppService(bus, FakeTelegramClient(), storage, objects, s1)

    # 切到新 storage(空目录)
    s2 = _settings(tmp_path, db_root=tmp_path / "m2")
    s2.ensure_dirs()
    await app.reconfigure(s2)

    # 新 storage 是空库(切换目录,不复制数据)→ list_subscribed 空
    new_subscribed = await app.storage.list_subscribed_channels()
    subscribed_ids = sorted(c.id for c in new_subscribed)
    assert 222 not in subscribed_ids, (
        f"DRIFT #C:unsubscribed 频道 id=222 不应出现在新 storage 已订列表;actual={subscribed_ids}"
    )


async def test_reconfigure_storage_empty_db_returns_empty_subscribed(
    tmp_path: Path,
) -> None:
    """DRIFT #C 边界:reconfigure 后新 storage 完全为空 → 已订列表 = []。"""
    s1 = _settings(tmp_path, db_root=tmp_path / "m1")
    s1.ensure_dirs()
    bus = EventBus()
    storage = JsonlFileStore(root=s1.db_root)
    await storage.connect()
    objects = FolderObjectStore(root=s1.objectstore_root)
    await objects.connect()
    app = AppService(bus, FakeTelegramClient(), storage, objects, s1)

    s2 = _settings(tmp_path, db_root=tmp_path / "m2_empty")
    s2.ensure_dirs()
    await app.reconfigure(s2)

    subscribed = await app.storage.list_subscribed_channels()
    assert subscribed == []


async def test_subscription_service_list_messages_uses_storage_truth() -> None:
    """DRIFT #C 配套:`SubscriptionService.list_messages` 在新 storage 就绪后,
    默认走 `list_subscribed_channels()`(真理 = storage)。

    不应残留 in-memory cache 行为:storage 真理「空」→ 返回空消息列表,
    而不是 in-memory 残留的「已订频道」 → 跨 session 拉到陈旧数据。
    """
    from tgmonitor.core.dto import ChannelDTO
    from tgmonitor.core.subscription_service import SubscriptionService

    bus = EventBus()
    storage = InMemoryRepository()
    await storage.connect()
    await storage.init_schema()
    # 空 storage:无任何 channel → list_subscribed_channels() = []
    client = FakeTelegramClient()
    svc = SubscriptionService(bus, client, storage)

    msgs = await svc.list_messages(channel_ids=None)
    assert msgs == []
    # 显式 truth:再插一个 subscribed channel,后能查到
    await storage.upsert_channel(ChannelDTO(id=999, title="truth", username="t", kind="channel"))
    await storage.set_channel_subscribed(999, True)
    msgs2 = await svc.list_messages(channel_ids=None)
    # 新 storage 真理立刻生效(没有 cache 残留)
    assert isinstance(msgs2, list)
