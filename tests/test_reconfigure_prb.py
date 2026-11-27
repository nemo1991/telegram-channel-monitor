"""ADR-002 PR-B:reconfigure 三段式准原子 + 并发守卫 + UI 文案分支。

覆盖:
- 重入拒绝:预备段 await 点二次进入 → 专用异常、零副作用、首次调用正常完成
- 预备段构造失败:新服务构造抛错 → 新后端关闭、运行时零变更
- 提交段失败:update_backends 抛错 → app 级引用一个都没动
- 收尾段失败:publish 抛错 → 不向上传播、旧后端仍被关闭
- 终态一致:新域服务/downloader/settings 全部指向新实例
- UI:重入异常走「正在应用」文案,不误报「后端校验未通过」;按钮恢复
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import InMemoryRepository
from tgmonitor.core.app_service import AppService, ReconfigureInProgressError
from tgmonitor.core.config import DBBackend, ObjectStoreBackend, Settings
from tgmonitor.core.events import EventBus, SettingsChanged
from tgmonitor.core.monitor.service import MediaDownloader, MonitorService
from tgmonitor.core.objectstore.folder_store import FolderObjectStore
from tgmonitor.core.telegram.fake_client import FakeTelegramClient


def _settings(tmp: Path, **kw) -> Settings:
    base = dict(
        db_backend=DBBackend.POSTGRES,
        db_dsn="postgresql://test:test@localhost:5432/test",
        db_root=tmp / "m",
        objectstore_backend=ObjectStoreBackend.FOLDER,
        objectstore_root=tmp / "o",
    )
    base.update(kw)
    return Settings.for_test(**base)


def _make_app(tmp: Path, monkeypatch: pytest.MonkeyPatch):
    """造一套真实 app + monitor,storage 构建可替换成新 InMemoryRepository。"""
    s1 = _settings(tmp)
    s1.ensure_dirs()
    bus = EventBus()
    storage = InMemoryRepository()
    objects = FolderObjectStore(root=s1.objectstore_root)
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
    return app, monitor, bus, storage, objects, s1


# ---------- 1) 重入拒绝 ----------


async def test_reconfigure_rejects_reentry_at_prep_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预备段 await 点二次进入 → 专用异常,零副作用;首次调用仍完成。

    UI 双击「应用」的真实竞态:两个协程都在 `_build_storage` 的 await 点
    交错。守卫必须在任何赋值前拒绝第二次,否则它会把首次刚建好的后端
    close 掉。
    """
    app, monitor, bus, old_storage, old_objects, s1 = _make_app(tmp_path, monkeypatch)
    new_storage = InMemoryRepository()
    await new_storage.connect()
    monkeypatch.setattr(
        "tgmonitor.core.app_service._reconfigure.build_storage",
        lambda settings: new_storage,
    )
    # 用两个 Event 卡住 _build_storage:entered 通知「已在 await 点」,
    # release 由测试放行 —— 避免忙等轮询。
    import asyncio

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _slow_build(new_settings):
        entered.set()
        await release.wait()
        return new_storage

    monkeypatch.setattr("tgmonitor.core.app_service._reconfigure._build_storage", _slow_build)

    s2 = _settings(tmp_path, db_dsn="postgresql://new/db")
    s2.ensure_dirs()
    seen: list[SettingsChanged] = []
    bus.subscribe(SettingsChanged, lambda e: seen.append(e))

    first = asyncio.create_task(app.reconfigure(s2))
    await entered.wait()

    # 第二次在预备段 await 点进入 → 立即拒绝
    with pytest.raises(ReconfigureInProgressError, match="正在应用"):
        await app.reconfigure(s2)

    # 首次调用未被污染:零提前副作用
    assert app.storage is old_storage
    assert app.settings is s1
    assert app._reconfiguring is True

    release.set()
    await first

    # 首次正常完成;第二次没有留下任何痕迹
    assert app.storage is new_storage
    assert app.settings is s2
    assert app._reconfiguring is False
    assert len(seen) == 1


async def test_reconfiguring_flag_reset_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """构建失败后标志必须复位,否则后续所有 reconfigure 都被永久拒绝。"""
    app, monitor, bus, old_storage, old_objects, s1 = _make_app(tmp_path, monkeypatch)

    async def _boom(settings):
        raise ConnectionError("db down")

    monkeypatch.setattr("tgmonitor.core.app_service._reconfigure._build_storage", _boom)
    s2 = _settings(tmp_path, db_dsn="postgresql://new/db")
    s2.ensure_dirs()

    with pytest.raises(ConnectionError):
        await app.reconfigure(s2)
    assert app._reconfiguring is False

    # 标志已复位 —— 不再抛重入异常(而是再次抛原始构建错误)
    with pytest.raises(ConnectionError):
        await app.reconfigure(s2)


# ---------- 2) 预备段构造失败 ----------


