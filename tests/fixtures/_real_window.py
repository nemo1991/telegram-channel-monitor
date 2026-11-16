"""构造真实 MainWindow 的测试 helper(v2.0.1 P2 装配回归共用)。

刻意不注册成 pytest fixture:只有装配回归需要完整窗口,避免给全仓
1500+ 用例引入额外的 Qt / 后台 loop 依赖。

setup 用一次性 temp_loop(沿用 test_main_window_channels.py 的模式,
避开 _LoopThread 跨 test 残留污染);MainWindow 自身用 session-scope
`qloop` 驱动构造期间的 publish_threadsafe 协程。窗口不调 close(),
由 `_qt_runtime` 的 per-test deleteLater 统一回收(不触发 closeEvent)。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from PySide6.QtWidgets import QApplication

from tests.conftest import InMemoryRepository
from tgmonitor.core.app_service import AppService
from tgmonitor.core.config import Settings
from tgmonitor.core.events import EventBus
from tgmonitor.core.monitor.service import MonitorService
from tgmonitor.core.objectstore.local_store import LocalObjectStore
from tgmonitor.core.telegram.fake_client import FakeTelegramClient
from tgmonitor.ui.main_window import MainWindow


def build_real_main_window(
    qloop: asyncio.AbstractEventLoop,
    base: Path,
    *,
    width: int = 1200,
    height: int = 800,
) -> MainWindow:
    """构造并 show() 一个真实 MainWindow,尺寸固定便于几何断言。"""
    settings = Settings.for_test(
        phone="+8612345",
        session_dir=base / "s",
        db_root=base / "m",
        objectstore_root=base / "o",
    )
    bus = EventBus()
    client = FakeTelegramClient()
    client._state = "ready"
    storage = InMemoryRepository()
    objects = LocalObjectStore(root=base / "o")

    async def _setup():
        await storage.connect()
        await objects.connect()
        return (
            AppService(bus, client, storage, objects, settings),
            MonitorService(bus, client, storage, objects, settings),
        )

    temp_loop = asyncio.new_event_loop()
    try:
        app_svc, monitor = temp_loop.run_until_complete(_setup())
    finally:
        temp_loop.close()

    win = MainWindow(app_svc, monitor, qloop, env_path=base / ".env")
    win.resize(width, height)
    win.show()
    QApplication.processEvents()
    return win
