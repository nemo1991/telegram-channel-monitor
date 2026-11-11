"""TelegramClient 工厂 — 根据 config 选择实现。

- **凭据缺失**(`TG_API_ID` / `TG_API_HASH` / `TG_PHONE` 未配置)→ 返回
  `UnconfiguredTelegramClient` 占位实现:应用能正常启动进 UI,显示"未登录"
  引导,用户在 设置 → 账户 填好凭据后重启即可监听。
- `use_fake=True` → 显式返回 Fake(测试用)。
- 其余情况构造真 `TelethonTelegramClient`;构造失败**不**静默 fallback
  (历史 bug #22:吞异常返回 Fake 导致"无后端也能 ready"),
  直接上抛,由 `app.py` 或上层 UI 暴露给用户。
"""

from __future__ import annotations

import logging

from tgmonitor.core.config import Settings
from tgmonitor.core.telegram.client import TelegramClient
from tgmonitor.core.telegram.errors import _missing_credentials

log = logging.getLogger(__name__)


def build_telegram_client(
    settings: Settings,
    *,
    use_fake: bool = False,
    event_bus: object | None = None,
) -> TelegramClient:
    """默认用 TelethonTelegramClient;凭据缺失返回占位;`use_fake=True` 返回 Fake。

    Args:
        settings:  全局配置
        use_fake:  显式要求 fake(测试用)。默认 False。
        event_bus: `EventBus` 实例(用于发 LoginStateChanged / AuthErrorOccurred)。
                   None 则新构造的 client 不会发事件。
    """
    if use_fake:
        from tgmonitor.core.telegram.fake_client import FakeTelegramClient

        return FakeTelegramClient()
    # 凭据未配置:不构造真 client,返回占位实现 — UI 正常启动显示"未登录"
    # 引导,填好凭据重启即可。
    missing = _missing_credentials(settings)
    if missing:
        log.info("[factory] Telegram 凭据未配置(%s),使用占位客户端", "、".join(missing))
        from tgmonitor.core.telegram.unconfigured import UnconfiguredTelegramClient

        return UnconfiguredTelegramClient()
    from tgmonitor.core.telegram.telethon_client import TelethonTelegramClient

    return TelethonTelegramClient(settings, event_bus=event_bus)
