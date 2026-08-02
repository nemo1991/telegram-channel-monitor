"""TDLib 异常归一 — 把 aiotdlib 抛的 / 兜底 `Exception` 转成用户可读描述。

模块拆分(2026-08-02):从 `tdlib_client.py` 抽出。

包含:
- `_extract_error_detail` — `getattr(e, "message", None) or str(e) or "未知错误"`
- `TelegramRateLimitError` — 429 / FLOOD_WAIT_* 归一异常
- `ClientClosingError` — `TdlibTelegramClient.close()` 已调时的 entry guard 异常

不依赖 `TdlibTelegramClient` 类,所以可独立单元测试 + 跨模块复用
(`channel_sync/service.py` 捕获 `TelegramRateLimitError`)。
"""
from __future__ import annotations


def _extract_error_detail(exc: BaseException) -> str:
    """从 aiotdlib 异常 / 一般 Exception 抽用户可读描述。

    - aiotdlib 0.27 的 `AioTDLibError` 用 `.message` 字段(原始 TDLib `error.message`)
    - 兜底 `str(exc)`(TimeoutError 等无 `.message`)
    - 兜底 `"未知错误"`(极端情况,字符串为空)

    出现位置:
      - `_check_authentication_code` / `_check_authentication_password`
      - `_translate_rate_limit` 解析 "FLOOD_WAIT_NNN" 字符串
    """
    msg = getattr(exc, "message", None) or str(exc) or "未知错误"
    return msg


class TelegramRateLimitError(RuntimeError):
    """TDLib 限流(429 / FLOOD_WAIT_*)归一异常。

    ChannelSyncService 收到这个异常后等 `retry_after_seconds` 再继续,
    保证不踩 Telegram 限流红线。
    """

    def __init__(self, retry_after_seconds: float, message: str = "") -> None:
        """`retry_after_seconds` = 等待秒数;`message` = 可选覆盖。"""
        self.retry_after_seconds = float(retry_after_seconds)
        super().__init__(message or f"Telegram rate limit: wait {retry_after_seconds:.0f}s")


class ClientClosingError(RuntimeError):
    """TdlibClient 已经进入关闭流程(`close()` 已调)。

    公共 async 方法(entry guard)在进入 aiotdlib bridge 之前 throw,
    避免再撞 10s request 超时 + 跨 loop wakeup 噪音。多数用户面方法是
    `best-effort`,会自己 catch 住;事务性方法(submit_* / logout)让它冒上去。
    """

    def __init__(self, message: str = "TdlibClient is closing") -> None:
        """`message` 默认 "TdlibClient is closing";子类可覆盖。"""
        super().__init__(message)