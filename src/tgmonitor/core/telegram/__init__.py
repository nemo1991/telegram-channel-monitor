"""telegram 子包 — 唯一接触 Telegram 后端(Telethon)之处。

公开给上层(core 其余部分、UI)只通过 `TelegramClient` Protocol/接口。
Telethon 自身的类型与原始更新对象不出本包。

模块结构(v2.0.0):
- `client.py` — `TelegramClient` Protocol + `UpdateStream` 抽象
- `factory.py` — `build_telegram_client()` 选择实现
- `fake_client.py` — 测试用 `FakeTelegramClient`
- `unconfigured.py` — 凭据缺失时的占位 client
- `telethon_client.py` — Telethon 实现(登录状态机 / 更新流 / 下载 / 批量操作)
- `telethon_messages.py` — Telethon Message → MessageDTO 纯映射
- `errors.py` — 后端无关异常(`TelegramRateLimitError` / `ClientClosingError` /
  `ChatUnavailableError` / `TelegramNotConfiguredError`)
- `proxy.py` — SOCKS5 代理解析 / 探测
"""

from tgmonitor.core.telegram.errors import (
    ChatUnavailableError,
    ClientClosingError,
    TelegramNotConfiguredError,
    TelegramRateLimitError,
)
from tgmonitor.core.telegram.proxy import parse_socks5_proxy

__all__ = [
    "ChatUnavailableError",
    "ClientClosingError",
    "TelegramNotConfiguredError",
    "TelegramRateLimitError",
    "parse_socks5_proxy",
]
