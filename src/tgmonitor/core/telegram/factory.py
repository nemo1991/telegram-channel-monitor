"""TelegramClient 工厂 — 根据 config 与 aiotdlib 可用性选择实现。

注意:本工厂**不再**对真 client 的构造失败做静默 fallback。早期版本会把任何
异常吞掉返回 Fake,导致生产环境出现"无 aiotdlib 也能 ready"的诡异现象
(参见 plan bug #22)。如果用户机器装好了 `aiotdlib>=0.27` 但 __init__ 失败,
现在会上抛 — 由 `app.py` 或上层 UI 暴露给用户。
"""
from __future__ import annotations

import logging

from tgmonitor.core.config import Settings
from tgmonitor.core.telegram.client import TelegramClient

log = logging.getLogger(__name__)


def build_telegram_client(
    settings: Settings,
    *,
    use_fake: bool = False,
    event_bus: object | None = None,
) -> TelegramClient:
    """默认用 TdlibTelegramClient;若 `use_fake=True`,显式返回 Fake。

    Args:
        settings:  全局配置
        use_fake:  显式要求 fake(测试用)。默认 False。
        event_bus: `EventBus` 实例(用于发 LoginStateChanged / AuthErrorOccurred)。
                   None 则新构造的 client 不会发事件。
    """
    if use_fake:
        from tgmonitor.core.telegram.fake_client import FakeTelegramClient
        return FakeTelegramClient()
    from tgmonitor.core.telegram.tdlib_client import (
        _HAVE_AIOTDLIB,
        TdlibTelegramClient,
    )
    if not _HAVE_AIOTDLIB:
        raise RuntimeError(
            "aiotdlib 未安装或版本过老:`pip install -U 'aiotdlib>=0.27'`"
        )
    return TdlibTelegramClient(settings, event_bus=event_bus)
