"""TelegramClient 工厂 — 根据 config 与 aiotdlib 可用性选择实现。"""
from __future__ import annotations

from tgmonitor.core.config import Settings
from tgmonitor.core.telegram.client import TelegramClient


def build_telegram_client(settings: Settings, use_fake: bool = False) -> TelegramClient:
    """默认用 TdlibTelegramClient;若 `use_fake=True` 或 aiotdlib 不可用,回退到 Fake。"""
    if use_fake:
        from tgmonitor.core.telegram.fake_client import FakeTelegramClient
        return FakeTelegramClient()
    try:
        from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient
        return TdlibTelegramClient(settings)
    except Exception:
        from tgmonitor.core.telegram.fake_client import FakeTelegramClient
        return FakeTelegramClient()
