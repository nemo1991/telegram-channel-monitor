"""telegram 子包 — 唯一接触官方 TDLib 之处。

公开给上层(core 其余部分、UI)只通过 `TelegramClient` Protocol/接口。
TDLib 自身的类型与原始更新对象不出本包。

# 模块拆分(2026-08-02)

`telegram` 子包内文件结构:
- `client.py` — `TelegramClient` Protocol + `UpdateStream` 抽象
- `factory.py` — `build_telegram_client()` 选择实现
- `fake_client.py` — 测试用 `FakeTelegramClient`
- `tdlib_client.py` — 唯一接触 aiotdlib 的 lifecycle controller
  (aiotdlib.Client 子类化 + 信号绑 + state machine + channels 子块)
- `tdlib_errors.py` — `_extract_error_detail` / `TelegramRateLimitError` /
  `ClientClosingError`
- `tdlib_proxy.py` — `parse_socks5_proxy` / `_load_or_create_encryption_key` /
  `_probe_proxy` / `_translate_boot_error` / `_AUTH_STATE_MAP`
- `tdlib_messages.py` — `_map_message` + 媒体 / service 派发表

子模块仅 `tdlib_errors.TelegramRateLimitError` / `ClientClosingError` /
`tdlib_proxy.parse_socks5_proxy` 在外部(`channel_sync/service.py` /
`factory.py`)被显式 import — 故在下面 re-export。其他符号保持模块私有。
"""
from tgmonitor.core.telegram.tdlib_errors import (
    ClientClosingError,
    TelegramRateLimitError,
)
from tgmonitor.core.telegram.tdlib_proxy import parse_socks5_proxy

__all__ = [
    "ClientClosingError",
    "TelegramRateLimitError",
    "parse_socks5_proxy",
]