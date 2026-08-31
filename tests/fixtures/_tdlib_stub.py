"""`stub_tdlib_init` fixture — 2026-08-31 v1.5.0 PR #A6。

原 tests/conftest.py::stub_tdlib_init(行 491-542)。

背景:`tdlib_json.TdlibJsonClient.__init__` 会调 native `TDJsonClient.create()`
加载 libtdjson。单元测试不需要 libtdjson — 这个 stub 把父类 `__init__` 换成
no-op,只塞一些 `TdlibJsonClient` 期望的内部属性,让 `TdlibTelegramClient`
能正常 super().__init__()。

任何要构造 `TdlibTelegramClient` 的测试都需要这个 fixture —
`test_telegram_lifecycle.py` / `test_main_window_channels.py` /
`test_live_updates.py`。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterator

import pytest

from tgmonitor.core.telegram import tdlib_client as tdc


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
