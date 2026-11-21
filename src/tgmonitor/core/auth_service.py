"""AuthService — Telegram 登录鉴权域服务。

从 `AppService` 抽出(2026-08-03 微切):
  - `_check_credentials` — 凭据预检(api_id / api_hash / phone)
  - `submit_phone` / `submit_code` / `submit_password` — 3 个
    「client.x → publish ErrorOccurred + return ('error', str)」模板

2026-11-11 v2.0.0 Phase 3-C2:补 `get_login_state` / `logout` —
鉴权调用面全部收拢到本服务,AppService 不再转发。

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

    def _check_credentials(
        self, phone: str | None = None, *, require_phone: bool = True
    ) -> str | None:
        """若凭据未配置,返回错误消息(供 UI 展示);否则返回 None。

        `phone` 为调用方当场提交的手机号(设置页输入框里的值,可能还没保存进
        settings)。2.0.8 设置页「登录」设计成「填了就能登录、不必先保存」,
        因此提交路径必须用这个当场值校验 —— 否则首次配置时 settings.phone
        仍为空,会把合法输入误判成「未配置」而根本不调用 client 发验证码。
        None 时回退 settings.phone(兼容不带 phone 的调用方)。

        `require_phone=False` 供二维码登录:只依赖 api_id/api_hash,不需要
        手机号(扫码由已登录的 Telegram App 授权)。
        """
        s = self._settings
        if s.api_id <= 0:
            return "TG_API_ID 未配置:请打开 设置… 填写"
        if not s.api_hash or len(s.api_hash) < 16:
            return "TG_API_HASH 未配置或过短:请打开 设置… 填写"
        if not require_phone:
            return None
        check_phone = phone.strip() if phone is not None else s.phone
        if not check_phone.startswith("+"):
            return "TG_PHONE 未配置(需 + 国家区号):请打开 设置… 填写"
        return None

    async def start_qr_login(self) -> tuple[str, str | None]:
        """二维码登录第一步:校验 api 凭据 → 生成二维码 URL(无需手机号)。"""
        err = self._check_credentials(require_phone=False)
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

    async def submit_phone(self, phone: str) -> tuple[str, str | None]:
        """用户点「登录」按钮 — 提交手机号 + 触发发送验证码。"""
        # 用当场提交的 phone 校验(设置页输入框的值),而非已保存的 settings.phone
        # —— 后者在首次配置时为 "",会把合法手机号误判为「未配置」而不发验证码。
        err = self._check_credentials(phone)
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

    async def submit_email(self, email: str) -> tuple[str, str | None]:
        """`email_required` 时提交邮箱地址。推进到 `email_code_required`
        (已有账号)或 `registration_required`(新账号)。
        """
        email = (email or "").strip()
        if "@" not in email or len(email) > 100:
            return await self._fail(
                "submit_email",
                "邮箱格式不正确",
            )
        try:
            return await self._client.submit_email(email)
        except Exception as e:  # noqa: BLE001
            return await self._fail("submit_email", e)

    async def submit_email_code(self, code: str) -> tuple[str, str | None]:
        """2026-08-27 v1.4.0 PR #13:`authorizationStateWaitEmailCode` 时
        提交邮箱验证码。错误走 ErrorOccurred。
        """
        code = (code or "").strip()
        if not code:
            return await self._fail(
                "submit_email_code",
                "验证码不能为空",
            )
        try:
            return await self._client.submit_email_code(code)
        except Exception as e:  # noqa: BLE001
            return await self._fail("submit_email_code", e)

    async def submit_registration(
        self, first_name: str, last_name: str = ""
    ) -> tuple[str, str | None]:
        """2026-08-27 v1.4.0 PR #13:`authorizationStateWaitRegistration` 时
        注册新账号。错误走 ErrorOccurred。
        """
        first_name = (first_name or "").strip()
        if not first_name:
            return await self._fail(
                "submit_registration",
                "first_name 不能为空",
            )
        try:
            return await self._client.submit_registration(first_name, last_name)
        except Exception as e:  # noqa: BLE001
            return await self._fail("submit_registration", e)
