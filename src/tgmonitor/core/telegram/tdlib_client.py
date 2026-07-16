"""TDLib 实现 — 通过 `aiotdlib` 封装。

- 业务侧只见 `TelegramClient` Protocol;此文件**唯一**接触 TDLib
- 鉴权:实际接 `aiotdlib` 的内部状态机(不假装 ready)
  - 用 `asyncio.Queue` 把 UI 提交的 code / 2FA 密码注入 aiotdlib 的钩子
  - 通过 `updateAuthorizationState` 事件把 TDLib 真实状态转 `LoginStateChanged` 推给 UI
- 实时更新:`updateNewMessage` → DTO → UI

依赖:`aiotdlib >= 0.27`(旧版直接 kwargs 调用有备选路径)。
"""
from __future__ import annotations

import asyncio
import collections
import logging
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)

try:
    from aiotdlib import Client as _AiClient  # type: ignore
    from aiotdlib.api import (  # type: ignore
        API,
        BaseObject,
        CheckAuthenticationCode,
        CheckAuthenticationPassword,
        GetAuthorizationState,
        GetBasicGroup,
        GetChat,
        GetChatHistory,
        GetChats,
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
        from aiotdlib.client_settings import (  # type: ignore
            ClientProxySettings,
            ClientProxyType,
            ClientSettings,
        )
    except Exception:  # noqa: BLE001
        ClientSettings = None  # type: ignore[assignment]
        ClientProxySettings = None  # type: ignore[assignment]
        ClientProxyType = None  # type: ignore[assignment]
    _HAVE_AIOTDLIB = True
except Exception:  # noqa: BLE001
    _HAVE_AIOTDLIB = False
    ClientSettings = None  # type: ignore[assignment]

from tgmonitor.core.config import Settings  # noqa: E402 — aiotdlib import 上方有 try/except 守卫
from tgmonitor.core.dto import ChannelDTO, MediaDTO, MediaType, MessageDTO  # noqa: E402
from tgmonitor.core.telegram.client import UpdateStream  # noqa: E402

# ---- aiotdlib AuthorizationState.ID → 我们的字符串 ----
# 注:`API.Types.AUTHORIZATION_STATE_*` 是 aiotdlib 内部常量;
# 用字符串常量更稳(aiotdlib 内部同样用字符串 key)。

_AUTH_STATE_MAP: dict[str, str] = {
    API.Types.AUTHORIZATION_STATE_WAIT_TDLIB_PARAMETERS: "tdlib_parameters",
    API.Types.AUTHORIZATION_STATE_WAIT_PHONE_NUMBER: "phone_required",
    API.Types.AUTHORIZATION_STATE_WAIT_CODE: "code_required",
    API.Types.AUTHORIZATION_STATE_WAIT_EMAIL_ADDRESS: "email_required",
    API.Types.AUTHORIZATION_STATE_WAIT_EMAIL_CODE: "email_code_required",
    API.Types.AUTHORIZATION_STATE_WAIT_REGISTRATION: "registration_required",
    API.Types.AUTHORIZATION_STATE_WAIT_PASSWORD: "password_required",
    API.Types.AUTHORIZATION_STATE_READY: "ready",
    API.Types.AUTHORIZATION_STATE_LOGGING_OUT: "logging_out",
    API.Types.AUTHORIZATION_STATE_CLOSING: "closing",
    API.Types.AUTHORIZATION_STATE_CLOSED: "closed",
}


def parse_socks5_proxy(url: str | None) -> Any:
    """`socks5://[user:pass@]host:port` → aiotdlib `ClientProxySettings`;空/None → None。

    注意:aiotdlib 的 `ProxyTypeSocks5` 是 pydantic v2,username/password 字段
    类型是 `str`(严格),不能传 `None` — 必须空串 `""`。
    """
    if not url or not url.strip():
        return None
    s = url.strip()
    if not (s.startswith("socks5://") or s.startswith("SOCKS5://")):
        raise ValueError(f"unsupported proxy scheme: {s!r}(仅支持 socks5)")
    if ClientProxySettings is None:  # pragma: no cover
        raise RuntimeError("aiotdlib 版本过老,不支持代理配置")
    rest = s.split("://", 1)[1]
    user: str = ""
    password: str = ""
    if "@" in rest:
        creds, hostport = rest.rsplit("@", 1)
        if ":" in creds:
            user, _, password = creds.partition(":")
        else:
            user = creds
    else:
        hostport = rest
    if ":" not in hostport:
        raise ValueError(f"proxy missing port: {s!r}")
    host, _, port_s = hostport.rpartition(":")
    if not host or not port_s.isdigit():
        raise ValueError(f"invalid proxy host:port: {s!r}")
    return ClientProxySettings(  # type: ignore[call-arg]
        host=host,
        port=int(port_s),
        type=ClientProxyType.SOCKS5,  # type: ignore[arg-type]
        username=user,
        password=password,
    )


class _AiotdlibUpdateStream(UpdateStream):
    """aiotdlib → asyncio.Queue → UI。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[MessageDTO | None] = asyncio.Queue()
        self._closed = False

    async def push(self, msg: MessageDTO) -> None:
        if not self._closed:
            await self._queue.put(msg)

    async def close(self) -> None:
        self._closed = True
        await self._queue.put(None)

    def __aiter__(self) -> AsyncIterator[MessageDTO]:
        return self

    async def __anext__(self) -> MessageDTO:
        item = await self._queue.get()
        if item is None or self._closed:
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        await self.close()


def _load_or_create_encryption_key(td_dir, *, rotate: bool = False) -> str:
    """session 加密 key 必须跨启动稳定,否则 TDLib 解不开上次落盘的 db。

    做法:首次启动生成 32 字节随机 key,base64 编码后存到 `tdlib/.encryption_key`;
    后续启动从文件读。

    Args:
        td_dir:   TDLib 数据目录
        rotate:   若 True,无视现有文件,删除后重新生成。用于检测到 401 等
                  "key 不匹配" 时的恢复路径。
    """
    import base64 as _b64
    import os as _os
    import secrets as _secrets

    key_file = td_dir / ".encryption_key"

    if rotate and key_file.exists():
        try:
            key_file.unlink()
            log.warning("encryption key rotated (deleted %s)", key_file)
        except OSError as e:
            log.warning("rotate failed: %s", e)

    try:
        if key_file.exists():
            key_b64 = key_file.read_text("utf-8").strip()
            raw = _b64.b64decode(key_b64, validate=True)
            if len(raw) >= 32:
                return key_b64
            log.warning("encryption key file too short (%d bytes), regenerating", len(raw))
    except OSError as e:
        log.warning("read encryption key failed: %s — regenerating", e)

    td_dir.mkdir(parents=True, exist_ok=True)
    raw = _secrets.token_bytes(32)
    key_b64 = _b64.b64encode(raw).decode("ascii")
    try:
        fd = _os.open(key_file, _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
        try:
            _os.write(fd, key_b64.encode("ascii"))
        finally:
            _os.close(fd)
        log.info("generated new TDLib encryption key: %s (32 bytes)", key_file)
    except OSError as e:
        log.error("write encryption key failed: %s — will use ephemeral key", e)
    return key_b64


async def _probe_proxy(proxy_url: str, timeout: float = 3.0) -> tuple[bool, str]:  # noqa: ASYNC109 — `timeout` 是 SOCKS5 握手本身的超时,不是 asyncio.wait_for;命名直白可用
    """真做 SOCKS5 握手 — 不光 TCP 端口可达,还要回 greeting + 响应 CONNECT。

    返回 `(ok, message)`:ok=True 时 message="SOCKS5 proxy OK: host:port";
    ok=False 时 message 是给 UI 看的失败原因。
    """
    """真做 SOCKS5 握手 — 不光 TCP 端口可达,还要回 greeting + 响应 CONNECT。

    之前只 open_connection,碰到端口开但服务挂掉(例如 Clash 关了 SOCKS 但
    别的进程在 listen)就会误报通。

    流程:
      1) TCP open host:port
      2) 发 SOCKS5 greeting:[0x05, 0x01, 0x00] (版本 5, 1 种认证方式:无)
      3) 等 server 回 [0x05, 0x00] (选 no-auth)
      4) 发 CONNECT 到 1.1.1.1:443(可达目标,只验证代理本身通,不出 DC)
      5) 等 server 回 [0x05, 0x00, ...]
    """
    import asyncio as _aio

    if not proxy_url or not proxy_url.strip():
        return False, "代理未配置"
    try:
        rest = proxy_url.strip().split("://", 1)[1]
        hostport = rest.rsplit("@", 1)[1] if "@" in rest else rest
        host, _, port_s = hostport.rpartition(":")
        port = int(port_s)
    except Exception as e:  # noqa: BLE001
        log.error("proxy URL parse failed: %s", e)
        return False, f"代理 URL 格式错: {e}"

    try:
        reader, writer = await _aio.wait_for(_aio.open_connection(host, port), timeout=timeout)
    except (TimeoutError, OSError) as e:
        log.error("proxy TCP unreachable: %s:%d — %s", host, port, e)
        return False, f"代理 TCP 不通 {host}:{port} — {e}"

    try:
        # 1) greeting
        writer.write(bytes([0x05, 0x01, 0x00]))  # ver=5, nmethods=1, method=no-auth
        await _aio.wait_for(writer.drain(), timeout=timeout)
        greeting = await _aio.wait_for(reader.readexactly(2), timeout=timeout)
        if len(greeting) < 2 or greeting[0] != 0x05 or greeting[1] != 0x00:
            log.error(
                "proxy greeting response invalid: %s — 不是 SOCKS5 服务,或要求认证",
                greeting.hex(),
            )
            return False, f"代理不是 SOCKS5 或要求认证 ({greeting.hex()})"
        # 2) CONNECT 1.1.1.1:443 (验证代理本身可达,不出 DC)
        target_host = b"1.1.1.1"
        target_port = 443
        req = bytes([0x05, 0x01, 0x00, 0x01]) + bytes([len(target_host)]) + target_host + bytes(
            [(target_port >> 8) & 0xFF, target_port & 0xFF]
        )
        writer.write(req)
        await _aio.wait_for(writer.drain(), timeout=timeout)
        # reply: ver(1) rep(1) rsv(1) atyp(1) bnd.addr bnd.port — 至少 10 字节
        reply = await _aio.wait_for(reader.readexactly(10), timeout=timeout)
        if reply[1] != 0x00:
            log.error(
                "SOCKS5 CONNECT to 1.1.1.1:443 failed: reply=%s (rep=%d — 0x00=success)",
                reply.hex(), reply[1],
            )
            return False, f"代理拒绝 CONNECT (rep={reply[1]})"
        log.info("SOCKS5 proxy OK: %s:%d", host, port)
        return True, f"SOCKS5 proxy OK: {host}:{port}"
    except (TimeoutError, Exception) as e:  # noqa: BLE001
        log.error("SOCKS5 handshake failed: %s:%d — %s", host, port, e)
        return False, f"代理握手失败 {host}:{port} — {e}"
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


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
                from tgmonitor.core.events import LoginStateChanged
                # 用 fire-and-forget task — 不要 await,避免让 `_updates_loop` 卡住
                self._bus.publish_threadsafe(
                    asyncio.get_event_loop(),
                    LoginStateChanged(state=new_state, detail=detail),
                ) if False else asyncio.create_task(
                    self._safe_publish_state(new_state, detail)
                )
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

    async def _check_authentication_code(self) -> None:
        """从队列收 UI 提交的验证码。错误 → 发 `AuthErrorOccurred`,
        但不 raise(让 aiotdlib 自动重新发 WaitCode,用户原地重输)。"""
        code = await self._code_queue.get()
        log.info("submitting authentication code (len=%d)", len(code))
        try:
            await self.request(
                CheckAuthenticationCode(code=code), request_timeout=30,
            )
        except AioTDLibError as e:
            log.warning("CheckAuthenticationCode failed: %s", e)
            # aiotdlib 0.27 的 AioTDLibError 用 .message,直接读;
            # 但若 wait_for 触发 TimeoutError 被某种 wrapper 包成裸 Exception 进来,
            # 兜底用 str(e) 而不是 e.message。
            detail = getattr(e, "message", None) or str(e) or "未知错误"
            await self._publish_auth_error("code", f"验证码错误: {detail}", e)
        except Exception as e:  # noqa: BLE001
            log.exception("CheckAuthenticationCode unexpected failure")
            await self._publish_auth_error("code", f"提交失败: {e}", e)

    async def _check_authentication_password(self) -> None:
        """2FA 密码注入。"""
        pwd = await self._password_queue.get()
        log.info("submitting 2FA password (len=%d)", len(pwd))
        try:
            await self.request(
                CheckAuthenticationPassword(password=pwd), request_timeout=30,
            )
        except AioTDLibError as e:
            log.warning("CheckAuthenticationPassword failed: %s", e)
            detail = getattr(e, "message", None) or str(e) or "未知错误"
            await self._publish_auth_error(
                "password", f"2FA 密码错误: {detail}", e,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("CheckAuthenticationPassword unexpected failure")
            await self._publish_auth_error("password", f"提交失败: {e}", e)

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
            # 错误码 -500 / 401 / 429 等是 TDLib 自己的错误,我们一律翻译成 error
            # 状态。401 是 special — AppService 应据此外层 rotate key。
            err_detail: str | None = None
            if 401 in self._seen_error_codes:
                err_detail = "local session db encryption key 不匹配 (TDLib code 401)"
            elif 429 in self._seen_error_codes:
                err_detail = "TDLib 限流 (code 429),稍后重试"
            elif self._seen_error_codes:
                err_detail = (
                    f"DC 握手失败 (TDLib codes {list(self._seen_error_codes)})"
                )
            else:
                err_detail = "TDLib 启动超时(可能代理不可达或 DC 不通)"
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
        await self._password_queue.put(password)
        self._state_event.clear()
        try:
            await asyncio.wait_for(self._state_event.wait(), timeout=15.0)
        except TimeoutError:
            log.warning("submit_password: timeout (state=%s)", self._state)
        return self._state, self._state_detail

    async def logout(self) -> None:
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
        msg = getattr(exc, "message", None) or str(exc)
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
        """app exit 时调 — 内部 aiotdlib + 关掉所有订阅流。"""
        # 关流
        for s in list(self._streams):
            try:
                await s.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._streams.clear()
        await self._kill_aiotdlib()

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
        """ChannelSyncService 用:拉一个频道的最新元数据。"""
        dto = await self._resolve_channel_metadata(channel_id)
        if dto is None:
            # 私有/secret 或 chat 不存在,fallback 给个 stub
            return ChannelDTO(id=channel_id, title=f"#{channel_id}")
        return dto

    async def list_joined_channels(self) -> list[ChannelDTO]:
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
            n_total = len(chats.chat_ids or [])
            for i, cid in enumerate(chats.chat_ids or []):
                try:
                    dto = await self._resolve_channel_metadata(cid)
                except Exception:  # noqa: BLE001
                    log.exception("_resolve_channel_metadata(%d) failed", cid)
                    continue
                if dto is None:
                    continue
                result.append(dto)
                if n_total >= 50 and (i + 1) % 50 == 0:
                    log.info("[tdlib] list_joined_channels progress %d/%d in %.2fs",
                             i + 1, n_total, _t.monotonic() - t0)
        except Exception:  # noqa: BLE001
            log.exception("list_joined_channels failed")
        log.info("[tdlib] list_joined_channels done: %d channels in %.2fs",
                 len(result), _t.monotonic() - t0)
        return result

    # ---- 历史消息分页(全量同步用) ----

    async def iter_chat_history(
        self,
        channel_id: int,
        *,
        from_msg_id: int = 0,
        limit: int = 100,
    ) -> AsyncIterator[MessageDTO]:
        """分页拉取频道历史消息。

        from_msg_id=0 → 最新 N 条;>0 → 续拉 from_msg_id 之后。
        TDLib 返回的 messages 是 reverse chronological(递减 id),所以翻页用
        本批**最后**一条的 id 作为下次 from_msg_id(更小)。
        限流:每页间不 sleep(由调用方 ChannelSyncService 控)。
        """
        from tgmonitor.core.telegram.tdlib_client import _map_message
        while True:
            t = GetChatHistory(  # type: ignore[call-arg]
                chat_id=channel_id,
                from_message_id=from_msg_id,
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
            if last_id is None or last_id == from_msg_id:
                break
            from_msg_id = last_id

    async def join_channel(self, identifier: str) -> ChannelDTO:
        username = identifier.lstrip("@") if identifier.startswith("@") else identifier
        # search 要拿响应 → request;join 不需要响应 → send
        resp = await self.request(SearchPublicChat(username=username))
        if resp is None:
            raise RuntimeError(f"SearchPublicChat 返回空: {username!r}")
        await self.send(JoinChat(chat_id=resp.id))
        return ChannelDTO(id=resp.id, title=resp.title, username=resp.username or None)

    # ---- 消息历史(当前为占位) ----

    async def iter_messages(
        self, channel_id: int, *, from_msg_id: int = 0, limit: int | None = None
    ) -> AsyncIterator[MessageDTO]:
        if False:  # pragma: no cover
            yield None  # type: ignore[misc]
        return

    def subscribe_updates(self) -> UpdateStream:
        s = _AiotdlibUpdateStream()
        self._streams.append(s)
        return s


# ============================================================
# Message mapping — 把 TDLib messageContent union 转成 MessageDTO
# ============================================================
# 设计:用 `type(content).__name__` 字符串做 dispatch,而不是已经废掉的
# `.get_type()`(aiotdlib 0.27 用 pydantic v2,没有 get_type 方法)。
#
# 两张 handler 表:
#   _MEDIA_HANDLERS — 8 个携带媒体二进制的 Message* 类型,返回 (list[MediaDTO], caption_text)
#   _SERVICE_HANDLERS — 30+ 个 service 类,返回人类可读文本
#
# 任何新类型只需在表里加一行,无需改 _map_message 本体。
# 不在两张表里的类走 fallback:`[service: ClassName]`。


def _extract_caption(content: Any) -> str:
    """`MessagePhoto.caption` / `MessageVideo.caption` 等的 FormattedText 提取。

    旧实现里 caption.text 可能是 None 或 str;不抛异常,空就空。
    """
    cap = getattr(content, "caption", None)
    if cap is None:
        return ""
    inner = getattr(cap, "text", None)
    return inner if isinstance(inner, str) else ""


def _formatted_text(content: Any, attr: str) -> str:
    """`content.<attr>` 是 FormattedText → 拿 inner `.text`;若已是 str 直接返回。

    aiotdlib 0.27 的 FormattedText 是 pydantic v2 model,`.text` 字段是 str;
    但万一传进来的就是 str(legacy / fake),也要兜底。
    """
    if content is None:
        return ""
    ft = getattr(content, attr, None)
    if ft is None:
        return ""
    if isinstance(ft, str):
        return ft
    inner = getattr(ft, "text", None)
    return inner if isinstance(inner, str) else ""


def _pick_biggest_photo_size(photo: Any) -> Any:
    """Photo.sizes 按 width*height 取最大的 PhotoSize。

    旧实现按 `.size` 字段取,TDLib 那个字段是 File.size 而不是 PhotoSize 自身,
    实际我们想要的是最大面积的图片 → 用 width*height 更准。
    """
    sizes = getattr(photo, "sizes", None) or []
    if not sizes:
        return None
    return max(
        sizes,
        key=lambda s: (getattr(s, "width", 0) or 0) * (getattr(s, "height", 0) or 0),
    )


def _file_id(file_obj: Any) -> str | None:
    """File.id → str;None → None。"""
    if file_obj is None:
        return None
    fid = getattr(file_obj, "id", None)
    return str(fid) if fid is not None else None


def _file_size(file_obj: Any) -> int | None:
    if file_obj is None:
        return None
    return getattr(file_obj, "size", None) or None


def _thumb_key_from(thumbnail: Any) -> tuple[str | None, str | None]:
    """Thumbnail.file.id → (thumb_key, thumb_backend);无 thumbnail → (None, None)。"""
    if thumbnail is None:
        return None, None
    f = getattr(thumbnail, "file", None)
    if f is None:
        return None, None
    fid = getattr(f, "id", None)
    if fid is None:
        return None, None
    return f"media/{fid}.thumb", "local"


# ---- 媒体 handler 工厂 ----
# 7 个非 Photo 非 Sticker 的媒体类(MessagePhoto / MessageSticker 各自特殊)
# 共用一个工厂:取 media_obj 上的 file_attr(File 对象)→ id/size/dims/duration/thumbnail。


def _build_media_handler(
    media_type: MediaType,
    *,
    media_obj: str,
    file_attr: str,
    mime_default: str | None = None,
    has_dims: bool = False,
    dims_square: bool = False,
    has_duration: bool = True,
    thumb_attr: str | None = "thumbnail",
):
    """返回一个 fn(content) -> (list[MediaDTO], caption_text) 闭包。

    - media_obj: content 下挂的媒体对象字段(如 "video" / "audio" / "voice_note")
    - file_attr: media_obj 下挂的 File 对象字段(如 "video" / "audio" / "voice")
    - mime_default: 媒体对象没给 mime_type 时兜底;None = 用对象自带的 mime_type
    - has_dims: 是否取 width/height
    - dims_square: VideoNote 用,length 同时表示 w=h
    - has_duration: 是否取 duration(秒);audio/voice/video/animation/video_note = True;document = False
    - thumb_attr: media_obj 下挂的 Thumbnail 字段名;None = 无缩略图(voice_note)
    """
    def _fn(content: Any) -> tuple[list[MediaDTO], str]:
        obj = getattr(content, media_obj, None)
        if obj is None:
            return ([], _extract_caption(content))
        file_obj = getattr(obj, file_attr, None)
        kwargs: dict = {
            "type": media_type,
            "mime_type": getattr(obj, "mime_type", None) or mime_default,
            "file_name": getattr(obj, "file_name", None),
            "telegram_file_id": _file_id(file_obj),
            "file_size": _file_size(file_obj),
        }
        if has_dims:
            if dims_square:
                length = getattr(obj, "length", None)
                kwargs["width"] = length
                kwargs["height"] = length
            else:
                kwargs["width"] = getattr(obj, "width", None)
                kwargs["height"] = getattr(obj, "height", None)
        if has_duration:
            kwargs["duration"] = getattr(obj, "duration", None)
        if thumb_attr:
            th = getattr(obj, thumb_attr, None)
            tk, tb = _thumb_key_from(th)
            kwargs["thumb_key"] = tk
            kwargs["thumb_backend"] = tb
        return ([MediaDTO(**kwargs)], _extract_caption(content))
    return _fn


def _handle_photo(content: Any) -> tuple[list[MediaDTO], str]:
    """Photo 特殊:从 sizes[] 里按面积取最大 PhotoSize。"""
    ph = getattr(content, "photo", None)
    if ph is None:
        return ([], _extract_caption(content))
    biggest = _pick_biggest_photo_size(ph)
    file_obj = getattr(biggest, "photo", None) if biggest is not None else None
    return (
        [MediaDTO(
            type=MediaType.PHOTO,
            mime_type="image/jpeg",
            file_size=_file_size(file_obj),
            width=getattr(biggest, "width", None) if biggest is not None else None,
            height=getattr(biggest, "height", None) if biggest is not None else None,
            telegram_file_id=_file_id(file_obj),
        )],
        _extract_caption(content),
    )


def _handle_sticker(content: Any) -> tuple[list[MediaDTO], str]:
    """Sticker 特殊:无 caption / duration / mime_type,有 emoji。"""
    st = getattr(content, "sticker", None)
    if st is None:
        return ([], "")
    file_obj = getattr(st, "sticker", None)
    th = getattr(st, "thumbnail", None)
    tk, tb = _thumb_key_from(th)
    return (
        [MediaDTO(
            type=MediaType.STICKER,
            file_size=_file_size(file_obj),
            width=getattr(st, "width", None),
            height=getattr(st, "height", None),
            telegram_file_id=_file_id(file_obj),
            thumb_key=tk,
            thumb_backend=tb,
            emoji=getattr(st, "emoji", None),
        )],
        "",
    )


_MEDIA_HANDLERS: dict[str, Any] = {
    "MessagePhoto": _handle_photo,
    "MessageVideo": _build_media_handler(
        MediaType.VIDEO,
        media_obj="video", file_attr="video",
        has_dims=True, thumb_attr="thumbnail",
    ),
    "MessageAnimation": _build_media_handler(
        MediaType.ANIMATION,
        media_obj="animation", file_attr="animation",
        has_dims=True, thumb_attr="thumbnail",
    ),
    "MessageAudio": _build_media_handler(
        MediaType.AUDIO,
        media_obj="audio", file_attr="audio",
        mime_default="audio/mpeg",
        has_dims=False, thumb_attr="album_cover_thumbnail",
    ),
    "MessageVoiceNote": _build_media_handler(
        MediaType.VOICE,
        media_obj="voice_note", file_attr="voice",
        mime_default="audio/ogg",
        has_dims=False, thumb_attr=None,  # voice 没缩略图
    ),
    "MessageVideoNote": _build_media_handler(
        MediaType.VIDEO_NOTE,
        media_obj="video_note", file_attr="video",
        has_dims=True, dims_square=True, thumb_attr="thumbnail",
    ),
    "MessageDocument": _build_media_handler(
        MediaType.DOCUMENT,
        media_obj="document", file_attr="document",
        has_dims=False, has_duration=False, thumb_attr="thumbnail",
    ),
    "MessageSticker": _handle_sticker,
}


# ---- Service handler(只产生 text)----


def _handle_dice(content: Any) -> str:
    return f"🎲 {getattr(content, 'emoji', '🎲')} {getattr(content, 'value', 0)}"


def _handle_location(content: Any) -> str:
    loc = getattr(content, "location", None)
    if loc is None:
        return ""
    lat = getattr(loc, "latitude", 0.0) or 0.0
    lon = getattr(loc, "longitude", 0.0) or 0.0
    suffix = " 🛰️" if getattr(content, "live_period", 0) else ""
    return f"📍 {lat:.4f}, {lon:.4f}{suffix}"


def _handle_venue(content: Any) -> str:
    venue = getattr(content, "venue", None)
    if venue is None:
        return ""
    title = getattr(venue, "title", "") or ""
    addr = getattr(venue, "address", "") or ""
    return f"📍 {title} — {addr}" if addr else f"📍 {title}"


def _handle_contact(content: Any) -> str:
    c = getattr(content, "contact", None)
    if c is None:
        return ""
    fn = (getattr(c, "first_name", "") or "").strip()
    ln = (getattr(c, "last_name", "") or "").strip()
    name = f"{fn} {ln}".strip()
    phone = getattr(c, "phone_number", "") or ""
    return f"📎 {name} (+{phone})" if name else f"📎 (+{phone})"


def _handle_poll(content: Any) -> str:
    p = getattr(content, "poll", None)
    if p is None:
        return "📊 <poll>"
    q = getattr(p, "question", None)
    return "📊 " + _formatted_text(q, "text") if q is not None else "📊 <poll>"


def _handle_call(content: Any) -> str:
    dur = getattr(content, "duration", 0) or 0
    is_video = bool(getattr(content, "is_video", False))
    prefix = "📹 视频通话" if is_video else "📞 通话"
    return f"{prefix} {dur}s"


def _handle_video_chat_scheduled(content: Any) -> str:
    from datetime import datetime as _dt
    start = getattr(content, "start_date", 0) or 0
    if not start:
        return "📅 视频通话已安排"
    return "📅 视频通话已安排 " + _dt.utcfromtimestamp(start).strftime("%Y-%m-%d %H:%M UTC")


def _handle_gift(content: Any) -> str:
    g = getattr(content, "gift", None)
    stars = getattr(g, "star_count", 0) if g is not None else 0
    is_private = bool(getattr(content, "is_private", False))
    text = _formatted_text(content, "text")
    base = "🎁 私密礼物" if is_private else f"🎁 {stars}⭐ 礼物"
    return f"{base}\n  {text}" if text else base


def _handle_gifted_premium(content: Any) -> str:
    months = getattr(content, "month_count", 0) or 0
    text = _formatted_text(content, "text")
    base = f"⭐ Telegram Premium {months} 个月"
    return f"{base}\n  {text}" if text else base


def _handle_giveaway_created(content: Any) -> str:
    # MessageGiveawayCreated 直接有 star_count(无 nested Gift)
    stars = getattr(content, "star_count", 0) or 0
    return f"🎁 抽奖已创建 · {stars}⭐" if stars else "🎁 抽奖已创建"


def _handle_upgraded_gift(content: Any) -> str:
    g = getattr(content, "gift", None)
    title = getattr(g, "title", "") if g is not None else ""
    number = getattr(g, "number", 0) if g is not None else 0
    if number:
        return f"💎 #{number}: {title}" if title else f"💎 #{number}"
    return f"💎 {title}" if title else "💎 升级版礼物"


_SERVICE_HANDLERS: dict[str, Any] = {
    "MessageText": lambda c: _formatted_text(c, "text"),
    "MessageDice": _handle_dice,
    "MessageAnimatedEmoji": lambda c: f"✨ {getattr(c, 'emoji', '')}",
    "MessageLocation": _handle_location,
    "MessageVenue": _handle_venue,
    "MessageContact": _handle_contact,
    "MessagePoll": _handle_poll,
    "MessageCall": _handle_call,
    "MessageCustomServiceAction": lambda c: _formatted_text(c, "text"),
    "MessageVideoChatScheduled": _handle_video_chat_scheduled,
    "MessageVideoChatStarted": lambda c: "📹 视频通话已开始",
    "MessageVideoChatEnded": lambda c: (
        f"📹 视频通话已结束({getattr(c, 'duration', 0)}s)"
    ),
    "MessageInviteVideoChatParticipants": lambda c: (
        f"📹 邀请 {len(getattr(c, 'user_ids', []) or [])} 人加入视频通话"
    ),
    "MessageStory": lambda c: (
        f"📖 转发的故事(频道 #{getattr(c, 'story_sender_chat_id', 0)})"
    ),
    "MessageGame": lambda c: f"🎮 {_formatted_text(getattr(c, 'game', None), 'title')}",
    "MessageGift": _handle_gift,
    "MessageGiftedPremium": _handle_gifted_premium,
    "MessageGiftedStars": lambda c: (
        f"⭐ {getattr(getattr(c, 'gift', None), 'star_count', 0)} Stars"
    ),
    "MessageGiveaway": lambda c: (
        f"🎁 抽奖开始({getattr(c, 'winner_count', 0)} 名获奖者)"
    ),
    "MessageGiveawayCreated": _handle_giveaway_created,
    "MessageGiveawayCompleted": lambda c: (
        f"🎁 抽奖结束 · {getattr(c, 'winner_count', 0)} 名获奖者"
    ),
    "MessageGiveawayWinners": lambda c: (
        f"🏆 {getattr(c, 'winner_count', 0)} 名获奖者"
    ),
    "MessageGiveawayPrizeStars": lambda c: (
        f"⭐ {getattr(c, 'star_count', 0)} Stars 抽奖奖励"
    ),
    "MessageUpgradedGift": _handle_upgraded_gift,
    "MessageRefundedUpgradedGift": lambda c: "↩️ 礼物已退款",
    "MessagePremiumGiftCode": lambda c: (
        f"🎟️ Premium 兑换码: {getattr(c, 'month_count', 0)} 个月"
    ),
    "MessagePinMessage": lambda c: (
        f"📌 已置顶消息 #{getattr(c, 'message_id', 0)}"
    ),
    "MessageChatBoost": lambda c: (
        f"🚀 群组被 boost {getattr(c, 'boost_count', 0)} 次"
    ),
    "MessageUnsupported": lambda c: "❓ 不支持的消息",
    "MessageExpiredPhoto": lambda c: "🕯️ 自毁照片已过期",
    "MessageExpiredVideo": lambda c: "🕯️ 自毁视频已过期",
    "MessageExpiredVideoNote": lambda c: "🕯️ 自毁视频消息已过期",
    "MessageExpiredVoiceNote": lambda c: "🕯️ 自毁语音已过期",
}


def _fallback_service(content: Any) -> str:
    """不在两张表里的类 — 显示类名,后续可补。"""
    return f"[service: {type(content).__name__}]"


def _map_message(msg: BaseObject) -> MessageDTO:  # type: ignore[name-defined]
    """TDLib Message → MessageDTO。

    aiotdlib 0.27 用 pydantic v2 model,TDLib 的 messageContent union
    (messageText / messagePhoto / ...) 在 Python 侧是独立类。
    dispatch 用 `type(content).__name__`,见顶部注释。
    """
    from datetime import datetime as _dt

    chat_id = getattr(msg, "chat_id", 0)
    content = getattr(msg, "content", None)
    media_list: list[MediaDTO] = []
    text_value: str = ""
    if content is not None:
        ctype_name = type(content).__name__
        media_handler = _MEDIA_HANDLERS.get(ctype_name)
        if media_handler is not None:
            media_list, text_value = media_handler(content)
        else:
            text_value = _SERVICE_HANDLERS.get(ctype_name, _fallback_service)(content)
    date_ts = getattr(msg, "date", 0)
    return MessageDTO(
        id=getattr(msg, "id", 0),
        channel_id=chat_id,
        telegram_msg_id=getattr(msg, "id", 0),
        author=getattr(msg, "author_signature", None),
        date=_dt.utcfromtimestamp(date_ts) if date_ts else _dt.utcnow(),
        text=text_value,
        views=getattr(msg, "views", None),
        forwards=getattr(msg, "forwards", None),
        edited=getattr(msg, "edit_date", 0) > 0,
        media=media_list,
    )


# ---- 限流错误归一 ----

class TelegramRateLimitError(RuntimeError):
    """TDLib 限流(429 / FLOOD_WAIT_*)归一异常。

    ChannelSyncService 收到这个异常后等 `retry_after_seconds` 再继续,
    保证不踩 Telegram 限流红线。
    """

    def __init__(self, retry_after_seconds: float, message: str = "") -> None:
        self.retry_after_seconds = float(retry_after_seconds)
        super().__init__(message or f"Telegram rate limit: wait {retry_after_seconds:.0f}s")
