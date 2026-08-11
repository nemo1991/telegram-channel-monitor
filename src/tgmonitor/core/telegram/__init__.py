"""telegram 子包 — 唯一接触官方 TDLib 之处。

公开给上层(core 其余部分、UI)只通过 `TelegramClient` Protocol/接口。
TDLib 自身的类型与原始更新对象不出本包。

# 模块拆分(2026-08-02)

`telegram` 子包内文件结构:
- `client.py` — `TelegramClient` Protocol + `UpdateStream` 抽象
- `factory.py` — `build_telegram_client()` 选择实现
- `fake_client.py` — 测试用 `FakeTelegramClient`
- `tdlib_client.py` — 唯一接触 tdlib_json 的 lifecycle controller
  (TdlibJsonClient 子类化 + 信号绑 + state machine + channels thin delegate)
- `tdlib_channels.py` — `ChannelsApi` composition 类(持 client 引用,真实 channels
  实现;`tdlib_client.py` 上 6 个 channels 方法是 thin delegate)
- `tdlib_errors.py` — `_extract_error_detail` / `TelegramRateLimitError` /
  `ClientClosingError`
- `tdlib_proxy.py` — `parse_socks5_proxy` / `_load_or_create_encryption_key` /
  `_probe_proxy` / `_translate_boot_error` / `_AUTH_STATE_MAP`
- `tdlib_messages.py` — `_map_message` + 媒体 / service 派发表

子模块在外部被显式 import 的符号:
  - `tdlib_errors.TelegramRateLimitError` / `ClientClosingError` →
    `core/channel_sync/service.py`
  - `tdlib_proxy.parse_socks5_proxy` → 已不再被外部直接 import(只在 `tdlib_client`
    `__init__` 装配时用),但仍 re-export 以保持向后兼容
  - `tdlib_channels.ChannelsApi` → 不被外部 import(仅 `tdlib_client.__init__`
    构造 `self.channels`),但 re-export 以便类型注解 / 测试可见
"""
from tgmonitor.core.telegram.tdlib_channels import ChannelsApi
from tgmonitor.core.telegram.tdlib_errors import (
    ClientClosingError,
    TelegramRateLimitError,
)
from tgmonitor.core.telegram.tdlib_proxy import parse_socks5_proxy

__all__ = [
    "ChannelsApi",
    "ClientClosingError",
    "TelegramRateLimitError",
    "parse_socks5_proxy",
]