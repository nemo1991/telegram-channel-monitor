from __future__ import annotations

# TdlibJsonClient — 与 aiotdlib 的 Client 对齐的高层异步客户端。
# 设计目标:作为 aiotdlib 已归档后的同构替代,保持 tgmonitor 现有代码
# 的调用面(stub 兼容),但把请求/响应全部换成 raw dict(TDLib JSON 格式)。
import asyncio
import base64
import logging
import pathlib
import uuid
from typing import Any, Callable, Coroutine

from .errors import TdlibError
from .objects import TDLibObject
from .proxy import Socks5Proxy
from .tdjson import TDJsonClient, TDJsonQuery

# 会阻塞等待用户输入的鉴权 action,必须派成独立任务而不能 inline await
# (原因见 _on_authorization_state_update 注释)。
_BLOCKING_AUTH_ACTIONS = frozenset(
    {
        "authorizationStateWaitCode",
        "authorizationStateWaitEmailAddress",
        "authorizationStateWaitEmailCode",
        "authorizationStateWaitRegistration",
        "authorizationStateWaitPassword",
    }
)


class Handler:
    """事件处理器包装:统一为 `async (client, update) -> None` 签名。"""

    def __init__(self, handler: Callable[..., Coroutine[Any, Any, Any]]):
        self.handler = handler

    async def __call__(self, client, update):
        await self.handler(client, update)


class PendingRequest:
    """一次未完成的 TDLib 请求:等待响应并记录错误。"""

    def __init__(self, client, request: dict):
        self.client = client
        self.request = request
        self.update: dict | None = None
        self.error: TdlibError | None = None
        self._ready_event = asyncio.Event()

    async def wait(self, *, timeout: float | None = None) -> None:  # noqa: ASYNC109 — 内部用 asyncio.wait_for 实现
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    def set_update(self, update: dict) -> None:
        if update.get("@type") == "error":
            self.error = TdlibError(int(update.get("code") or 0), str(update.get("message") or ""))

        self.update = update
        self._ready_event.set()


