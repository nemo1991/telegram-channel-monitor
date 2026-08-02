"""TDLib 实现 — 通过 `aiotdlib` 封装。

- 业务侧只见 `TelegramClient` Protocol;此文件**唯一**接触 TDLib
- 鉴权:实际接 `aiotdlib` 的内部状态机(不假装 ready)
  - 用 `asyncio.Queue` 把 UI 提交的 code / 2FA 密码注入 aiotdlib 的钩子
  - 通过 `updateAuthorizationState` 事件把 TDLib 真实状态转 `LoginStateChanged` 推给 UI
- 实时更新:`updateNewMessage` → DTO → UI

模块拆分(2026-08-02):本文件保留 **lifecycle controller**(aiotdlib.Client 子类化
+ 信号绑 + state machine + channels 子块);下列 pure helpers 抽到独立模块:
  - `tdlib_errors.py` — `_extract_error_detail` / `TelegramRateLimitError` /
    `ClientClosingError`
  - `tdlib_proxy.py` — `parse_socks5_proxy` / `_load_or_create_encryption_key` /
    `_probe_proxy` / `_translate_boot_error` / `_AUTH_STATE_MAP`
  - `tdlib_messages.py` — `_map_message` + 媒体 / service 派发表

依赖:`aiotdlib >= 0.27`(旧版直接 kwargs 调用有备选路径)。
"""
from __future__ import annotations

import asyncio
import collections
import logging
from typing import Any, AsyncIterator, Callable

log = logging.getLogger(__name__)

try:
    from aiotdlib import Client as _AiClient  # type: ignore
    from aiotdlib.api import (  # type: ignore
        API,
        BaseObject,
        CheckAuthenticationCode,
        CheckAuthenticationPassword,
        DownloadFile,
        GetAuthorizationState,
        GetBasicGroup,
        GetChat,
        GetChatHistory,
        GetChats,
        GetFile,
        GetSupergroup,
        JoinChat,
        LogOut,
        SearchPublicChat,
        SetLogVerbosityLevel,
    )
    try:
        from aiotdlib.api.error import AioTDLibError  # type: ignore
    except Exception:  # noqa: BLE001
        AioTDLibError = Exception  # type: ignore[misc,assignment]  # fallback so the except clause still type-checks
    try:
        # aiotdlib 0.27+:
        from aiotdlib.client_settings import ClientSettings  # type: ignore
    except Exception:  # noqa: BLE001
        ClientSettings = None  # type: ignore[assignment]
    _HAVE_AIOTDLIB = True
except Exception:  # noqa: BLE001
    _HAVE_AIOTDLIB = False
    ClientSettings = None  # type: ignore[assignment]

from tgmonitor.core.config import Settings  # noqa: E402 — aiotdlib import 上方有 try/except 守卫
from tgmonitor.core.dto import ChannelDTO, MessageDTO  # noqa: E402
from tgmonitor.core.telegram.client import UpdateStream  # noqa: E402
from tgmonitor.core.telegram.tdlib_errors import (  # noqa: E402
    ClientClosingError,
    TelegramRateLimitError,
    _extract_error_detail,
)
from tgmonitor.core.telegram.tdlib_messages import _map_message  # noqa: E402
from tgmonitor.core.telegram.tdlib_proxy import (  # noqa: E402
    _AUTH_STATE_MAP,
    _load_or_create_encryption_key,
    _probe_proxy,
    _translate_boot_error,
    parse_socks5_proxy,
)


