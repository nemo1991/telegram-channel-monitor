# mypy: disable-error-code="misc,assignment,override"
"""TDLib 鉴权状态机 — `TdlibTelegramClient` 的 auth 子系统。

2026-10-10 v1.12.x plan-batch2 目标 3 step 2 从 `TdlibTelegramClient` 抽出:
鉴权状态机内部逻辑(5 个 `_check_authentication_*` + `_register_user` +
5 个 `_ask_for_*` + `_submit_auth_step` + `_publish_auth_error` +
`_on_authorization_state_update`)合计 ~250 LOC 抽到 `AuthStateHandler`
composition delegate。6 个公共 `submit_*` + `logout` **必须**仍留在
`TdlibTelegramClient` 本体(Protocol 公开面,test_protocol_method_signatures_match
_tdlib_concrete 守门用 `getattr(tdc.TdlibTelegramClient, name)`),内部走
thin delegate 调 `self._auth.submit_*()`。

# 设计理由

为什么不 mixin?
  - 同 `ChannelsApi` / `EventsHandler`:auth handler 用 `self._state` /
    `self._state_event` / `self._code_queue` / `self._bus` /
    `self._safe_publish_state` / `self._set_state` 等 lifecycle 私有状态,
    纯 mixin 没这些
  - 6 个公共 `submit_*` 必须留在 TdlibTelegramClient 本体(Protocol 守门),
    mixin 没法让这些 method 双向可见(运行在主类、声明在 mixin)

为什么不拆 state machine + prompts?(候选 B)
  - TDLib `AuthorizationState` 推进协议脆弱,改坏卡死在某 state
  - 抽一次已经够,再拆反而难 debug

# 公开 vs 私有

**全部私有** — Protocol 公共方法(`submit_*` / `logout`)在 TdlibTelegramClient
本体做 thin delegate。AuthStateHandler 自身**不**被 Protocol 暴露。

# 依赖方向

`tdlib_auth.py` → `tdlib_errors._extract_error_detail`(已有独立模块)
`tdlib_auth.py` → `tdlib_proxy._AUTH_STATE_MAP`(已有独立模块)
`tdlib_auth.py` → `tdlib_json.TdlibError`(从 tdlib_json 子项目)
`tdlib_auth.py` 不反向 import `tdlib_client` — 无循环依赖。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

from tgmonitor.core.telegram.tdlib_errors import _extract_error_detail
from tgmonitor.core.telegram.tdlib_proxy import _AUTH_STATE_MAP

if TYPE_CHECKING:
    from tgmonitor.core.telegram.tdlib_client import TdlibTelegramClient

log = logging.getLogger(__name__)


class AuthStateHandler:
    """TDLib `AuthorizationState` 状态机 + 鉴权输入队列管理。

    持 `client: TdlibTelegramClient` 引用以访问 lifecycle 资源
    (`_state` / `_state_event` / `_code_queue` / `_password_queue` /
    `_email_queue` / `_email_code_queue` / `_registration_queue` / `_bus` /
    `_set_state` / `_safe_publish_state`)。

    不直接构造 — 由 `TdlibTelegramClient.__init__` 末尾建:
        self._auth = AuthStateHandler(self)
    然后 6 个公共 `submit_*` + `logout` thin delegate 走 `self._auth.X`。
    """

    def __init__(self, client: TdlibTelegramClient) -> None:
        """`client` = 父 lifecycle 控制器(只持引用,不构造任何资源)。"""
        self._c = client

    # ============================================================
    # `_on_authorization_state_update` — tdlib_json 钩子 override
    # ============================================================

    async def _on_authorization_state_update(self, authorization_state) -> None:
        """tdlib_json 的 `_updates_loop` 自己截胡 `updateAuthorizationState`,直接
        调我们这个方法(不走 add_event_handler)。所以唯一写法是 override。

        tdlib_json 鉴权状态机的两个已知缺陷:
        - 用 `send()`(fire-and-forget)发 setTdlibParameters / check* — 错误
          (例如 `Error code=401`)响应被静默丢,因为没有 request_id。
        - 我们 override 后**必须** `await super()`,否则 tdlib_json 自己不会调
          `_check_authentication_code` 等后续钩子。
        """
        try:
            state_id = authorization_state.get("@type")
            new_state = _AUTH_STATE_MAP.get(state_id, "unknown")
        except Exception:  # noqa: BLE001
            log.exception("auth state mapping failed")
            new_state = "unknown"
        # 走 _set_state — 唯一写路径
        self._c._set_state(new_state)
        try:
            await super(type(self._c), self._c)._on_authorization_state_update(
                authorization_state,
            )
        except Exception:  # noqa: BLE001
            # 不 raise — `_updates_loop` 会把异常 raise 给自己并终止所有
            # 后续 update 派发。这只是兜底 log,tdlib_json 自己的 send() 错误
            # 因为没有 request_id 不会进到这里;真到这里说明 tdlib_json 内部
            # 出问题,例如 _set_authentication_phone_number 异常。
            log.exception("super _on_authorization_state_update failed (suppressed)")

    # ============================================================
    # 鉴权输入队列消费(被 TDLib state 推进钩子调)
    # ============================================================

    async def _submit_auth_step(
        self,
        source: str,
        queue: asyncio.Queue,
        request_factory: Callable[[str], Any],
        error_label: str,
        detail_prefix: str,
    ) -> None:
        """`_check_authentication_code` / `_check_authentication_password` 共用骨架。

        流程:
          1. 同步从 queue 取 UI 提交的值
          2. 调 `request_factory(value)` 发 raw dict 请求(checkAuthentication*)
          3. TdlibError → 转 detail,发 `AuthErrorOccurred` (不 raise)
          4. 其它 Exception → log.exception + 发 `AuthErrorOccurred`

        Args:
          - `source`:AuthErrorOccurred.source("code" / "password")
          - `queue`:asyncio.Queue,等 UI 提交(`_code_queue` / `_password_queue`)
          - `request_factory`:用 value 构造 raw dict 请求(checkAuthenticationCode / checkAuthenticationPassword)
          - `error_label`:日志标签(e.g. "CheckAuthenticationCode")
          - `detail_prefix`:错误文案前缀(e.g. "验证码错误: " / "2FA 密码错误: ")
        """
        from tdlib_json import TdlibError

        value = await queue.get()
        log.info("submitting %s (len=%d)", source, len(value))
        try:
            await self._c.request(request_factory(value), request_timeout=30)
        except TdlibError as e:
            log.warning("%s failed: %s", error_label, e)
            detail = _extract_error_detail(e)
            await self._publish_auth_error(
                source,
                f"{detail_prefix}{detail}",
                e,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("%s unexpected failure", error_label)
            await self._publish_auth_error(source, f"提交失败: {e}", e)

    async def _check_authentication_code(self) -> None:
        """从队列收 UI 提交的验证码。错误 → 发 `AuthErrorOccurred`,
        但不 raise(让 TDLib 自动重新发 WaitCode,用户原地重输)。"""
        await self._submit_auth_step(
            source="code",
            queue=self._c._code_queue,
            request_factory=lambda code: {"@type": "checkAuthenticationCode", "code": code},
            error_label="CheckAuthenticationCode",
            detail_prefix="验证码错误: ",
        )

    async def _check_authentication_password(self) -> None:
        """2FA 密码注入。"""
        await self._submit_auth_step(
            source="password",
            queue=self._c._password_queue,
            request_factory=lambda pwd: {"@type": "checkAuthenticationPassword", "password": pwd},
            error_label="CheckAuthenticationPassword",
            detail_prefix="2FA 密码错误: ",
        )

    async def _check_authentication_email_code(self) -> None:
        """2026-08-27 v1.4.0 PR #13:邮箱验证码注入。"""
        await self._submit_auth_step(
            source="email_code",
            queue=self._c._email_code_queue,
            request_factory=lambda code: {
                "@type": "checkAuthenticationEmailCode",
                "code": {"@type": "emailAddressAuthenticationCode", "code": code},
            },
            error_label="CheckAuthenticationEmailCode",
            detail_prefix="邮箱验证码错误: ",
        )

    async def _register_user(self) -> None:
        """2026-08-27 v1.4.0 PR #13:注册新账号。从 `_registration_queue` 收
        `(first_name, last_name)` 后调 `registerUser`。
        """
        from tdlib_json import TdlibError

        first_name, last_name = await self._c._registration_queue.get()
        log.info(
            "registering new user (first=%r, last=%r)",
            first_name,
            last_name,
        )
        try:
            await self._c.request(
                {
                    "@type": "registerUser",
                    "first_name": first_name,
                    "last_name": last_name,
                },
                request_timeout=30,
            )
        except TdlibError as e:
            detail = _extract_error_detail(e)
            log.warning("registerUser failed: %s", e)
            await self._publish_auth_error(
                "registration",
                f"注册失败: {detail}",
                e,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("registerUser unexpected failure")
            await self._publish_auth_error(
                "registration",
                f"注册失败: {e}",
                e,
            )

    # ============================================================
    # TDLib state 推进钩子(被基类 _on_authorization_state_update 调)
    # ============================================================

    async def _ask_for_code(self) -> None:
        """TDLib 进入 authorizationStateWaitCode 时由基类分发调用 — 接到队列消费链路上。"""
        await self._check_authentication_code()

    async def _ask_for_password(self) -> None:
        """TDLib 进入 authorizationStateWaitPassword 时由基类分发调用 — 接到队列消费链路上。"""
        await self._check_authentication_password()

    async def _ask_for_email_code(self) -> None:
        """2026-08-27 v1.4.0 PR #13:`authorizationStateWaitEmailCode` 时
        由基类分发调用。
        """
        await self._check_authentication_email_code()

    async def _ask_for_registration(self) -> None:
        """2026-08-27 v1.4.0 PR #13:`authorizationStateWaitRegistration` 时
        由基类分发调用。
        """
        await self._register_user()

    # ============================================================
    # 鉴权错误发布(供 submit_* 公共方法 + 队列消费链共用)
    # ============================================================

    async def _publish_auth_error(
        self, source: str, message: str, exception: BaseException | None = None
    ) -> None:
        """transient 鉴权错误(验证码错、密码错、phone 错)— 不改顶层状态,
        只通过 `AuthErrorOccurred` 通知 UI。"""
        if self._c._bus is None:
            log.warning("auth error %s: %s (no bus)", source, message)
            return
        try:
            from tgmonitor.core.events import AuthErrorOccurred

            await self._c._bus.publish(
                AuthErrorOccurred(
                    source=source,
                    message=message,
                    exception=exception,
                )
            )
        except Exception:  # noqa: BLE001
            log.exception("publish AuthErrorOccurred failed")

    # ============================================================
    # 公共 submit_* 方法(被 Protocol 公共面 thin delegate 调用)
    # ============================================================
    # 6 个公共方法 + logout 都需留在 TdlibTelegramClient 本体(Protocol 守门用
    # `getattr(tdc.TdlibTelegramClient, name)`),这里装真实实现。

    async def submit_phone(self, phone: str) -> tuple[str, str | None]:
        """用户点「登录」时调用 — 提交手机号给 TDLib,触发验证码下发。

        显式发 `setAuthenticationPhoneNumber`,不再依赖 tdlib_json 构造时的
        自动发号:那一路取的是 init 参数,用户后填 / 后改的手机号根本进不了
        调用链,且空号也会照发(fire-and-forget),导致卡在 `phone_required`
        而 UI 无任何反馈。错误 → `AuthErrorOccurred` 事件,不改顶层状态。
        """
        from tdlib_json import TdlibError

        self._c._check_alive()
        if not getattr(self._c, "_running", False):
            return self._c._state, self._c._state_detail
        phone = (phone or "").strip()
        if not phone.startswith("+"):
            await self._publish_auth_error(
                "phone",
                "手机号需以 + 国家区号开头,如 +8613800000000",
            )
            return self._c._state, self._c._state_detail
        # 等 TDLib 进入 phone_required(最多 5s)
        if self._c._state != "phone_required":
            self._c._state_event.clear()
            try:
                await asyncio.wait_for(self._c._state_event.wait(), timeout=5.0)
            except TimeoutError:
                pass
        if self._c._state != "phone_required":
            log.warning(
                "submit_phone: state=%s,非 phone_required 无法发号",
                self._c._state,
            )
            return self._c._state, self._c._state_detail
        log.info("submitting phone %s → setAuthenticationPhoneNumber", phone)
        try:
            await self._c.request(
                {
                    "@type": "setAuthenticationPhoneNumber",
                    "phone_number": phone,
                    "settings": {
                        "@type": "phoneNumberAuthenticationSettings",
                        "allow_flash_call": False,
                        "allow_missed_call": False,
                        "is_current_phone_number": True,
                        "allow_sms_retriever_api": False,
                        "authentication_tokens": [],
                    },
                },
                request_timeout=30,
            )
        except TdlibError as e:
            detail = _extract_error_detail(e)
            log.warning("submit_phone failed: %s", e)
            await self._publish_auth_error("phone", f"登录失败: {detail}", e)
            return self._c._state, self._c._state_detail
        # 等 TDLib 推进到 code_required(最多 15s)
        self._c._state_event.clear()
        try:
            await asyncio.wait_for(self._c._state_event.wait(), timeout=15.0)
        except TimeoutError:
            log.warning(
                "submit_phone: 发号后 15s 未推进 (state=%s)",
                self._c._state,
            )
        return self._c._state, self._c._state_detail

    async def submit_code(self, code: str) -> tuple[str, str | None]:
        """UI 提交验证码。

        把 code push 进队列(由 `_check_authentication_code` 钩子消费),然后等
        状态变更。错误经 `request()` → `AuthErrorOccurred` 事件传出。
        """
        self._c._check_alive()
        await self._c._code_queue.put(code)
        self._c._state_event.clear()
        try:
            await asyncio.wait_for(self._c._state_event.wait(), timeout=15.0)
        except TimeoutError:
            log.warning("submit_code: timeout (state=%s)", self._c._state)
        return self._c._state, self._c._state_detail

    async def submit_password(self, password: str) -> tuple[str, str | None]:
        """UI 提交 2FA 密码。push 进队列 + 等状态变更;错误经 `AuthErrorOccurred` 传出。"""
        self._c._check_alive()
        await self._c._password_queue.put(password)
        self._c._state_event.clear()
        try:
            await asyncio.wait_for(self._c._state_event.wait(), timeout=15.0)
        except TimeoutError:
            log.warning("submit_password: timeout (state=%s)", self._c._state)
        return self._c._state, self._c._state_detail

    async def submit_email(self, email: str) -> tuple[str, str | None]:
        """2026-08-27 v1.4.0 PR #13:`authorizationStateWaitEmailAddress` 时
        提交邮箱地址。同步发 `setAuthenticationEmailAddress`,等状态推进到
        `email_code_required`(已有账号)或 `registration_required`(新账号)。
        """
        from tdlib_json import TdlibError

        self._c._check_alive()
        if not getattr(self._c, "_running", False):
            return self._c._state, self._c._state_detail
        email = (email or "").strip()
        if "@" not in email or len(email) > 100:
            await self._publish_auth_error(
                "email",
                "邮箱格式不正确",
            )
            return self._c._state, self._c._state_detail
        # 等 TDLib 进入 email_required(最多 5s)
        if self._c._state != "email_required":
            self._c._state_event.clear()
            try:
                await asyncio.wait_for(self._c._state_event.wait(), timeout=5.0)
            except TimeoutError:
                pass
        if self._c._state != "email_required":
            log.warning(
                "submit_email: state=%s,非 email_required 无法发邮箱",
                self._c._state,
            )
            return self._c._state, self._c._state_detail
        log.info("submitting email → setAuthenticationEmailAddress")
        try:
            await self._c.request(
                {
                    "@type": "setAuthenticationEmailAddress",
                    "email_address": email,
                },
                request_timeout=30,
            )
        except TdlibError as e:
            detail = _extract_error_detail(e)
            log.warning("submit_email failed: %s", e)
            await self._publish_auth_error("email", f"邮箱提交失败: {detail}", e)
            return self._c._state, self._c._state_detail
        self._c._state_event.clear()
        try:
            await asyncio.wait_for(self._c._state_event.wait(), timeout=15.0)
        except TimeoutError:
            log.warning(
                "submit_email: 发邮箱后 15s 未推进 (state=%s)",
                self._c._state,
            )
        return self._c._state, self._c._state_detail

    async def submit_email_code(self, code: str) -> tuple[str, str | None]:
        """2026-08-27 v1.4.0 PR #13:UI 提交邮箱验证码。push 进队列 + 等状态变更。
        错误经 `AuthErrorOccurred` 传出。
        """
        self._c._check_alive()
        await self._c._email_code_queue.put(code)
        self._c._state_event.clear()
        try:
            await asyncio.wait_for(self._c._state_event.wait(), timeout=15.0)
        except TimeoutError:
            log.warning("submit_email_code: timeout (state=%s)", self._c._state)
        return self._c._state, self._c._state_detail

    async def submit_registration(
        self, first_name: str, last_name: str = ""
    ) -> tuple[str, str | None]:
        """2026-08-27 v1.4.0 PR #13:`authorizationStateWaitRegistration` 时
        注册新账号。push 进队列 + 等状态变更。
        """
        self._c._check_alive()
        first_name = (first_name or "").strip()
        last_name = (last_name or "").strip()
        if not first_name:
            await self._publish_auth_error(
                "registration",
                "注册需提供 first_name",
            )
            return self._c._state, self._c._state_detail
        await self._c._registration_queue.put((first_name, last_name))
        self._c._state_event.clear()
        try:
            await asyncio.wait_for(self._c._state_event.wait(), timeout=15.0)
        except TimeoutError:
            log.warning("submit_registration: timeout (state=%s)", self._c._state)
        return self._c._state, self._c._state_detail

    async def logout(self) -> None:
        """登出 — TDLib 会自动反推状态机 Closed → PhoneNumber。"""
        self._c._check_alive()
        try:
            await self._c.request({"@type": "logOut"})
        except Exception:  # noqa: BLE001
            log.exception("logout failed")
        self._c._me = None
