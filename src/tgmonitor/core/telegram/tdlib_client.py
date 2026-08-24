# mypy: disable-error-code="misc,assignment,override"
"""TDLib 实现 — 通过 `tdlib_json`(自编译 libtdjson 的 ctypes 绑定)封装。

- 业务侧只见 `TelegramClient` Protocol;此文件**唯一**接触 TDLib
- 鉴权:实际接 `tdlib_json` 的内部状态机(不假装 ready)
  - 用 `asyncio.Queue` 把 UI 提交的 code / 2FA 密码注入 tdlib_json 的钩子
  - 通过 `updateAuthorizationState` 事件把 TDLib 真实状态转 `LoginStateChanged` 推给 UI
- 实时更新:`updateNewMessage` → DTO → UI

模块拆分(2026-08-02):本文件保留 **lifecycle controller**(TdlibJsonClient
子类化 + 信号绑 + state machine + channels 子块);下列 pure helpers 抽到独立模块:
  - `tdlib_errors.py` — `_extract_error_detail` / `TelegramRateLimitError` /
    `ClientClosingError`
  - `tdlib_proxy.py` — `parse_socks5_proxy` / `_load_or_create_encryption_key` /
    `_probe_proxy` / `_translate_boot_error` / `_AUTH_STATE_MAP`
  - `tdlib_messages.py` — `_map_message` + 媒体 / service 派发表

依赖:`tdlib-json-client`(workspace 子项目,aiotdlib 归档后的替代)。
"""
from __future__ import annotations

import asyncio
import collections
import logging
import platform
from typing import Any, AsyncIterator, Callable

log = logging.getLogger(__name__)

try:
    from tdlib_json import TdlibError, TDLibObject
    from tdlib_json import TdlibJsonClient as _AiClient
    _HAVE_TDLIB_JSON = True
except Exception:  # noqa: BLE001
    _HAVE_TDLIB_JSON = False

from tgmonitor import __version__  # noqa: E402
from tgmonitor.core.config import Settings  # noqa: E402 — tdlib_json import 上方有 try/except 守卫
from tgmonitor.core.dto import ChannelDTO, MessageDTO  # noqa: E402
from tgmonitor.core.telegram.client import UpdateStream  # noqa: E402
from tgmonitor.core.telegram.tdlib_errors import (  # noqa: E402
    ClientClosingError,
    TelegramNotConfiguredError,
    TelegramRateLimitError,
    _extract_error_detail,
    _missing_credentials,
)
from tgmonitor.core.telegram.tdlib_messages import _map_message  # noqa: E402
from tgmonitor.core.telegram.tdlib_proxy import (  # noqa: E402
    _AUTH_STATE_MAP,
    _CONN_STATE_MAP,
    _load_or_create_encryption_key,
    _probe_proxy,
    _translate_boot_error,
    parse_socks5_proxy,
)


class _TdlibJsonUpdateStream(UpdateStream):
    """tdlib_json → asyncio.Queue → UI。

    `aclose()` 时除了塞 sentinel 让 async generator 退出,还会触发 caller
    注册的 `on_close` callback — 用来把自己从 `client._streams` 拿掉,
    避免长会话列表只增不减(契约见 `client.UpdateStream` 文档)。
    """

    def __init__(self, on_close: Callable[[_TdlibJsonUpdateStream], None] | None = None) -> None:
        self._queue: asyncio.Queue[MessageDTO | None] = asyncio.Queue()
        self._closed = False
        self._on_close = on_close

    async def push(self, msg: MessageDTO) -> None:
        if not self._closed:
            await self._queue.put(msg)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)
        if self._on_close is not None:
            try:
                self._on_close(self)
            except Exception:  # noqa: BLE001
                log.exception("UpdateStream on_close callback failed")

    def __aiter__(self) -> AsyncIterator[MessageDTO]:
        return self

    async def __anext__(self) -> MessageDTO:
        item = await self._queue.get()
        if item is None or self._closed:
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        await self.close()