class TdlibJsonClient:
    """基于 libtdjson 的异步 Telegram 客户端。

    与 aiotdlib 的 `Client` 对齐的公开 API:

    - `add_event_handler` / `remove_event_handler`:事件订阅;
    - `request(query, request_timeout=...)`:发起带超时的请求并等待响应;
    - `execute(query)`:同步执行(TDLib 的 td_execute);
    - `send(query)`:单向发送,不等待响应;
    - `start()` / `authorize()` / `stop()`:生命周期控制(tgmonitor 用
      `_do_start_inner` 手工驱动,不走 `start()`);
    - `_updates_loop`:更新事件分发主循环(由子类创建任务)。

    所有请求/响应都是 raw dict(TDLib JSON 格式,`@type` 字段标识类型)。
    """

    def __init__(self, parameters: dict[str, Any], *, proxy: Socks5Proxy | None = None):
        self.settings: dict[str, Any] = parameters
        self.proxy: Socks5Proxy | None = proxy
        self.library_path: str | None = parameters.get("library_path") or None
        self.tdjson_client: TDJsonClient = TDJsonClient.create(self.library_path)
        self.logger = logging.getLogger(f"{self.__class__.__name__}_{self.tdjson_client.client_id}")

        self._authorized_event = asyncio.Event()
        self._running = False
        self._update_task: asyncio.Task | None = None
        self._last_updates_loop_restart = 0.0
        self._handlers_tasks: set[asyncio.Task] = set()
        self._pending_requests: dict[str, PendingRequest] = {}
        self._pending_messages: dict[str, Any] = {}
        self._updates_handlers: dict[str, set[Handler]] = {}
        self._middlewares: list[Any] = []
        self._middlewares_handlers: list[Any] = []

    @property
    def is_bot(self) -> bool:
        return bool(self.settings.get("bot_token"))

    # ------------------------------------------------------------------
    # 事件订阅
    # ------------------------------------------------------------------

    def add_event_handler(
        self, handler: Callable[..., Coroutine[Any, Any, Any]], update_type: str = "*"
    ) -> Handler:
        wrapped = Handler(handler)
        self._updates_handlers.setdefault(update_type, set()).add(wrapped)
        return wrapped

    def remove_event_handler(self, handler: Handler) -> None:
        for handlers in self._updates_handlers.values():
            handlers.discard(handler)

    # ------------------------------------------------------------------
    # 请求 API
    # ------------------------------------------------------------------

    async def send(self, query: TDJsonQuery):
        if not self._running:
            raise RuntimeError("Client not started")

        return await self.tdjson_client.send(query)

    async def execute(self, query: TDJsonQuery):
        if not self._running:
            raise RuntimeError("Client not started")

        result = await self.tdjson_client.execute(query)

        if isinstance(result, dict) and result.get("@type") == "error":
            raise TdlibError(int(result.get("code") or 0), str(result.get("message") or ""))

        if result is None:
            return None

        return TDLibObject.from_dict(result)

    async def request(
        self,
        query: dict[str, Any],
        *,
        request_id: str | None = None,
        request_timeout: float | None = None,
    ):
        if not self._running:
            raise RuntimeError("Client not started")

        if not isinstance(query, dict):
            raise TypeError("Query must be a dict")

        if request_timeout is None:
            request_timeout = 10

        if request_id is None:
            request_id = uuid.uuid4().hex

        payload = dict(query)
        extra = payload.get("@extra")

        if not isinstance(extra, dict):
            extra = {}

        extra = {**extra, "request_id": request_id}
        payload["@extra"] = extra

        pending = PendingRequest(self, payload)
        self._pending_requests[request_id] = pending

        try:
            await self.send(payload)
            await pending.wait(timeout=request_timeout)
        except TimeoutError:
            self._pending_requests.pop(request_id, None)
            raise
        finally:
            self._pending_requests.pop(request_id, None)

        if pending.error is not None:
            raise pending.error

        return pending.update

    # ------------------------------------------------------------------
    # 更新分发
    # ------------------------------------------------------------------

    async def _updates_loop(self):
        async for packet in self.tdjson_client.receive():
            if not isinstance(packet, dict):
                continue

            update = TDLibObject.from_dict(packet)
            update_type = update.get("@type")

            if update_type == "updateAuthorizationState":
                authorization_state = update.get("authorization_state")

                if not isinstance(authorization_state, dict):
                    authorization_state = {}

                authorization_state = TDLibObject.from_dict(authorization_state)

                try:
                    await self._on_authorization_state_update(authorization_state)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # 不 raise — `_updates_loop` 是唯一消费 TDLib 推送的任务,
                    # 它一死,实时消息流和所有 request() 响应同时静默超时
                    # (表现为"一段时间不监听")。这里只记日志,让循环继续跑。
                    self.logger.exception(
                        "Failed to process authorization state update; loop keeps running"
                    )
            else:
                try:
                    self._handle_pending_request(update)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("Failed to process pending request update")

                self._create_handler_task(self._handle_update(update))

    def _schedule_updates_loop(self) -> None:
        """创建 `_updates_loop` 任务并挂 done callback — 崩溃后自动重启。

        start() 和各处手动启动都必须走这里,而不是裸 `create_task`,
        否则意外终止没有自愈入口。
        """
        task = asyncio.create_task(self._updates_loop())
        task.add_done_callback(self._on_updates_loop_done)
        self._update_task = task

    def _on_updates_loop_done(self, task: asyncio.Task) -> None:
        """`_updates_loop` 意外终止时排一次重启;cancel / 正常结束不动。"""
        if task.cancelled():
            return
        if task is not self._update_task:
            return  # 已被新 task 顶替,老 task 的收尾回调,忽略
        if task.exception() is None:
            return  # 正常结束(receive() 循环理论上不自己结束)
        self.logger.error(
            "updates_loop crashed with %r; scheduling restart",
            task.exception(),
        )
        asyncio.create_task(self._restart_updates_loop())

    async def _restart_updates_loop(self) -> None:
        """带最小重启间隔,避免崩溃→重启死循环刷爆事件循环。"""
        if not self._running:
            return
        loop = asyncio.get_running_loop()
        delay = max(0.0, 1.0 - (loop.time() - self._last_updates_loop_restart))
        if delay > 0:
            await asyncio.sleep(delay)
        if not self._running:
            return
        if self._update_task is not None and not self._update_task.done():
            return  # 等待期间已有新任务在跑
        self._last_updates_loop_restart = loop.time()
        self._schedule_updates_loop()

    def _handle_pending_request(self, update: dict) -> None:
        extra = update.get("@extra")
        request_id = None

        if isinstance(extra, dict):
            request_id = extra.get("request_id")

        if request_id is None:
            return

        pending = self._pending_requests.pop(request_id, None)

        if pending is not None:
            pending.set_update(update)

    async def _handle_update(self, update: dict) -> None:
        update_type = update.get("@type")
        handlers = self._updates_handlers.get(update_type, set())
        handlers = handlers | self._updates_handlers.get("*", set())

        for handler in handlers:
            try:
                await handler(self, update)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Update handler raised an error")

    def _create_handler_task(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._handlers_tasks.add(task)
        task.add_done_callback(self._handlers_tasks.discard)

    # ------------------------------------------------------------------
    # 启动流程
    # ------------------------------------------------------------------

    async def _setup_proxy(self):
        if self.proxy is None:
            await self.send({"@type": "disableProxy"})
            return

        await self.send(
            {
                "@type": "addProxy",
                "enable": True,
                "server": self.proxy.host,
                "port": self.proxy.port,
                "type": {
                    "@type": "proxyTypeSocks5",
                    "username": self.proxy.username,
                    "password": self.proxy.password,
                },
            }
        )

    async def _setup_options(self):
        options = self.settings.get("options")

        if not isinstance(options, dict):
            return

        for name, value in options.items():
            if isinstance(value, bool):
                option_value = {"@type": "optionValueBoolean", "value": value}
            elif isinstance(value, int):
                option_value = {"@type": "optionValueInteger", "value": value}
            elif isinstance(value, str):
                option_value = {"@type": "optionValueString", "value": value}
            else:
                option_value = {"@type": "optionValueEmpty"}

            await self.send({"@type": "setOption", "name": name, "value": option_value})

    def _set_tdlib_parameters(self) -> dict[str, Any]:
        files_dir = self.settings.get("files_directory")
        files_dir = str(files_dir or "tdlib")

        database_encryption_key = str(self.settings.get("database_encryption_key") or "")
        database_encryption_key = base64.b64encode(database_encryption_key.encode("utf-8")).decode()

        parameters = {
            "@type": "setTdlibParameters",
            "use_test_dc": bool(self.settings.get("use_test_dc", False)),
            "database_directory": str(pathlib.Path(files_dir) / "database"),
            "files_directory": str(pathlib.Path(files_dir) / "files"),
            "use_file_database": bool(self.settings.get("use_file_database", True)),
            "use_chat_info_database": bool(self.settings.get("use_chat_info_database", True)),
            "use_message_database": bool(self.settings.get("use_message_database", True)),
            "use_secret_chats": bool(self.settings.get("use_secret_chats", False)),
            "api_id": int(self.settings.get("api_id") or 0),
            "api_hash": str(self.settings.get("api_hash") or ""),
            "system_language_code": str(self.settings.get("system_language_code") or "en"),
            "device_model": str(self.settings.get("device_model") or "tgmonitor"),
            "system_version": str(self.settings.get("system_version") or ""),
            # TDLib 1.8.46 强制要求非空:为空直接 400 "Application version
            # must be non-empty",setTdlibParameters 失败后状态机卡在
            # tdlib_parameters,永远走不到验证码流程。
            "application_version": str(self.settings.get("application_version") or "1.0.0"),
            "enable_storage_optimizer": True,
            "ignore_file_names": False,
        }

        return parameters

    async def _set_authentication_phone_number(self):
        phone_number = str(self.settings.get("phone_number") or "")
        phone_number = "".join(ch for ch in phone_number if ch.isdigit())

        # 空号直接不发:自动发空号会被 TDLib 以 400 拒绝,且会抢在
        # 调用方显式 `setAuthenticationPhoneNumber` 之前造成竞态。
        if not phone_number:
            self.logger.warning(
                "skip auto setAuthenticationPhoneNumber: phone_number is empty"
            )
            return

        await self.send(
            {
                "@type": "setAuthenticationPhoneNumber",
                "phone_number": phone_number,
                "settings": {
                    "@type": "phoneNumberAuthenticationSettings",
                    "allow_flash_call": False,
                    "allow_missed_call": False,
                    "is_current_phone_number": True,
                    "allow_sms_retriever_api": False,
                    "authentication_tokens": [],
                },
            }
        )

    async def _set_authentication_phone_number_or_check_bot_token(self):
        await self.send({"@type": "setOption", "name": "online", "value": {"@type": "optionValueBoolean", "value": True}})

        if self.is_bot:
            await self.send({"@type": "checkAuthenticationBotToken", "token": str(self.settings.get("bot_token") or "")})
        else:
            await self._set_authentication_phone_number()

    async def _check_authentication_code(self, code: str):
        raise NotImplementedError("`_check_authentication_code` must be implemented in the subclass")

    async def _check_authentication_password(self, password: str):
        raise NotImplementedError("`_check_authentication_password` must be implemented in the subclass")

    async def _check_authentication_email_address(self, email_address: str):
        raise NotImplementedError("`_check_authentication_email_address` must be implemented in the subclass")

    async def _check_authentication_email_code(self, code: str):
        raise NotImplementedError("`_check_authentication_email_code` must be implemented in the subclass")

    async def _register_user(self):
        self.logger.warning("`_register_user` is not implemented")

    async def _auth_start(self):
        await self.send({"@type": "getAuthorizationState"})

    async def _auth_completed(self):
        self._authorized_event.set()

    async def _on_authorization_state_update(self, authorization_state: dict):
        authorization_state_type = authorization_state.get("@type")

        actions = {
            "authorizationStateWaitTdlibParameters": self._set_tdlib_parameters_send,
            "authorizationStateWaitPhoneNumber": self._set_authentication_phone_number_or_check_bot_token,
            "authorizationStateWaitCode": self._ask_for_code,
            "authorizationStateWaitEmailAddress": self._ask_for_email_address,
            "authorizationStateWaitEmailCode": self._ask_for_email_code,
            "authorizationStateWaitRegistration": self._register_user,
            "authorizationStateWaitPassword": self._ask_for_password,
            "authorizationStateReady": self._auth_completed,
            "authorizationStateLoggingOut": self._log_info_logging_out,
            "authorizationStateClosing": self._log_info_closing,
            "authorizationStateClosed": self._log_info_closed,
        }

        action = actions.get(authorization_state_type)

        if action is None:
            return

        # 会阻塞等待用户输入的 action 不能 inline await:
        # _updates_loop 是唯一消费 tdjson socket 流的任务,若在这里等待
        # 用户输入,响应到达时循环冻着没人读 → 后续请求(如
        # checkAuthenticationCode)永远等不到响应 → 30s 超时(自死锁)。
        # 这些 action 派成独立任务,让循环立刻回到 receive() 继续消费。
        if authorization_state_type in _BLOCKING_AUTH_ACTIONS:
            task = asyncio.create_task(action())
            task.add_done_callback(self._auth_action_done)
        else:
            await action()

    def _auth_action_done(self, task: asyncio.Task) -> None:
        """防派发的 auth action 异常静默丢失。"""
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            self.logger.exception("Auth action raised an exception")

    async def _set_tdlib_parameters_send(self):
        await self.send(self._set_tdlib_parameters())

    async def _ask_for_code(self):
        self.logger.warning("TDLib requested an authentication code, but no handler is attached")

    async def _ask_for_email_address(self):
        self.logger.warning("TDLib requested an email address, but no handler is attached")

    async def _ask_for_email_code(self):
        self.logger.warning("TDLib requested an email code, but no handler is attached")

    async def _ask_for_password(self):
        self.logger.warning("TDLib requested a password, but no handler is attached")

    async def _log_info_logging_out(self):
        self.logger.info("TDLib is logging out")

    async def _log_info_closing(self):
        self.logger.info("TDLib is closing")

    async def _log_info_closed(self):
        self.logger.info("TDLib is closed")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def authorize(self):
        await self._auth_start()
        await self._authorized_event.wait()

    async def start(self):
        self._running = True
        self.logger.info("Starting %s", self.__class__.__name__)

        await self.execute({"@type": "setLogVerbosityLevel", "new_verbosity_level": 0})
        await self._setup_proxy()
        await self._setup_options()

        self._schedule_updates_loop()

        try:
            await self.authorize()
        except asyncio.CancelledError:
            await self._cleanup()
            raise

    async def stop(self):
        await self._cleanup()

    async def _cleanup(self):
        self._pending_requests.clear()
        self._pending_messages.clear()

        if self._update_task is not None and not self._update_task.cancelled():
            self._update_task.cancel()

            try:
                await self._update_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._handlers_tasks:
            for task in self._handlers_tasks:
                task.cancel()

            await asyncio.wait(self._handlers_tasks, return_when=asyncio.ALL_COMPLETED)

        try:
            await self.tdjson_client.close()
        except Exception:
            self.logger.exception("Failed to close tdjson client")

        self._running = False
