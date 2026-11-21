"""AuthService — Telegram 登录鉴权域服务。

从 `AppService` 抽出(2026-08-03 微切),2026-11-11 v2.0.0 Phase 3-C2
补 `get_login_state` / `logout`,鉴权调用面全部收拢到本服务。

2026-11-21:手机号+验证码登录链(submit_phone/code/password/email/
registration)在本版本移除 —— 验证码下发渠道不可控导致收不到码。登录方式
只保留二维码扫码:`start_qr_login` / `wait_qr_login`,不依赖手机号。

设计:
  - 持 `bus + client + settings`,不知道 storage / objects / channel_sync,
    只管鉴权这一摊
  - 失败路径统一走 `_fail()` → publish ErrorOccurred + return ('error', str)
  - `bootstrap` 不在此 — 它涉及 client 重建 + AppService.client 替换,
    留在 `AppService` 当 orchestration 入口

公开方法返回 `(state, detail)` 元组,跟 `TelegramClient.state` 一致 —
UI 端调用直接看 state 判断成功 / 失败。
"""

from __future__ import annotations

from tgmonitor.core.config import Settings
from tgmonitor.core.events import ErrorOccurred, EventBus
from tgmonitor.core.telegram.client import TelegramClient


class AuthService:
    """二维码登录编排 + 凭据预检 + 登录态查询/登出。

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
        """若 api 凭据未配置,返回错误消息(供 UI 展示);否则返回 None。

        二维码登录只依赖 api_id/api_hash,不校验手机号(扫码由已登录的
        Telegram App 授权,无需手机号/短信/验证码)。
        """
        s = self._settings
        if s.api_id <= 0:
            return "TG_API_ID 未配置:请打开 设置… 填写"
        if not s.api_hash or len(s.api_hash) < 16:
            return "TG_API_HASH 未配置或过短:请打开 设置… 填写"
        return None

    async def start_qr_login(self) -> tuple[str, str | None]:
        """二维码登录第一步:校验 api 凭据 → 生成二维码 URL(无需手机号)。"""
        err = self._check_credentials()
        if err:
            return await self._fail("start_qr_login", err)
        try:
            return await self._client.start_qr_login()
        except Exception as e:  # noqa: BLE001
            return await self._fail("start_qr_login", e)

    async def wait_qr_login(self) -> tuple[str, str | None]:
        """二维码登录第二步:等待扫码;成功转成 ready。"""
        try:
            return await self._client.wait_qr_login()
        except Exception as e:  # noqa: BLE001
            return await self._fail("wait_qr_login", e)

    async def get_login_state(self) -> str:
        """当前登录状态机值(透传 client.state,不缓存)。"""
        return self._client.state

    async def logout(self) -> None:
        """登出 — 清 session;异常冒泡给调用方(run_coro 统一显示)。"""
        await self._client.logout()