class TdlibTelegramClient(_AiClient):
    """生产实现 — 子类 TdlibJsonClient,把鉴权输入接入我们的队列。

    v0.3 重写后的设计原则(详见 plan /Users/forcetone/.claude/plans/...):
      - `_set_state` 是**唯一**写 `self._state` 的入口,所有路径(包括
        `_on_authorization_state_update` / `_nuke_session_and_reinit` /
        `logout`)都过它。任何地方都不许直接 `self._state = ...`。
      - `_state_event: asyncio.Event` 是 `_set_state` 同步 set 的,
        `start()` / `submit_phone()` / `submit_code()` / `submit_password()`
        都 await 这个事件 — 取代旧的 polling loop。
      - 启动不用 `TdlibJsonClient.start()`(它会 `await self.authorize()` 然后
        因为 `_authorized_event` 没人 set 而永久挂起)。改用手工复刻的
        `_do_start_inner()` 直接驱动 updates_loop。
      - submit 错误用 `request()` 抛 `TdlibError`,捕获后通过
        `AuthErrorOccurred` 事件通知 UI,**不**改顶层状态(让用户原地重试)。
      - 401 等"key 不匹配"由 boot 超时 + rotate_key=True 解决:
        调用方(`AppService`)负责在检测到超时 + 401 标记后重建 client。
    """

    # boot() 阶段允许 start 阻塞的最大秒数;过了就当 session 损坏,调用方应重建。
    # SOCKS5 代理冷启动通常 15-25s 才连上 DC,留 30s 余量。
    _BOOT_TIMEOUT = 30.0

    # 启动期"瞬态"状态 — `start()` 等首个状态推进后若仍停在这些状态,
    # 给它 `_SETTLE_GRACE` 秒宽限;超时后按 seen error codes 分流:
    #   - 有 codes(api_id/api_hash 无效等被 TDLib 拒绝)→ 立即转可见错误,
    #     避免 UI 永远停在 `tdlib_parameters` 裸标签;
    #   - 0 codes → TDLib 没拒绝,只是慢(SOCKS5 冷启动连 DC / restore 半成品
    #     session),继续等状态推进,总预算由外层 `_BOOT_TIMEOUT` 兜底。
    _BOOT_TRANSIENT_STATES: frozenset[str] = frozenset({"uninit", "tdlib_parameters"})
    _SETTLE_GRACE = 5.0

    def __init__(self, settings: Settings, *, event_bus: Any | None = None) -> None:
        """`settings` = Telegram / DB / 代理等配置;`event_bus` = 可选 UI 事件总线。

        子类化 TdlibJsonClient,override `_on_authorization_state_update` 钩状态机;
        末尾建 `self.channels = ChannelsApi(self)`(composition delegate)。
        """
        if not _HAVE_TDLIB_JSON:
            raise RuntimeError("tdlib-json-client 未安装:`uv sync` 后重试")
        # 凭据预检 — 未配置时抛 TelegramNotConfiguredError,把底层 TDLib
        # 的启动错误(如 api_id=0)换成用户可读中文消息,
        # 由 app.py 启动失败弹窗直接展示。不静默回退 fake(历史 bug #22)。
        missing = _missing_credentials(settings)
        if missing:
            raise TelegramNotConfiguredError(
                "未配置 Telegram 凭据:" + "、".join(missing)
                + "。请在 .env 或 设置… 中填写后再启动。"
            )
        self._settings = settings
        self._me: dict | None = None
        self._streams: list[_TdlibJsonUpdateStream] = []
        self._chat_titles: dict[int, str] = {}
        self._chat_usernames: dict[int, str] = {}
        self._bus = event_bus
        # 鉴权输入队列(super().__init__ 之前必须建好 — TdlibJsonClient 内部某些路径会读)
        self._code_queue: asyncio.Queue[str] = asyncio.Queue()
        self._password_queue: asyncio.Queue[str] = asyncio.Queue()

        # 顶层状态机当前值。初值是 "uninit" — 真值由 `start()` 后的
        # `updateAuthorizationState` 决定。在 `start()` 调用之前,
        # 任何读到 `state` 的代码都会看到 "uninit"。
        self._state: str = "uninit"
        # 当前状态附带的描述(例如 "SOCKS5 代理不可达")
        self._state_detail: str = ""
        # 状态变化同步 set — `start()` / `submit_*` 等 await 它。
        self._state_event = asyncio.Event()

        # tdlib_json 把 fire-and-forget 的 send() 结果当作 silently dropped 的
        # 错误处理(因为没有 request_id → _handle_pending_request 查不到对应
        # pending request)。但我们的 _updates_loop 仍会看到一个 Error 包,
        # 它会进 `_handle_update` 派发。我们用一个 add_event_handler("*")
        # 兜底,把所有 tdlib_json 内 Error 包的 code 收集起来,这样可以在
        # `start` 超时时判断是不是 "401 wrong encryption key"。
        self._seen_error_codes: collections.deque[int] = collections.deque(maxlen=20)
        # 最近一条无主 Error 包的 message — 翻译 boot 错误时优先用原生 msg
        # 兜底(例如 code=400 "Can't lock file ... already in use")。
        self._last_error_message: str = ""

        # 关闭流程标志位 —— `close()` 入口处立刻 True,所有公共 async 方法
        # 通过 `_check_alive()` 拦截后续 entry。`best-effort` 方法
        # (如 `list_joined_channels`)自己 catch;事务性方法让它冒到调用方。
        # 同时阻断启动后 race:`start()` 还没 ready 时 VM 已 fire-and-forget
        # 调 `list_joined_channels`,tdlib_json bridge 还没绑到当前 loop 上,
        # `request()` 会撞 10s 超时 + qasync 跨 loop wakeup 噪音。
        self._closing: bool = False

        proxy = parse_socks5_proxy(settings.proxy)
        # tdlib_verbosity 决定 tdlib_json 把多少 TDLib 内部日志转发到 Python logging。
        # 默认 FATAL;调试时调到 INFO 可见 401 等线索。
        verbosity = int(getattr(settings, "tdlib_verbosity", 0) or 0)
        parameters: dict[str, Any] = dict(
            api_id=settings.api_id,
            api_hash=settings.api_hash,
            phone_number=settings.phone,
            device_model="tgmonitor",
            system_version=platform.platform(),
            application_version=__version__,
            database_encryption_key=_load_or_create_encryption_key(
                self._settings.session_dir / "tdlib"
            ),
            files_directory=str(settings.session_dir / "tdlib"),
            tdlib_verbosity=verbosity,
            # tdlib_json 默认不批量下发 ClientOptions;部分选项受 TDLib
            # "can be set only if can_<X> is true" 规则约束,user account 下发
            # 会被拒(code=400 "Option can't be set")。没有需要覆盖的选项 →
            # options=None,只发 setTdlibParameters + proxy。
            options=None,
        )
        super().__init__(parameters=parameters, proxy=proxy)

        # tdlib_json 用字符串 update_type 分发事件(updateNewMessage)走 add_event_handler;
        # updateAuthorizationState 走我们 override 的 _on_authorization_state_update
        # (tdlib_json 的 _updates_loop 自己截胡)。
        self.add_event_handler(
            self._on_new_message,
            update_type="updateNewMessage",
        )
        # 2026-08-24:消息编辑( updateMessageContent)— 推到同一 stream,
        # MonitorService 用 _seen_ids 区分「这是新消息 vs 编辑」。
        self.add_event_handler(
            self._on_message_edited,
            update_type="updateMessageContent",
        )
        # 网络连接状态(updateConnectionState)→ ConnectionStateChanged 事件
        self.add_event_handler(
            self._on_connection_state,
            update_type="updateConnectionState",
        )
        # 全局 catch:任何 update 进来都看一眼,把 Error 包的 code 记录下来。
        # tdlib_json 用 `await handler(self, update)` 调用,所以必须 (self, update)。
        async def _on_any_update(client_self, update) -> None:
            if update.get("@type") == "error":
                code = update.get("code")
                if isinstance(code, int):
                    self._seen_error_codes.append(code)
                    msg = str(update.get("message", ""))
                    self._last_error_message = msg
                    log.warning("tdlib_json Error observed: code=%s msg=%s",
                                code, msg)
        self.add_event_handler(
            _on_any_update,
            update_type="*",
        )

        # Channels 子系统 composition 类(2026-08-02 抽出)— 持 self 引用,
        # 内部走 self._c.request / self._c._check_alive 等 lifecycle 资源。
        # caller 仍走 `client.list_joined_channels(...)` 等 thin delegate,
        # Protocol 形状不变。
        from tgmonitor.core.telegram.tdlib_channels import (  # noqa: E402 — 延迟 import 避免循环
            ChannelsApi,
        )
        self.channels = ChannelsApi(self)

    # ============================================================
    # 状态管理: 唯一写路径
    # ============================================================

    def _set_state(self, new_state: str, *, detail: str = "") -> None:
        """唯一允许写 `self._state` 的入口。所有路径(tdlib_json 状态推送、
        我们自己的 nuke/logout 等)都过它。同时负责:
          - 唤醒 `_state_event`,让 await 在上面的 `start()` / `submit_*` 推进
          - 通过 EventBus 发 `LoginStateChanged`
        """
        if new_state == self._state and detail == self._state_detail:
            return
        prev = self._state
        self._state = new_state
        self._state_detail = detail
        log.info("state: %s → %s%s", prev, new_state,
                 f" ({detail})" if detail else "")
        self._state_event.set()
        if self._bus is not None:
            try:
                # 用 fire-and-forget task — 不要 await,避免让 `_updates_loop` 卡住
                asyncio.create_task(self._safe_publish_state(new_state, detail))
            except Exception:  # noqa: BLE001
                log.exception("scheduling LoginStateChanged failed")

    async def _safe_publish_state(self, state: str, detail: str) -> None:
        try:
            from tgmonitor.core.events import LoginStateChanged
            assert self._bus is not None
            await self._bus.publish(LoginStateChanged(state=state, detail=detail))
        except Exception:  # noqa: BLE001
            log.exception("publish LoginStateChanged failed")

    async def _publish_auth_error(
        self, source: str, message: str, exception: BaseException | None = None
    ) -> None:
        """transient 鉴权错误(验证码错、密码错、phone 错)— 不改顶层状态,
        只通过 `AuthErrorOccurred` 通知 UI。"""
        if self._bus is None:
            log.warning("auth error %s: %s (no bus)", source, message)
            return
        try:
            from tgmonitor.core.events import AuthErrorOccurred
            await self._bus.publish(AuthErrorOccurred(
                source=source, message=message, exception=exception,
            ))
        except Exception:  # noqa: BLE001
            log.exception("publish AuthErrorOccurred failed")

    # ============================================================
    # tdlib_json 钩子 override
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
        value = await queue.get()
        log.info("submitting %s (len=%d)", source, len(value))
        try:
            await self.request(request_factory(value), request_timeout=30)
        except TdlibError as e:
            log.warning("%s failed: %s", error_label, e)
            detail = _extract_error_detail(e)
            await self._publish_auth_error(
                source, f"{detail_prefix}{detail}", e,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("%s unexpected failure", error_label)
            await self._publish_auth_error(source, f"提交失败: {e}", e)

    async def _check_authentication_code(self) -> None:
        """从队列收 UI 提交的验证码。错误 → 发 `AuthErrorOccurred`,
        但不 raise(让 TDLib 自动重新发 WaitCode,用户原地重输)。"""
        await self._submit_auth_step(
            source="code",
            queue=self._code_queue,
            request_factory=lambda code: {"@type": "checkAuthenticationCode", "code": code},
            error_label="CheckAuthenticationCode",
            detail_prefix="验证码错误: ",
        )

    async def _check_authentication_password(self) -> None:
        """2FA 密码注入。"""
        await self._submit_auth_step(
            source="password",
            queue=self._password_queue,
            request_factory=lambda pwd: {"@type": "checkAuthenticationPassword", "password": pwd},
            error_label="CheckAuthenticationPassword",
            detail_prefix="2FA 密码错误: ",
        )

    async def _ask_for_code(self) -> None:
        """TDLib 进入 authorizationStateWaitCode 时由基类分发调用 — 接到队列消费链路上。"""
        await self._check_authentication_code()

    async def _ask_for_password(self) -> None:
        """TDLib 进入 authorizationStateWaitPassword 时由基类分发调用 — 接到队列消费链路上。"""
        await self._check_authentication_password()

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
        self._set_state(new_state)
        try:
            await super()._on_authorization_state_update(authorization_state)
        except Exception:  # noqa: BLE001
            # 不 raise — `_updates_loop` 会把异常 raise 给自己并终止所有
            # 后续 update 派发。这只是兜底 log,tdlib_json 自己的 send() 错误
            # 因为没有 request_id 不会进到这里;真到这里说明 tdlib_json 内部
            # 出问题,例如 _set_authentication_phone_number 异常。
            log.exception("super _on_authorization_state_update failed (suppressed)")

    async def _on_new_message(self, client_self, update: TDLibObject) -> None:
        """tdlib_json 用 `await handler(self, update)` 调用 handler,所以签名
        必须是 `(client, update)`,这里 `client_self` 其实是 client 实例本身
        (我们就是它),所以丢弃。"""
        try:
            msg = getattr(update, "message", None)
            if msg is None:
                log.debug(
                    "updateNewMessage received without message payload: %s",
                    getattr(update, "@type", type(update).__name__),
                )
                return
            log.debug(
                "updateNewMessage received: chat_id=%s msg_id=%s",
                getattr(msg, "chat_id", None),
                getattr(msg, "id", None),
            )
            dto = _map_message(msg)
            for s in list(self._streams):
                await s.push(dto)
        except Exception:  # noqa: BLE001
            log.exception("updateNewMessage handling failed")

    async def _on_message_edited(self, client_self, update: TDLibObject) -> None:
        """updateMessageContent(2026-08-24):message 字段携带编辑后完整 Message 对象。

        走 `_map_message` 同样的入口,产出 DTO 后推给所有 `UpdateStream` ——
        与 _on_new_message 共用 `_streams` 列表,DTO 类型不变。
        MonitorService 在 `_run` 用 `_seen_ids` 区分「新消息 vs 编辑」。

        不在 handler 侧做 whitelist 守门:编辑事件来自「已订阅时收到的消息历史」,
        用户可能后续退订了频道 — 编辑仍要落库,UI 显示该消息时按 channels_changed
        自行判断是否过滤。
        """
        try:
            msg = getattr(update, "message", None)
            if msg is None:
                log.debug(
                    "updateMessageContent received without message payload: %s",
                    getattr(update, "@type", type(update).__name__),
                )
                return
            log.debug(
                "updateMessageContent received: chat_id=%s msg_id=%s",
                getattr(msg, "chat_id", None),
                getattr(msg, "id", None),
            )
            dto = _map_message(msg)
            for s in list(self._streams):
                await s.push(dto)
        except Exception:  # noqa: BLE001
            log.exception("updateMessageContent handling failed")

    async def _on_connection_state(self, client_self, update) -> None:
        """TDLib 网络连接状态(updateConnectionState)→ `ConnectionStateChanged` 事件。

        状态来源是 TDLib 推送的 `connectionState*` 对象(如 connectionStateReady);
        这是 UI 底部状态栏"与 TG 的通信状态"的最准信号 — 代理 / DC 不通时它只会
        停在 waiting_for_network / connecting,一眼可见。
        """
        try:
            state_obj = getattr(update, "state", None)
            state_type = ""
            if isinstance(state_obj, dict):
                state_type = state_obj.get("@type", "")
            elif state_obj is not None:
                state_type = getattr(state_obj, "@type", "")
            new_state = _CONN_STATE_MAP.get(state_type, "unknown")
            log.info("tdlib connection state → %s (%s)", new_state, state_type)
            if self._bus is not None:
                asyncio.create_task(self._safe_publish_conn_state(new_state))
        except Exception:  # noqa: BLE001
            log.exception("connection state handling failed")

    async def _safe_publish_conn_state(self, state: str) -> None:
        try:
            from tgmonitor.core.events import ConnectionStateChanged
            assert self._bus is not None
            await self._bus.publish(ConnectionStateChanged(state=state))
        except Exception:  # noqa: BLE001
            log.exception("publish ConnectionStateChanged failed")

    # ============================================================
    # Preflight & 启动
    # ============================================================

    async def _run_preflight(self) -> tuple[bool, str | None]:
        """启动 TDLib 前清理/探测。

        返回 (ok, error_detail) — 若 proxy 配置但不可达,error_detail 是给 UI 的
        简短描述;调用方应立即走 `_set_state("error", detail=...)` 不再 start。
        """
        td_dir = self._settings.session_dir / "tdlib"
        log.info(
            "start preflight: session_dir=%s | proxy=%s | td_dir_exists=%s",
            td_dir, self._settings.proxy, td_dir.exists(),
        )
        # stale lock / wal / shm 清理 — 不 raise,只 warn
        for lock in td_dir.rglob("*.lock"):
            log.warning("stale lock file found: %s", lock)
        for ext in ("-wal", "-shm", "-journal"):
            for stale in td_dir.rglob(f"*{ext}"):
                if stale.is_file():
                    try:
                        stale.unlink()
                        log.warning("removed stale sqlite artifact: %s", stale)
                    except OSError as exc:
                        log.warning("remove %s failed: %s", stale, exc)

        if self._settings.proxy:
            ok, msg = await _probe_proxy(self._settings.proxy)
            if not ok:
                return False, msg
        return True, None

    async def _do_start_inner(self) -> None:
        """不动 TdlibJsonClient.start()(它会 await authorize 然后 hang);
        手工复刻启动顺序,等我们自己的 _state_event 来推进。

        每一步都有耗时日志,启动卡在哪一步一眼能看出(4:30-5:00 排查场景)。
        """
        import time as _t
        t0 = _t.monotonic()
        # 启动 updates_loop + tdlib_json 内部 task(带崩溃自愈)
        self._schedule_updates_loop()
        self._running = True
        log.info("[tdlib] updates_loop task scheduled in %.3fs", _t.monotonic() - t0)
        t = _t.monotonic()
        await self.execute({"@type": "setLogVerbosityLevel", "new_verbosity_level": 0})
        log.info("[tdlib] SetLogVerbosityLevel in %.3fs", _t.monotonic() - t)
        # 走 base 的 _setup_proxy / _setup_options
        t = _t.monotonic()
        try:
            await self._setup_proxy()
        except TdlibError as e:
            detail = _extract_error_detail(e)
            log.error("[tdlib] _setup_proxy failed: %s", e)
            await self._kill_client()
            self._set_state("error", detail=f"代理设置失败: {detail}")
            return
        log.info("[tdlib] _setup_proxy in %.3fs", _t.monotonic() - t)
        t = _t.monotonic()
        await self._setup_options()
        log.info("[tdlib] _setup_options in %.3fs (options=None → no-op)", _t.monotonic() - t)
        # 发 getAuthorizationState 触发状态机 — 这是 fire-and-forget,
        # 响应是 `updateAuthorizationState`,会走 _on_authorization_state_update
        t = _t.monotonic()
        await self.send({"@type": "getAuthorizationState"})
        log.info("[tdlib] getAuthorizationState sent in %.3fs", _t.monotonic() - t)
        # 等状态机推进 — 首次推进常停在 `tdlib_parameters`(tdlib_json 自动发
        # setTdlibParameters 之后才继续)。给它 `_SETTLE_GRACE` 宽限:若 TDLib
        # 因 setTdlibParameters 被拒(api_id/api_hash 无效,code=400)而卡在
        # WaitTdlibParameters,seen error codes 会累积 — 转成可见错误,而不是
        # 让 UI 永远停在 `tdlib_parameters` 裸标签。
        t = _t.monotonic()
        log.info("[tdlib] waiting for state machine to advance (current=%s)…",
                 self._state)
        await self._state_event.wait()
        while self._state in self._BOOT_TRANSIENT_STATES:
            log.info("[tdlib] state=%s still transient, waiting to settle…",
                     self._state)
            self._state_event.clear()
            try:
                await asyncio.wait_for(
                    self._state_event.wait(), timeout=self._SETTLE_GRACE,
                )
            except TimeoutError:
                # 有 seen error codes → TDLib 明确拒绝了启动请求(如
                # api_id/api_hash 无效,code=400),立即转可见错误。
                if self._seen_error_codes:
                    err_detail = _translate_boot_error(
                        self._seen_error_codes, self._last_error_message,
                    )
                    log.error("[tdlib] stuck in %s: %s", self._state, err_detail)
                    await self._kill_client()
                    self._set_state("error", detail=err_detail)
                    return
                # 0 codes → TDLib 没拒绝,只是慢(冷启动连 DC / restore 半成品
                # session)。不杀,继续等状态推进;总预算由 `start()` 外层的
                # wait_for(_BOOT_TIMEOUT) 兜底。历史坑:这里单次 `_SETTLE_GRACE`
                # 超时就直接杀,30s 预算形同虚设,冷启动被 5s 误杀。
                log.info(
                    "[tdlib] state=%s no movement in %.0fs, no error codes — "
                    "keep waiting (boot budget %.0fs)",
                    self._state, self._SETTLE_GRACE, self._BOOT_TIMEOUT,
                )
        log.info("[tdlib] state machine advanced to %s in %.3fs",
                 self._state, _t.monotonic() - t)

    async def start(self) -> tuple[str, str | None]:
        """主入口 — 应用启动时调一次。

        流程:
          1) preflight (stale 文件 + SOCKS5 探测,失败立即给 UI)
          2) 跑 `_do_start_inner()`,超时 `_BOOT_TIMEOUT`
          3) 超时的话取最末的 tdlib_json 错误码:
             - 401 → 说明加密 key 错;返回 `("error", "encryption key 不匹配")`,
               把这个信息抛给 AppService,它负责 rotate key + 重建 client。
             - 别的(0 / timeout / proxy / DC 不通)→ `("error", "...具体原因...")`
          4) 成功 → 返回 `(_state, _state_detail)`
        """
        self._check_alive()
        if self._state == "ready":
            return self._state, self._state_detail
        ok, proxy_err = await self._run_preflight()
        if not ok:
            self._set_state("error", detail=proxy_err or "preflight failed")
            return self._state, self._state_detail

        # 清旧状态
        self._state_event.clear()
        self._seen_error_codes.clear()
        self._last_error_message = ""
        try:
            await asyncio.wait_for(
                self._do_start_inner(), timeout=self._BOOT_TIMEOUT,
            )
            log.info("start: settled on state=%s", self._state)
            return self._state, self._state_detail
        except TimeoutError:
            log.error(
                "start: timed out after %.0fs; seen_error_codes=%s",
                self._BOOT_TIMEOUT, list(self._seen_error_codes),
            )
            err_detail = _translate_boot_error(
                self._seen_error_codes, self._last_error_message,
            )
            await self._kill_client()
            self._set_state("error", detail=err_detail)
            return self._state, self._state_detail
        except Exception as e:  # noqa: BLE001
            log.exception("start: unexpected")
            await self._kill_client()
            self._set_state("error", detail=f"unexpected: {e}")
            return self._state, self._state_detail

    # ============================================================
    # 登入操作(被 AppService 调用)
    # ============================================================

    async def submit_phone(self, phone: str) -> tuple[str, str | None]:
        """用户点「登录」时调用 — 提交手机号给 TDLib,触发验证码下发。

        显式发 `setAuthenticationPhoneNumber`,不再依赖 tdlib_json 构造时的
        自动发号:那一路取的是 init 参数,用户后填 / 后改的手机号根本进不了
        调用链,且空号也会照发(fire-and-forget),导致卡在 `phone_required`
        而 UI 无任何反馈。错误 → `AuthErrorOccurred` 事件,不改顶层状态。
        """
        self._check_alive()
        if not getattr(self, "_running", False):
            return self._state, self._state_detail
        phone = (phone or "").strip()
        if not phone.startswith("+"):
            await self._publish_auth_error(
                "phone", "手机号需以 + 国家区号开头,如 +8613800000000",
            )
            return self._state, self._state_detail
        # 等 TDLib 进入 phone_required(最多 5s)
        if self._state != "phone_required":
            self._state_event.clear()
            try:
                await asyncio.wait_for(self._state_event.wait(), timeout=5.0)
            except TimeoutError:
                pass
        if self._state != "phone_required":
            log.warning(
                "submit_phone: state=%s,非 phone_required 无法发号", self._state,
            )
            return self._state, self._state_detail
        log.info("submitting phone %s → setAuthenticationPhoneNumber", phone)
        try:
            await self.request({
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
            }, request_timeout=30)
        except TdlibError as e:
            detail = _extract_error_detail(e)
            log.warning("submit_phone failed: %s", e)
            await self._publish_auth_error("phone", f"登录失败: {detail}", e)
            return self._state, self._state_detail
        # 等 TDLib 推进到 code_required(最多 15s)
        self._state_event.clear()
        try:
            await asyncio.wait_for(self._state_event.wait(), timeout=15.0)
        except TimeoutError:
            log.warning(
                "submit_phone: 发号后 15s 未推进 (state=%s)", self._state,
            )
        return self._state, self._state_detail

    async def submit_code(self, code: str) -> tuple[str, str | None]:
        """UI 提交验证码。

        把 code push 进队列(由 `_check_authentication_code` 钩子消费),然后等
        状态变更。错误经 `request()` → `AuthErrorOccurred` 事件传出。
        """
        self._check_alive()
        await self._code_queue.put(code)
        self._state_event.clear()
        try:
            await asyncio.wait_for(self._state_event.wait(), timeout=15.0)
        except TimeoutError:
            log.warning("submit_code: timeout (state=%s)", self._state)
        return self._state, self._state_detail

    async def submit_password(self, password: str) -> tuple[str, str | None]:
        """UI 提交 2FA 密码。push 进队列 + 等状态变更;错误经 `AuthErrorOccurred` 传出。"""
        self._check_alive()
        await self._password_queue.put(password)
        self._state_event.clear()
        try:
            await asyncio.wait_for(self._state_event.wait(), timeout=15.0)
        except TimeoutError:
            log.warning("submit_password: timeout (state=%s)", self._state)
        return self._state, self._state_detail

    async def logout(self) -> None:
        """登出 — TDLib 会自动反推状态机 Closed → PhoneNumber。"""
        self._check_alive()
        try:
            await self.request({"@type": "logOut"})
        except Exception:  # noqa: BLE001
            log.exception("logout failed")
        self._me = None

    # ============================================================
    # 限流 / Flood Wait 处理(ChannelSyncService 用)
    # ============================================================

    @staticmethod
    def _translate_rate_limit(exc: BaseException) -> TelegramRateLimitError | None:
        """把 tdlib_json 抛的 TdlibError / 含 FLOOD_WAIT 的 Error 归一。

        返回:
          - TelegramRateLimitError(retry_after=...) 如果识别为限流
          - None 否则(原异常往外抛)
        """
        # code=429 是限流的官方 code
        code = getattr(exc, "code", None)
        if code == 429:
            ra = getattr(exc, "retry_after", None)
            if isinstance(ra, (int, float)) and ra > 0:
                return TelegramRateLimitError(float(ra))
            # 没给 retry_after 给个保守 60s
            return TelegramRateLimitError(60.0)
        # 字符串里 "FLOOD_WAIT_NNN" 也算(TdlibError 某些场景 code 不是 429)
        msg = _extract_error_detail(exc)
        import re as _re
        m = _re.search(r"FLOOD_WAIT[_ ](\d+)", msg)
        if m:
            return TelegramRateLimitError(float(m.group(1)))
        return None

    # ============================================================
    # 清理
    # ============================================================

    async def _kill_client(self) -> None:
        """完整杀掉内部的 tdlib_json 客户端状态机 — 给 start 超时 / 出错用。
        之后想再启动需要重建整个 Client 实例(由 AppService 负责)。
        """
        if not getattr(self, "_running", False):
            return
        try:
            await self.stop()
        except Exception:  # noqa: BLE001
            log.exception("stop() failed")
        update_task = getattr(self, "_update_task", None)
        if update_task is not None and not update_task.done():
            update_task.cancel()
            try:
                await update_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._running = False
        # drain 输入队列 — 避免旧的 code/pwd 留在里面被下个 session 错读
        while not self._code_queue.empty():
            try:
                self._code_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        while not self._password_queue.empty():
            try:
                self._password_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def nuke_and_rebuild(self, rotate_key: bool = False) -> None:
        """清掉 session db + (可选) 旋转加密 key + 杀掉内部 tdlib_json 客户端。
        调用方负责后续重新构造本对象。"""
        td_dir = self._settings.session_dir / "tdlib"
        await self._kill_client()
        import shutil as _sh
        for sub in ("database", "files", ".aiotdlib"):
            target = td_dir / sub
            if target.exists():
                try:
                    if target.is_dir():
                        _sh.rmtree(target)
                    else:
                        target.unlink()
                    log.warning("nuked %s", target)
                except OSError as exc:
                    log.warning("nuke %s failed: %s", target, exc)
        if rotate_key:
            # 同时让下次构造时拿新 key
            _load_or_create_encryption_key(td_dir, rotate=True)
        self._set_state(
            "phone_required",
            detail="本地会话已重置,请重新登录",
        )

    async def close(self) -> None:
        """app exit 时调 — 内部 tdlib_json 客户端 + 关掉所有订阅流。

        第一件事就 `_closing=True`,让任何还在 in-flight 的协程
        (`list_joined_channels` / VM refresh / 等)下次 tick 看到
        `ClientClosingError`,不再排新的 request 进 10s 超时。
        """
        self._closing = True
        # 关流
        for s in list(self._streams):
            try:
                await s.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._streams.clear()
        await self._kill_client()

    def _check_alive(self) -> None:
        """公共 async 方法的 entry guard。

        顺序:在进入 tdlib_json bridge 之前 throw `ClientClosingError`,
        不让请求排进 10s 超时。**只用于事务性方法**(submit_* / logout);
        best-effort 方法(`list_joined_channels`)自己处理并 return 占位。
        """
        if self._closing:
            raise ClientClosingError()

    async def _wait_for_state(self, target: str, *, timeout: float) -> None:  # noqa: ASYNC109 — `timeout` 是 state-machine 推进的最大等待时间,不是 asyncio.wait_for 的语义;命名直白可用
        """等 `_state` 推进到 `target`(或超时)。

        等价于 Python `Event.wait()`,但 `Event` 是 **set-only** —— 一旦 set,
        后续 `wait()` 立即返回(不真 yield),所以纯 `wait()/wait_for()` polling
        会**spin**(每轮都 await 一次已经 done 的 Future,次数能跑到 1000/s,
        吃满 qasync loop,Qt 事件没机会 pump,UI 冻死)。

        正确做法:
          1) event 已 set 时**主动 `asyncio.sleep(0.05)`** 让出 CPU,
             重新 poll `_state` — 因为我们要等的是"状态变化",不是"event set";
          2) event 未 set 时 `wait_for(state_event.wait(), 0.5)` 真等 fire。
        """
        import time as _t
        deadline = _t.monotonic() + timeout
        while _t.monotonic() < deadline:
            if self._state == target:
                return
            if self._state_event.is_set():
                # event 早就被前面的 _set_state(...) set 过了 — 主动 yield,
                # 然后重新读 self._state。因为我们等的是"状态变成 target",
                # 不是"event set"。
                await asyncio.sleep(0.05)
                continue
            try:
                await asyncio.wait_for(self._state_event.wait(), timeout=0.5)
            except TimeoutError:
                continue
        if self._state == target:
            return
        raise TimeoutError(f"state did not reach {target!r} within {timeout}s")

    @property
    def state(self) -> str:
        """当前顶层状态(继承自 TelegramClient Protocol)。"""
        return self._state

    @property
    def me(self) -> dict | None:
        """当前登录用户 {id, username, first_name};未登录 None。"""
        return self._me

    # ---- 频道(delegate → ChannelsApi,2026-08-02 抽出)----
    # 真实实现在 `tdlib_channels.py:ChannelsApi`。这里 6 个 thin delegate
    # 保持 `TelegramClient` Protocol 形状不变,caller 不需要知道 composition。

    async def get_channel_metadata(self, channel_id: int) -> ChannelDTO:
        """Delegate → ChannelsApi.get_channel_metadata(Protocol 形状保留)。"""
        return await self.channels.get_channel_metadata(channel_id)

    async def list_joined_channels(self) -> list[ChannelDTO]:
        """Delegate → ChannelsApi.list_joined_channels(Protocol 形状保留)。"""
        return await self.channels.list_joined_channels()

    def iter_chat_history(
        self,
        channel_id: int,
        *,
        before_msg_id: int = 0,
        limit: int = 100,
    ) -> AsyncIterator[MessageDTO]:
        """Delegate → ChannelsApi.iter_chat_history(Protocol 形状保留)。

        async generator 必须直接 `return`,不能包 `async def yield`(破坏 lazy)。
        """
        # async generator 必须直接返回,不能包 `async def yield`(会破坏 lazy 语义)
        return self.channels.iter_chat_history(
            channel_id, before_msg_id=before_msg_id, limit=limit,
        )

    async def join_channel(self, identifier: str) -> ChannelDTO:
        """Delegate → ChannelsApi.join_channel(Protocol 形状保留)。"""
        return await self.channels.join_channel(identifier)

    async def download_file(self, file_id: str) -> bytes | None:
        """Delegate → ChannelsApi.download_file(Protocol 形状保留)。"""
        return await self.channels.download_file(file_id)

    def subscribe_updates(self) -> UpdateStream:
        """订阅实时更新流(由 tdlib_json push);`aclose` 必调一次,否则 list 只增不减。"""
        s = _TdlibJsonUpdateStream(on_close=self._remove_stream)
        self._streams.append(s)
        return s

    def _remove_stream(self, s: _TdlibJsonUpdateStream) -> None:
        """`_TdlibJsonUpdateStream.close()` 回调 — 拿掉自己,避免 list 只增不减。

        `close()` 路径里仍然做全量清空(兜底),此回调是常规路径。
        """
        try:
            self._streams.remove(s)
        except ValueError:
            pass  # close() 已清空,忽略
