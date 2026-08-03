"""AuthService — Telegram 登录鉴权 façade(给 AppService 用)。

从 `AppService` 抽出(2026-08-03 微切):
  - `_check_credentials` — 凭据预检(api_id / api_hash / phone)
  - `submit_phone` / `submit_code` / `submit_password` — 3 个
    「client.x → publish ErrorOccurred + return ('error', str)」模板

设计:
  - 持 `bus + client + settings`,不知道 storage / objects / channel_sync,
    只管鉴权这一摊
  - 3 个 submit 方法用同一个 `_fail()` helper 收敛错误路径
  - `bootstrap` 不在此 — 它涉及 client 重建 + AppService.client 替换,
    留在 `AppService` 当 orchestration 入口

公开方法返回 `(state, detail)` 元组,跟 `TelegramClient.state` 一致 —
UI 端 `submit_*` 调用直接看 state 判断成功 / 失败。
"""
from __future__ import annotations

from tgmonitor.core.config import Settings
from tgmonitor.core.events import ErrorOccurred, EventBus
from tgmonitor.core.telegram.client import TelegramClient


class AuthService:
    """3 个登录提交方法 + 凭据预检。

    错误统一走 `bus.publish(ErrorOccurred(source=...))`,跟 `AppService` 别处
    的 try/except + publish 模式一致;return `("error", str)` 让 UI 能直接
    显示给用户。
    """

    def __init__(self, bus: EventBus, client: TelegramClient, settings: Settings) -> None:
        """持 3 个引用:bus(发 ErrorOccurred)+ client(委派)+ settings(凭据预检)。"""
        self._bus = bus
        self._client = client
        self._settings = settings

    async def _fail(self, source: str, exc_or_msg: Exception | str) -> tuple[str, str]:
        """统一失败路径 — publish ErrorOccurred + return ('error', str)。

        `exc_or_msg` 是 Exception → 顺带传 `exception=` 字段(便于 log 留 stack);
        是 str → 只传 message(凭据预检错不是 Exception)。
        """
        msg = str(exc_or_msg)
        kwargs: dict = {"source": source, "message": msg}
        if isinstance(exc_or_msg, Exception):
            kwargs["exception"] = exc_or_msg
        await self._bus.publish(ErrorOccurred(**kwargs))
        return "error", msg

    def _check_credentials(self) -> str | None:
        """若凭据未配置,返回错误消息(供 UI 展示);否则返回 None。"""
        s = self._settings
        if s.api_id <= 0:
            return "TG_API_ID 未配置:请打开 设置… 填写"
        if not s.api_hash or len(s.api_hash) < 16:
            return "TG_API_HASH 未配置或过短:请打开 设置… 填写"
        if not s.phone.startswith("+"):
            return "TG_PHONE 未配置(需 + 国家区号):请打开 设置… 填写"
        return None

    async def submit_phone(self, phone: str) -> tuple[str, str | None]:
        """用户点「登录」按钮 — 提交手机号 + 触发 aiotdlib 发 code。"""
        err = self._check_credentials()
        if err:
            return await self._fail("submit_phone", err)
        try:
            return await self._client.submit_phone(phone)
        except Exception as e:  # noqa: BLE001
            return await self._fail("submit_phone", e)

    async def submit_code(self, code: str) -> tuple[str, str | None]:
        """UI 提交验证码。错误由 AuthErrorOccurred 事件 + catch-all ErrorOccurred 双轨。"""
        try:
            return await self._client.submit_code(code)
        except Exception as e:  # noqa: BLE001
            return await self._fail("submit_code", e)

    async def submit_password(self, password: str) -> tuple[str, str | None]:
        """UI 提交 2FA 密码。错误走 ErrorOccurred;UI 通常弹窗提示。"""
        try:
            return await self._client.submit_password(password)
        except Exception as e:  # noqa: BLE001
            return await self._fail("submit_password", e)