class _AiotdlibUpdateStream(UpdateStream):
    """aiotdlib → asyncio.Queue → UI。

    `aclose()` 时除了塞 sentinel 让 async generator 退出,还会触发 caller
    注册的 `on_close` callback — 用来把自己从 `client._streams` 拿掉,
    避免长会话列表只增不减(契约见 `client.UpdateStream` 文档)。
    """

    def __init__(self, on_close: Callable[[_AiotdlibUpdateStream], None] | None = None) -> None:
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
    """生产实现 — 子类 aiotdlib.Client,把鉴权输入接入我们的队列。

    v0.3 重写后的设计原则(详见 plan /Users/forcetone/.claude/plans/...):
      - `_set_state` 是**唯一**写 `self._state` 的入口,所有路径(包括
        `_on_authorization_state_update` / `_nuke_session_and_reinit` /
        `logout`)都过它。任何地方都不许直接 `self._state = ...`。
      - `_state_event: asyncio.Event` 是 `_set_state` 同步 set 的,
        `start()` / `submit_phone()` / `submit_code()` / `submit_password()`
        都 await 这个事件 — 取代旧的 polling loop。
      - 启动不用 `aiotdlib.Client.start()`(它会 `await self.authorize()` 然后
        因为 `_authorized_event` 没人 set 而永久挂起)。改用手工复刻的
        `_do_start_inner()` 直接驱动 updates_loop。
      - submit 错误用 `request()` 抛 `AioTDLibError`,捕获后通过
        `AuthErrorOccurred` 事件通知 UI,**不**改顶层状态(让用户原地重试)。
      - 401 等"key 不匹配"由 boot 超时 + rotate_key=True 解决:
        调用方(`AppService`)负责在检测到超时 + 401 标记后重建 client。
    """

    # boot() 阶段允许 start 阻塞的最大秒数;过了就当 session 损坏,调用方应重建。
    # SOCKS5 代理冷启动通常 15-25s 才连上 DC,留 30s 余量。
    _BOOT_TIMEOUT = 30.0

    def __init__(self, settings: Settings, *, event_bus: Any | None = None) -> None:
        if not _HAVE_AIOTDLIB:
            raise RuntimeError("aiotdlib 未安装:`pip install -U aiotdlib>=0.27`")
        self._settings = settings
        self._me: dict | None = None
        self._streams: list[_AiotdlibUpdateStream] = []
        self._chat_titles: dict[int, str] = {}
        self._chat_usernames: dict[int, str] = {}
        self._bus = event_bus
        # 鉴权输入队列(super().__init__ 之前必须建好 — aiotdlib 内部某些路径会读)
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

        # aiotdlib 把 fire-and-forget 的 send() 结果当作 silently dropped 的
        # 错误处理(因为没有 request_id → _handle_pending_request 查不到对应
        # pending request)。但我们的 _updates_loop 仍会看到一个 Error 包,
        # 它会进 `_handle_update` 派发。我们用一个 add_event_handler("*")
        # 兜底,把所有 aiotdlib 内 Error 包的 code 收集起来,这样可以在
        # `start` 超时时判断是不是 "401 wrong encryption key"。
        self._seen_error_codes: collections.deque[int] = collections.deque(maxlen=20)

        # 关闭流程标志位 —— `close()` 入口处立刻 True,所有公共 async 方法
        # 通过 `_check_alive()` 拦截后续 entry。`best-effort` 方法
        # (如 `list_joined_channels`)自己 catch;事务性方法让它冒到调用方。
        # 同时阻断启动后 race:`start()` 还没 ready 时 VM 已 fire-and-forget
        # 调 `list_joined_channels`,aiotdlib bridge 还没绑到当前 loop 上,
        # `request()` 会撞 10s 超时 + qasync 跨 loop wakeup 噪音。
        self._closing: bool = False

        proxy = parse_socks5_proxy(settings.proxy)
        # tdlib_verbosity 决定 aiotdlib 把多少 TDLib 内部日志转发到 Python logging。
        # 默认 FATAL;调试时调到 INFO 可见 401 等线索。
        verbosity = int(getattr(settings, "tdlib_verbosity", 0) or 0)
        settings_kwargs: dict[str, Any] = dict(
            api_id=settings.api_id,
            api_hash=settings.api_hash,
            phone_number=settings.phone,
            database_encryption_key=_load_or_create_encryption_key(
                self._settings.session_dir / "tdlib"
            ),
            files_directory=str(settings.session_dir / "tdlib"),
            library_path=None,
            tdlib_verbosity=verbosity,
        )
        if proxy is not None:
            settings_kwargs["proxy_settings"] = proxy
        # aiotdlib 默认 ClientOptions 会批量下发 disable_top_chats /
        # ignore_inline_thumbnails / ignore_background_updates 等开关,
        # 但部分选项受 TDLib "can be set only if can_<X> is true" 规则约束,
        # 在 user account + 默认安全设置下会被 TDLib 拒(返回 code=400
        # "Option can't be set"),日志里冒两条 WARNING。
        # 我们没有需要覆盖的选项 → 关掉,只发 tdlib_parameters + proxy。
        settings_kwargs["options"] = None
        if ClientSettings is not None:
            super().__init__(settings=ClientSettings(**settings_kwargs))  # type: ignore[arg-type]
        else:  # pragma: no cover
            super().__init__(**settings_kwargs)  # type: ignore[call-overload]

        # aiotdlib 用同步事件总线的事件(updateNewMessage)走 add_event_handler;
        # updateAuthorizationState 走我们 override 的 _on_authorization_state_update
        # (aiotdlib 的 _updates_loop 自己截胡)。
        self.add_event_handler(
            self._on_new_message,
            update_type=API.Types.UPDATE_NEW_MESSAGE,
        )
        # 全局 catch:任何 update 进来都看一眼,把 Error 包的 code 记录下来。
        # aiotdlib 0.27+ 用 `await handler(self, update)` 调用,所以必须 (self, update)。
        async def _on_any_update(client_self, update) -> None:
            try:
                from aiotdlib.api.types import Error as _Err
                if isinstance(update, _Err):
                    code = getattr(update, "code", None)
                    if isinstance(code, int):
                        self._seen_error_codes.append(code)
                        log.warning("aiotdlib Error observed: code=%s msg=%s",
                                    code, getattr(update, "message", ""))
            except Exception:  # noqa: BLE001
                pass
        self.add_event_handler(
            _on_any_update,
            update_type=API.Types.ANY,
        )

    # ============================================================
    # 状态管理: 唯一写路径
    # ============================================================

    def _set_state(self, new_state: str, *, detail: str = "") -> None:
        """唯一允许写 `self._state` 的入口。所有路径(aiotdlib 状态推送、
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
    # aiotdlib 钩子 override
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
          2. 调 `request_factory(value)` 跑 aiotdlib Check 函数
          3. AioTDLibError → 转 detail,发 `AuthErrorOccurred` (不 raise)
          4. 其它 Exception → log.exception + 发 `AuthErrorOccurred`

        Args:
          - `source`:AuthErrorOccurred.source("code" / "password")
          - `queue`:asyncio.Queue,等 UI 提交(`_code_queue` / `_password_queue`)
          - `request_factory`:用 value 构造 aiotdlib Request(CheckAuthenticationCode / Password)
          - `error_label`:日志标签(e.g. "CheckAuthenticationCode")
          - `detail_prefix`:错误文案前缀(e.g. "验证码错误: " / "2FA 密码错误: ")
        """
        value = await queue.get()
        log.info("submitting %s (len=%d)", source, len(value))
        try:
            await self.request(request_factory(value), request_timeout=30)
        except AioTDLibError as e:
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
        但不 raise(让 aiotdlib 自动重新发 WaitCode,用户原地重输)。"""
        await self._submit_auth_step(
            source="code",
            queue=self._code_queue,
            request_factory=lambda code: CheckAuthenticationCode(code=code),
            error_label="CheckAuthenticationCode",
            detail_prefix="验证码错误: ",
        )

    async def _check_authentication_password(self) -> None:
        """2FA 密码注入。"""
        await self._submit_auth_step(
            source="password",
            queue=self._password_queue,
            request_factory=lambda pwd: CheckAuthenticationPassword(password=pwd),
            error_label="CheckAuthenticationPassword",
            detail_prefix="2FA 密码错误: ",
        )

    async def _on_authorization_state_update(self, authorization_state) -> None:
        """aioTDLib 的 `_updates_loop` 自己截胡 `UpdateAuthorizationState`,直接
        调我们这个方法(不走 add_event_handler)。所以唯一写法是 override。

        aioTDLib 鉴权状态机的两个已知缺陷:
        - 用 `send()`(fire-and-forget)发 SetTdlibParameters / Check* — 错误
          (例如 `Error code=401`)响应被静默丢,因为没有 request_id。
        - 我们 override 后**必须** `await super()`,否则 aiotdlib 自己不会调
          `_check_authentication_code` 等后续钩子。
        """
        try:
            state_id = (
                getattr(authorization_state, "ID", None)
                or type(authorization_state).__name__
            )
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
            # 后续 update 派发。这只是兜底 log,aioTDLib 自己的 send() 错误
            # 因为没有 request_id 不会进到这里;真到这里说明 aiotdlib 内部
            # 出问题,例如 _set_authentication_phone_number 异常。
            log.exception("super _on_authorization_state_update failed (suppressed)")

    async def _on_new_message(self, client_self, update: BaseObject) -> None:
        """aiotdlib 0.27 用 `await handler(self, update)` 调用 handler,所以签名
        必须是 `(client, update)`,这里 `client_self` 其实是 client 实例本身
        (我们就是它),所以丢弃。"""
        try:
            msg = getattr(update, "message", None)
            if msg is None:
                return
            dto = _map_message(msg)
            for s in list(self._streams):
                await s.push(dto)
        except Exception:  # noqa: BLE001
            log.exception("updateNewMessage handling failed")

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
        """不动 aiotdlib.Client.start()(它会 await authorize 然后 hang);
        手工复刻启动顺序,等我们自己的 _state_event 来推进。

        每一步都有耗时日志,启动卡在哪一步一眼能看出(4:30-5:00 排查场景)。
        """
        import time as _t
        t0 = _t.monotonic()
        # 启动 updates_loop + aiotdlib 内部 task
        self._update_task = asyncio.create_task(self._updates_loop())
        self._running = True
        log.info("[tdlib] updates_loop task scheduled in %.3fs", _t.monotonic() - t0)
        t = _t.monotonic()
        await self.execute(SetLogVerbosityLevel(new_verbosity_level=0))  # 暂时无所谓
        log.info("[tdlib] SetLogVerbosityLevel in %.3fs", _t.monotonic() - t)
        # 走 base 的 _setup_proxy / _setup_options
        t = _t.monotonic()
        await self._setup_proxy()
        log.info("[tdlib] _setup_proxy in %.3fs", _t.monotonic() - t)
        t = _t.monotonic()
        await self._setup_options()
        log.info("[tdlib] _setup_options in %.3fs (options=None → no-op)", _t.monotonic() - t)
        # 发 GetAuthorizationState 触发状态机 — 这是 fire-and-forget,
        # 响应是 `updateAuthorizationState`,会走 _on_authorization_state_update
        t = _t.monotonic()
        await self.send(GetAuthorizationState())
        log.info("[tdlib] GetAuthorizationState sent in %.3fs", _t.monotonic() - t)
        # 等状态机推进 — 任何非 bo 状态都意味着启动成功
        t = _t.monotonic()
        log.info("[tdlib] waiting for state machine to advance (current=%s)…",
                 self._state)
        await self._state_event.wait()
        log.info("[tdlib] state machine advanced to %s in %.3fs",
                 self._state, _t.monotonic() - t)

    async def start(self) -> tuple[str, str | None]:
        self._check_alive()
        """主入口 — 应用启动时调一次。

        流程:
          1) preflight (stale 文件 + SOCKS5 探测,失败立即给 UI)
          2) 跑 `_do_start_inner()`,超时 `_BOOT_TIMEOUT`
          3) 超时的话取最末的 aiotdlib 错误码:
             - 401 → 说明加密 key 错;返回 `("error", "encryption key 不匹配")`,
               把这个信息抛给 AppService,它负责 rotate key + 重建 client。
             - 别的(0 / timeout / proxy / DC 不通)→ `("error", "...具体原因...")`
          4) 成功 → 返回 `(_state, _state_detail)`
        """
        if self._state == "ready":
            return self._state, self._state_detail
        ok, proxy_err = await self._run_preflight()
        if not ok:
            self._set_state("error", detail=proxy_err or "preflight failed")
            return self._state, self._state_detail

        # 清旧状态
        self._state_event.clear()
        self._seen_error_codes.clear()
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
            err_detail = _translate_boot_error(self._seen_error_codes)
            await self._kill_aiotdlib()
            self._set_state("error", detail=err_detail)
            return self._state, self._state_detail
        except Exception as e:  # noqa: BLE001
            log.exception("start: unexpected")
            await self._kill_aiotdlib()
            self._set_state("error", detail=f"unexpected: {e}")
            return self._state, self._state_detail

    # ============================================================
    # 登入操作(被 AppService 调用)
    # ============================================================

    async def submit_phone(self, phone: str) -> tuple[str, str | None]:
        self._check_alive()
        """用户点「登录」时调用 — 改 phone / 触发 aiotdlib 发 code。

        若 TDLib 没在 `phone_required` 状态,先 wait_for 至转好。
        """
        if not getattr(self, "_running", False):
            return self._state, self._state_detail
        if phone and phone != self._settings.phone:
            log.warning(
                "phone changed (%s → %s); restart to take effect",
                self._settings.phone, phone,
            )
        # 等进 phone_required 后 aiotdlib 会自动处理 — 这里不强发请求。
        # 已存在的 phone 在 init 时已传给 ClientSettings,aioTDLib 会发。
        # 我们的钩子 _set_authentication_phone_number_or_check_bot_token
        # 会自动 SetAuthenticationPhoneNumber。
        if self._state != "phone_required":
            # 等状态变成 phone_required(最多 5s)
            self._state_event.clear()
            try:
                await asyncio.wait_for(
                    self._state_event.wait(), timeout=5.0,
                )
            except TimeoutError:
                pass
        return self._state, self._state_detail

    async def submit_code(self, code: str) -> tuple[str, str | None]:
        self._check_alive()
        """UI 提交验证码。

        把 code push 进队列(由 `_check_authentication_code` 钩子消费),然后等
        状态变更。错误经 `request()` → `AuthErrorOccurred` 事件传出。
        """
        await self._code_queue.put(code)
        self._state_event.clear()
        try:
            await asyncio.wait_for(self._state_event.wait(), timeout=15.0)
        except TimeoutError:
            log.warning("submit_code: timeout (state=%s)", self._state)
        return self._state, self._state_detail

    async def submit_password(self, password: str) -> tuple[str, str | None]:
        self._check_alive()
        await self._password_queue.put(password)
        self._state_event.clear()
        try:
            await asyncio.wait_for(self._state_event.wait(), timeout=15.0)
        except TimeoutError:
            log.warning("submit_password: timeout (state=%s)", self._state)
        return self._state, self._state_detail

    async def logout(self) -> None:
        self._check_alive()
        """登出 — aiotdlib 会自动反推状态机 Closed → PhoneNumber。"""
        try:
            await self.request(LogOut())
        except Exception:  # noqa: BLE001
            log.exception("logout failed")
        self._me = None

    # ============================================================
    # 限流 / Flood Wait 处理(ChannelSyncService 用)
    # ============================================================

    @staticmethod
    def _translate_rate_limit(exc: BaseException) -> TelegramRateLimitError | None:
        """把 aiotdlib 抛的 AioTDLibError / 含 FLOOD_WAIT 的 Error 归一。

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
        # 字符串里 "FLOOD_WAIT_NNN" 也算(aiotdlib 某些版本 code 不是 429)
        msg = _extract_error_detail(exc)
        import re as _re
        m = _re.search(r"FLOOD_WAIT[_ ](\d+)", msg)
        if m:
            return TelegramRateLimitError(float(m.group(1)))
        return None

    # ============================================================
    # 清理
    # ============================================================

    async def _kill_aiotdlib(self) -> None:
        """完整杀掉内部的 aiotdlib 状态机 — 给 start 超时 / 出错用。
        之后想再启动需要重建整个 Client 实例(由 AppService 负责)。
        """
        if not getattr(self, "_running", False):
            return
        try:
            await self.stop()
        except Exception:  # noqa: BLE001
            log.exception("aiotdlib stop() failed")
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
        """清掉 session db + (可选) 旋转加密 key + 杀掉内部 aiotdlib。
        调用方负责后续重新构造本对象。"""
        td_dir = self._settings.session_dir / "tdlib"
        await self._kill_aiotdlib()
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
        """app exit 时调 — 内部 aiotdlib + 关掉所有订阅流。

        第一件事就 `_closing=True`,让任何还在 in-flight 的协程
        (`list_joined_channels` / VM refresh / 等)下次 tick 看到
        `ClientClosingError`,不再排新的 aiotdlib request 进 10s 超时。
        """
        self._closing = True
        # 关流
        for s in list(self._streams):
            try:
                await s.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._streams.clear()
        await self._kill_aiotdlib()

    def _check_alive(self) -> None:
        """公共 async 方法的 entry guard。

        顺序:在进入 aiotdlib bridge 之前 throw `ClientClosingError`,
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
        return self._state

    @property
    def me(self) -> dict | None:
        return self._me

    # ---- 频道 ----

    async def _resolve_channel_metadata(self, chat_id: int) -> ChannelDTO | None:
        """GetChat + GetSupergroup/GetBasicGroup 拿完整元数据。

        修 `tdlib_client.py:818-819` 旧 bug:`getattr(chat, "username", None)`
        永远拿不到 — `Chat` 类型没 username / member_count,这些在
        `Supergroup` / `BasicGroup` 上。
        """
        from aiotdlib.api import (
            ChatTypeBasicGroup,
            ChatTypeSupergroup,
        )

        chat = await self.request(GetChat(chat_id=chat_id))
        if chat is None:
            return None
        ct = getattr(chat, "type_", None) or getattr(chat, "type", None)
        title = chat.title
        if isinstance(ct, ChatTypeSupergroup):
            is_channel = bool(getattr(ct, "is_channel", False))
            kind = "channel" if is_channel else "supergroup"
            sg = await self.request(
                GetSupergroup(supergroup_id=ct.supergroup_id)
            )
            username = None
            member_count = None
            if sg is not None:
                usernames = getattr(sg, "usernames", None)
                if usernames is not None:
                    active = getattr(usernames, "active_usernames", None) or []
                    if active:
                        username = active[0]
                mc = getattr(sg, "member_count", None)
                if isinstance(mc, int) and mc > 0:
                    member_count = mc
            return ChannelDTO(
                id=chat_id, title=title, username=username, kind=kind,
                member_count=member_count,
            )
        if isinstance(ct, ChatTypeBasicGroup):
            bg = await self.request(
                GetBasicGroup(basic_group_id=ct.basic_group_id)
            )
            member_count = None
            if bg is not None:
                mc = getattr(bg, "member_count", None)
                if isinstance(mc, int) and mc > 0:
                    member_count = mc
            return ChannelDTO(
                id=chat_id, title=title, username=None, kind="basic_group",
                member_count=member_count,
            )
        return None  # private / secret — 同步功能不覆盖

    async def get_channel_metadata(self, channel_id: int) -> ChannelDTO:
        self._check_alive()
        """ChannelSyncService 用:拉一个频道的最新元数据。"""
        dto = await self._resolve_channel_metadata(channel_id)
        if dto is None:
            # 私有/secret 或 chat 不存在,fallback 给个 stub
            return ChannelDTO(id=channel_id, title=f"#{channel_id}")
        return dto

    async def list_joined_channels(self) -> list[ChannelDTO]:
        # best-effort UX:被 VM `_go` 在三种时机 fire-and-forget 调用:
        #   1) close() 中途
        #   2) startup 时 bridge 还没 ready(VM 的 `bootstrap_ui` 在
        #      `app.bootstrap()` 完成前后 fire 了 `list_*`,
        #      但 bridge/_state="ready" 还没等到 — 真打开 app 时撞这个)
        #   3) LoginStateChanged 转 ready 后 VM 再拉一次
        # 这三种情况都"安静走",不撞 aiotdlib 10s request_timeout,
        # 让 VM 自然 idle,等下次 LoginStateChanged 或用户点 Refresh 再触发。
        #
        # 关键(2026-07-18 修复):之前 `if self._state != "ready": return []`
        # 立即返回,但**bootstrap race 路径下**老版本会错过稍后才到的 "ready":
        #   - `start()` 等的是 `_state_event.wait()`,任何状态变化都 set,
        #     所以 aiotdlib 触发 `updateAuthorizationState(WaitTdlibParameters)`
        #     就可能让 start() 提前返(state="tdlib_parameters")
        #   - VM.bootstrap_ui 紧接着 fire list_joined_channels
        #   - guard 看到 state != "ready" → 立即 [],错过 200ms 后到的 "ready"
        #   - channels 永不显示,直到用户手动 Refresh
        # 现在改成"非 ready 时短暂等待再判"。
        if self._closing:
            log.info("[tdlib] list_joined_channels: client closing, returning []")
            return []
        if self._state != "ready":
            # 等 ≤ N 秒让 aiotdlib 完成从 Wait* → Ready 的过渡
            # 仍 best-effort:超过 N 秒还没 ready(网络挂了/401/...)就 []
            try:
                await self._wait_for_state("ready", timeout=8.0)
            except TimeoutError:
                log.debug(
                    "[tdlib] list_joined_channels: state=%r (未到 ready,8s 超时)",
                    self._state,
                )
                return []
            if self._state != "ready":
                return []
        import time as _t
        t0 = _t.monotonic()
        result: list[ChannelDTO] = []
        try:
            t = _t.monotonic()
            chats = await self.request(GetChats(limit=200))  # type: ignore[arg-type]
            log.info("[tdlib] GetChats(limit=200) returned %d ids in %.3fs",
                     len(chats.chat_ids) if chats and chats.chat_ids else 0,
                     _t.monotonic() - t)
            if chats is None:
                return result
            async for dto in self._iter_resolved_chats(
                chats.chat_ids or [], t0,
            ):
                result.append(dto)
        except ClientClosingError:
            # mid-loop 命中 `_check_alive()` —— 用户关窗 / 重启触发了 close(),
            # 静默退出,不再打 traceback
            log.info("[tdlib] list_joined_channels: aborted (client closing)")
        except Exception:  # noqa: BLE001
            log.exception("list_joined_channels failed")
        log.info("[tdlib] list_joined_channels done: %d channels in %.2fs",
                 len(result), _t.monotonic() - t0)
        return result

    async def _iter_resolved_chats(
        self,
        chat_ids: list[int],
        t0: float,
    ) -> AsyncIterator[ChannelDTO]:
        """把 GetChats 拿到的 chat_id 列表逐个解析成 ChannelDTO。

        抽出来是为了:
          1. `list_joined_channels` 只剩 lifecycle guard + GetChats + 聚合,
             单方法 30 行以里,可读;
          2. 单条解析失败 / mid-loop close 是迭代器的事(每个 yield 一个 DTO),
             caller 专心 aggregate。

        边界:
          - `_check_alive()` 中途命中 → 抛 ClientClosingError(让 caller 静默 catch);
          - 单条 `_resolve_channel_metadata` 失败 → log + skip(不影响其他 cid);
          - `_resolve_channel_metadata` 返 None(private / secret chat)→ skip;
          - n_total >= 50 时每 50 条打一次 progress(debug 友好)。
        """
        import time as _t

        n_total = len(chat_ids)
        for i, cid in enumerate(chat_ids):
            # 每个 cid 解析前再 check 一次 —— 拉 mid-loop 时已经被 close()
            # 也不要把这条请求继续排进 aiotdlib bridge
            self._check_alive()
            try:
                dto = await self._resolve_channel_metadata(cid)
            except ClientClosingError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("_resolve_channel_metadata(%d) failed", cid)
                continue
            if dto is None:
                continue
            if n_total >= 50 and (i + 1) % 50 == 0:
                log.info("[tdlib] list_joined_channels progress %d/%d in %.2fs",
                         i + 1, n_total, _t.monotonic() - t0)
            yield dto

    # ---- 历史消息分页(全量同步用) ----

    async def iter_chat_history(
        self,
        channel_id: int,
        *,
        before_msg_id: int = 0,
        limit: int = 100,
    ) -> AsyncIterator[MessageDTO]:
        """分页拉取频道历史消息(向旧方向递减)。

        before_msg_id=0 → 拉最新 N 条;>0 → 从该 id 之前(更早)开始续拉。
        TDLib `GetChatHistory.from_message_id` 只支持向旧方向翻页,所以传
        参语义是"截止这条之前",而非"从这条之后"。翻页游标 = 本批最小 id。
        限流:每页间不 sleep(由调用方 ChannelSyncService 控)。
        """
        from tgmonitor.core.telegram.tdlib_messages import _map_message
        # Async generator:`_check_alive()` 在每次分页入口 throw,中途 close() 就
        # 立刻结束迭代(不再排下一页 GetChatHistory request,免得撞 10s 超时 +
        # 跨 loop wakeup 噪音)
        while True:
            self._check_alive()
            t = GetChatHistory(  # type: ignore[call-arg](
                chat_id=channel_id,
                from_message_id=before_msg_id,
                offset=0,
                limit=limit,
            )
            resp = await self.request(t)
            if resp is None or not getattr(resp, "messages", None):
                break
            batch = list(resp.messages)
            for raw in batch:
                if raw is None:
                    continue
                # _map_message 自己从 msg.chat_id 取 channel_id,
                # 不需要外面传;这里只 yield
                yield _map_message(raw)
            # TDLib 文档:limit<=100;返回数 < limit → 已到尽头
            if len(batch) < limit:
                break
            # 续拉:用本批最末(最小)id 作为下次 from_message_id
            last_id = None
            for raw in batch:
                rid = getattr(raw, "id", None)
                if rid is not None and (last_id is None or rid < last_id):
                    last_id = rid
            if last_id is None or last_id == before_msg_id:
                break
            before_msg_id = last_id

    async def join_channel(self, identifier: str) -> ChannelDTO:
        self._check_alive()
        username = identifier.lstrip("@") if identifier.startswith("@") else identifier
        # search 要拿响应 → request;join 不需要响应 → send
        resp = await self.request(SearchPublicChat(username=username))
        if resp is None:
            raise RuntimeError(f"SearchPublicChat 返回空: {username!r}")
        await self.send(JoinChat(chat_id=resp.id))
        return ChannelDTO(id=resp.id, title=resp.title, username=resp.username or None)

    # ---- 媒体下载(REVIEW M2.1 — 真实现) ----

    async def download_file(self, file_id: str) -> bytes | None:
        """两步下载原文件 bytes;失败 / 超时返 None,**不抛**(让 monitor 循环继续)。

        步骤:
          1) DownloadFile(synchronous=False) 触发后台下载(priority=1, 不等)。
          2) GetFile 轮询直到 `local.is_downloading_completed`;读 `local.path`。
          3) 边界:
             - 入口 _check_alive():close 中 throw ClientClosingError(已有)。
             - 30 min hard cap:超过 → 返 None + WARNING。
             - GetFile 返 None / path 缺失 → 返 None + WARNING。
        """
        import asyncio as _aio
        import time as _t
        from pathlib import Path as _Path

        self._check_alive()
        # 1) 触发后台下载(不等 — DownloadFile synchronous=False)
        try:
            await self.request(
                DownloadFile(file_id=file_id, priority=1, synchronous=False)
            )
        except ClientClosingError:
            raise  # 让 close() 路径正常 throw,monitor loop 兜底
        except Exception as e:  # noqa: BLE001
            log.warning("DownloadFile(%s) failed: %s", file_id, e)
            return None

        # 2) 轮询直到 complete 或 hard cap
        deadline = _t.monotonic() + 1800.0  # 30 min
        while _t.monotonic() < deadline:
            self._check_alive()
            try:
                f = await self.request(GetFile(file_id=file_id))
            except ClientClosingError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("GetFile(%s) failed: %s", file_id, e)
                return None
            if f is None:
                log.warning("GetFile(%s) returned None", file_id)
                return None
            local = getattr(f, "local", None)
            if local is None:
                log.warning("GetFile(%s).local is None", file_id)
                return None
            if getattr(local, "is_downloading_completed", False):
                path = getattr(local, "path", None)
                if not path:
                    log.warning("GetFile(%s).local.path missing on complete", file_id)
                    return None
                try:
                    # Path.read_bytes 是 sync IO;asyncio.to_thread 把
                    # 它 off-loop 跑,免得在 qasync / uvloop loop 上 block。
                    return await _aio.to_thread(_Path(path).read_bytes)
                except OSError as e:
                    log.warning("read_bytes(%s) failed: %s", path, e)
                    return None
            await _aio.sleep(0.5)

        log.warning("download_file(%s) timed out after 30 min", file_id)
        return None

    def subscribe_updates(self) -> UpdateStream:
        s = _AiotdlibUpdateStream(on_close=self._remove_stream)
        self._streams.append(s)
        return s

    def _remove_stream(self, s: _AiotdlibUpdateStream) -> None:
        """`_AiotdlibUpdateStream.aoclose()` 回调 — 拿掉自己,避免 list 只增不减。

        `close()` 路径里仍然做全量清空(兜底),此回调是常规路径。
        """
        try:
            self._streams.remove(s)
        except ValueError:
            pass  # close() 已清空,忽略
