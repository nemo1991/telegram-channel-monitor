"""TDLib 异常归一 — 把 TDLib 抛的 / 兜底 `Exception` 转成用户可读描述。

模块拆分(2026-08-02):从 `tdlib_client.py` 抽出。

包含:
- `_extract_error_detail` — `getattr(e, "message", None) or str(e) or "未知错误"`
- `TelegramRateLimitError` — 429 / FLOOD_WAIT_* 归一异常
- `ClientClosingError` — `TdlibTelegramClient.close()` 已调时的 entry guard 异常
- `TelegramNotConfiguredError` — 凭据未配置时构造 client 前的守卫异常
- `_missing_credentials` — 判定缺失的凭据项(与 `AuthService._check_credentials`
  条件一致,返回缺失项名列表)

不依赖 `TdlibTelegramClient` 类,所以可独立单元测试 + 跨模块复用
(`channel_sync/service.py` 捕获 `TelegramRateLimitError`)。
"""

from __future__ import annotations

from tgmonitor.core.config import Settings


def _extract_error_detail(exc: BaseException) -> str:
    """从 TDLib 异常 / 一般 Exception 抽用户可读描述。

    - `TdlibError`(tdlib_json)用 `.message` 字段
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

    公共 async 方法(entry guard)在进入 TDLib bridge 之前 throw,
    避免再撞 10s request 超时 + 跨 loop wakeup 噪音。多数用户面方法是
    `best-effort`,会自己 catch 住;事务性方法(submit_* / logout)让它冒上去。
    """

    def __init__(self, message: str = "TdlibClient is closing") -> None:
        """`message` 默认 "TdlibClient is closing";子类可覆盖。"""
        super().__init__(message)


def _missing_credentials(settings: Settings) -> list[str]:
    """返回缺失的 Telegram 凭据项名(空列表 = 完整)。

    判定条件与 `AuthService._check_credentials` 保持一致:
      - `api_id > 0`
      - `api_hash` 非空且长度 ≥ 16(my.telegram.org 给的 hash 长度)
      - `phone` 以 `+` 开头(需国家区号)
    供 `TdlibTelegramClient.__init__` 在 `TdlibJsonClient` 的 parameters 校验之前
    抛 `TelegramNotConfiguredError` 用。
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

    在 `TdlibTelegramClient.__init__` 里、`TdlibJsonClient` 的 parameters
    校验之前抛出,把裸 `ValidationError`(如 `api_id: 0`)换成用户可读的中文
    消息 — 由 `app.py` 启动失败弹窗直接展示,引导用户填 `.env` / 设置…
    """

    def __init__(self, message: str = "") -> None:
        """`message` 默认给通用提示;调用方传拼接好的缺失项。"""
        super().__init__(
            message
            or "未配置 Telegram 凭据(TG_API_ID / TG_API_HASH / TG_PHONE),"
            "请先在 .env 或 设置… 中填写"
        )
