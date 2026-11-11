"""Telegram 客户端通用异常 — 与具体后端(TDLib / Telethon)无关。

v2.0.0 从 `tdlib_errors.py` / `tdlib_channels.py` 抽出的后端无关部分:
- `_extract_error_detail` — `getattr(e, "message", None) or str(e) or "未知错误"`
- `TelegramRateLimitError` — 429 / FLOOD_WAIT_* 归一异常
- `ClientClosingError` — client `close()` 已调时的 entry guard 异常
- `TelegramNotConfiguredError` — 凭据未配置时构造 client 前的守卫异常
- `ChatUnavailableError` — 频道不可访问(backfill 降级为 warning 跳过)
- `_missing_credentials` — 判定缺失的凭据项(与 `AuthService._check_credentials`
  条件一致,返回缺失项名列表)

不依赖任何 Telegram 后端库,可独立单元测试 + 跨模块复用
(`channel_sync/service.py` 捕获 `TelegramRateLimitError`,
`monitor/backfill.py` 捕获 `ChatUnavailableError` / `ClientClosingError`)。
"""

from __future__ import annotations

from tgmonitor.core.config import Settings


def _extract_error_detail(exc: BaseException) -> str:
    """从后端异常 / 一般 Exception 抽用户可读描述。

    - 带 `.message` 字段的异常(TDLib TdlibError / Telethon RPCError)优先用它
    - 兜底 `str(exc)`(TimeoutError 等无 `.message`)
    - 兜底 `"未知错误"`(极端情况,字符串为空)
    """
    msg = getattr(exc, "message", None) or str(exc) or "未知错误"
    return msg


class TelegramRateLimitError(RuntimeError):
    """Telegram 限流(429 / FLOOD_WAIT_*)归一异常。

    ChannelSyncService 收到这个异常后等 `retry_after_seconds` 再继续,
    保证不踩 Telegram 限流红线。
    """

    def __init__(self, retry_after_seconds: float, message: str = "") -> None:
        """`retry_after_seconds` = 等待秒数;`message` = 可选覆盖。"""
        self.retry_after_seconds = float(retry_after_seconds)
        super().__init__(message or f"Telegram rate limit: wait {retry_after_seconds:.0f}s")


class ClientClosingError(RuntimeError):
    """client 已经进入关闭流程(`close()` 已调)。

    公共 async 方法(entry guard)在进入后端 bridge 之前 throw,
    避免再撞 request 超时 + 跨 loop wakeup 噪音。多数用户面方法是
    `best-effort`,会自己 catch 住;事务性方法(submit_* / logout)让它冒上去。
    """

    def __init__(self, message: str = "Telegram client is closing") -> None:
        """`message` 默认 "Telegram client is closing";子类可覆盖。"""
        super().__init__(message)


class ChatUnavailableError(Exception):
    """频道在后端侧不可访问(拿不到 chat 对象)。

    触发:`iter_chat_history` 预热失败(如频道已不可访问 / 新 session 未加载
    该 chat)。语义是"该频道本轮无法补拉":`MonitorService._backfill` 捕获后
    降级为 warning 并跳过,不再每轮刷 error traceback。其它错误
    (网络 / flood wait 等)仍照常冒泡,由调用方决定怎么处理。
    """

    def __init__(self, channel_id: int, *, reason: str = "") -> None:
        """`channel_id` = 不可访问的频道 id;`reason` = 底层错误描述(可空)。"""
        self.channel_id = channel_id
        self.reason = reason
        super().__init__(f"channel {channel_id} unavailable: {reason or 'unknown'}")


def _missing_credentials(settings: Settings) -> list[str]:
    """返回缺失的 Telegram 凭据项名(空列表 = 完整)。

    判定条件与 `AuthService._check_credentials` 保持一致:
      - `api_id > 0`
      - `api_hash` 非空且长度 ≥ 16(my.telegram.org 给的 hash 长度)
      - `phone` 以 `+` 开头(需国家区号)
    供真 client 构造前抛 `TelegramNotConfiguredError` 用。
    """
    missing: list[str] = []
    if settings.api_id <= 0:
        missing.append("TG_API_ID")
    if not settings.api_hash or len(settings.api_hash) < 16:
        missing.append("TG_API_HASH")
    if not settings.phone.startswith("+"):
        missing.append("TG_PHONE(需 + 国家区号)")
    return missing


class TelegramNotConfiguredError(RuntimeError):
    """Telegram 凭据(api_id / api_hash / phone)未配置 — 构造 client 前的守卫异常。

    在真 client `__init__` 里、底层库 parameters 校验之前抛出,把裸
    `ValidationError`(如 `api_id: 0`)换成用户可读的中文消息 — 由 `app.py`
    启动失败弹窗直接展示,引导用户填 `.env` / 设置…
    """

    def __init__(self, message: str = "") -> None:
        """`message` 默认给通用提示;调用方传拼接好的缺失项。"""
        super().__init__(
            message
            or "未配置 Telegram 凭据(TG_API_ID / TG_API_HASH / TG_PHONE),"
            "请先在 .env 或 设置… 中填写"
        )