async def test_prep_construction_failure_closes_new_and_keeps_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预备段里新服务构造抛错 → 新后端关闭、旧运行时零变更、不 publish。"""
    app, monitor, bus, old_storage, old_objects, s1 = _make_app(tmp_path, monkeypatch)
    new_storage = InMemoryRepository()
    await new_storage.connect()
    new_storage_closes: list[int] = []
    _orig_close = new_storage.close

    async def _spy_close() -> None:
        new_storage_closes.append(1)
        await _orig_close()

    new_storage.close = _spy_close  # type: ignore[method-assign]
    monkeypatch.setattr(
        "tgmonitor.core.app_service._reconfigure.build_storage",
        lambda settings: new_storage,
    )

    # ChannelSyncService 构造抛错(预备段成员,发生在两个 await 之后)
    monkeypatch.setattr(
        "tgmonitor.core.channel_sync.ChannelSyncService",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("ctor boom")),
    )

    s2 = _settings(tmp_path, db_dsn="postgresql://new/db")
    s2.ensure_dirs()
    seen: list[SettingsChanged] = []
    bus.subscribe(SettingsChanged, lambda e: seen.append(e))

    with pytest.raises(RuntimeError, match="ctor boom"):
        await app.reconfigure(s2)

    # 零副作用:旧后端仍可用、settings 未提交、无事件、新后端已关闭
    assert app.storage is old_storage
    assert app.objects is old_objects
    assert app.settings is s1
    assert app.channel_sync.storage is old_storage
    assert new_storage_closes == [1]
    assert seen == []


# ---------- 3) 提交段失败 ----------


async def test_commit_failure_leaves_app_refs_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """update_backends 抛错 → app 级引用一个都没动(准原子核心保证)。"""
    app, monitor, bus, old_storage, old_objects, s1 = _make_app(tmp_path, monkeypatch)
    new_storage = InMemoryRepository()
    await new_storage.connect()
    monkeypatch.setattr(
        "tgmonitor.core.app_service._reconfigure.build_storage",
        lambda settings: new_storage,
    )

    async def _boom(*args, **kwargs):
        raise RuntimeError("update_backends boom")

    monkeypatch.setattr(monitor, "update_backends", _boom)
    s2 = _settings(tmp_path, db_dsn="postgresql://new/db")
    s2.ensure_dirs()
    seen: list[SettingsChanged] = []
    bus.subscribe(SettingsChanged, lambda e: seen.append(e))

    with pytest.raises(RuntimeError, match="update_backends boom"):
        await app.reconfigure(s2)

    # 提交段在任何赋值之前就抛,app 全部保持旧引用
    assert app.storage is old_storage
    assert app.objects is old_objects
    assert app.settings is s1
    assert app.channel_sync.storage is old_storage
    assert seen == []


# ---------- 4) 收尾段失败隔离 ----------


async def test_finalize_publish_failure_does_not_propagate_and_still_closes_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """publish 抛错 → 不向上传播,切换已完成、旧后端仍关闭(原实现会泄漏)。"""
    app, monitor, bus, old_storage, old_objects, s1 = _make_app(tmp_path, monkeypatch)
    new_storage = InMemoryRepository()
    await new_storage.connect()
    monkeypatch.setattr(
        "tgmonitor.core.app_service._reconfigure.build_storage",
        lambda settings: new_storage,
    )
    old_closes: list[int] = []
    _orig_close = old_storage.close

    async def _spy_close() -> None:
        old_closes.append(1)
        await _orig_close()

    old_storage.close = _spy_close  # type: ignore[method-assign]

    async def _boom(event):
        raise RuntimeError("publish boom")

    monkeypatch.setattr(bus, "publish", _boom)
    s2 = _settings(tmp_path, db_dsn="postgresql://new/db")
    s2.ensure_dirs()

    # 不抛 —— 切换已成功,publish 失败仅 log
    await app.reconfigure(s2)

    assert app.storage is new_storage
    assert app.settings is s2
    assert old_closes == [1]


# ---------- 5) 终态一致 ----------


async def test_final_state_all_refs_point_to_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """三段式完成后,域服务 / downloader / settings 全部指向新实例。"""
    app, monitor, bus, old_storage, old_objects, s1 = _make_app(tmp_path, monkeypatch)
    new_storage = InMemoryRepository()
    await new_storage.connect()
    monkeypatch.setattr(
        "tgmonitor.core.app_service._reconfigure.build_storage",
        lambda settings: new_storage,
    )
    s2 = _settings(tmp_path, db_dsn="postgresql://new/db")
    s2.ensure_dirs()

    await app.reconfigure(s2)

    assert app.storage is new_storage
    assert app.channels._storage is new_storage
    assert app.messages._storage is new_storage
    assert app.media._storage is new_storage
    assert app.channel_sync.storage is new_storage
    assert app.channel_sync.downloader is app.downloader
    assert app.media._downloader is app.downloader
    assert app.downloader is monitor.downloader
    assert monitor.downloader.storage is new_storage
    assert app.settings is s2
    assert app.auth is not None


# ---------- 6) UI 文案分支 ----------


def test_settings_page_reentry_shows_correct_message(
    qapp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重入异常走「正在应用」弹窗,不误报「后端校验未通过」;按钮恢复。

    用同步 run_coro 替身直接驱动 `_on_apply` 的协程到异常点,验证生产代码
    真正的错误回调分支(不是复刻逻辑)。
    """
    from unittest.mock import AsyncMock, Mock

    from PySide6.QtWidgets import QMessageBox

    from tgmonitor.ui.widgets import settings_page as sp

    mock_app = Mock()
    mock_app.settings = Settings.for_test()
    mock_app.reconfigure = AsyncMock(side_effect=ReconfigureInProgressError("in progress"))
    page = sp.SettingsPage(app=mock_app, loop=Mock(), env_path=tmp_path / ".env")

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda parent, title, text: calls.append(("info", title)) or 0,
    )
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda parent, title, text: calls.append(("crit", title)) or 0,
    )

    def _sync_run_coro(loop, coro, *, on_success=None, on_error=None, error_label=""):
        """把 coro 同步推到第一个 await —— AsyncMock 在此立即抛异常。"""
        try:
            coro.send(None)
        except BaseException as exc:  # noqa: BLE001 — 模拟 run_coro 的错误回调
            if on_error is not None:
                on_error(exc)

    monkeypatch.setattr(sp, "run_coro", _sync_run_coro)

    page._on_apply()

    assert calls == [("info", "正在应用")], f"重入应走 information 而非 critical,实际 {calls}"
    assert page.btn_apply.isEnabled() is True, "错误回调必须恢复保存按钮"
