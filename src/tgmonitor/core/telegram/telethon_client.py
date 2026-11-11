# mypy: disable-error-code="misc,assignment"
"""TelethonTelegramClient — v2.0.0 生产实现(Telethon 后端,替换 TDLib)。

实现 `client.TelegramClient` Protocol 全量方法。与 TDLib 版的关键差异:

# 鉴权:请求/响应,无队列
TDLib 是状态机推送 + 输入队列(`_code_queue` 等);Telethon 是显式 RPC:
`send_code_request` / `sign_in` / `CheckPasswordRequest`。每个 `submit_*`
直接发请求、按返回 / 异常推进 `_state`,不需要后台 authorize loop。

# 连接状态:watchdog 合成
Telethon 没有 TDLib 的 `updateConnectionState` 推送。用一个 5s 轮询
`client.is_connected()` 的 watchdog task 合成 `ConnectionStateChanged`
(ready / waiting_for_network),断线时主动 reconnect。只在 ready 后启动。

# file-id:纯消息定位符
见 `telethon_messages` 模块 docstring。下载 = `get_messages` 重取消息 →
`download_media`(Telethon 自动刷新过期 file_reference)。

# 限流:flood_sleep_threshold=0
让 `FloodWaitError` 总是抛出(不被 Telethon 内部 sleep 吞掉),
`iter_chat_history` 翻译成 `TelegramRateLimitError` 给 ChannelSyncService 退避。

# session
`settings.session_dir / "tgmonitor"`(Telethon 自动补 `.session` 后缀)。
401 / 登出 → 删 session 文件 + 重建 client(Telethon `log_out()` 后
`session=None`,旧 client 不可复用)。
"""

from __future__ import annotations

import asyncio
import logging
import platform
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from telethon import TelegramClient, errors, events, functions, types, utils

from tgmonitor import __version__
from tgmonitor.core.config import Settings
from tgmonitor.core.dto import ChannelDTO, MessageDTO
from tgmonitor.core.events import (
    AuthErrorOccurred,
    ChannelMetadataChanged,
    ChannelPhotoChanged,
    ChannelTitleChanged,
    ConnectionStateChanged,
    LoginStateChanged,
    MessageDeleted,
    MessageInteractionsChanged,
    MessagePinChanged,
)
from tgmonitor.core.telegram.client import UpdateStream
from tgmonitor.core.telegram.errors import (
    ChatUnavailableError,
    ClientClosingError,
    TelegramNotConfiguredError,
    TelegramRateLimitError,
    _extract_error_detail,
    _missing_credentials,
)
from tgmonitor.core.telegram.proxy import parse_socks5_proxy, probe_proxy
from tgmonitor.core.telegram.telethon_messages import (
    map_message,
    map_reactions,
    parse_file_id,
    pick_thumb_size,
)

log = logging.getLogger(__name__)


class _TelethonUpdateStream(UpdateStream):
    """Telethon update handler → asyncio.Queue → UI。

    与 TDLib 版 `_TdlibJsonUpdateStream` 同契约:`aclose()` 幂等、塞 sentinel
    让 `__anext__` 抛 StopAsyncIteration、触发 `on_close` 把自己从
    `client._streams` 拿掉(防长会话列表只增不减)。
    """

    def __init__(self, on_close: Callable[[_TelethonUpdateStream], None] | None = None) -> None:
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


class TelethonTelegramClient:
    """生产实现 — Telethon 后端,实现 `TelegramClient` Protocol。

    设计原则(与 TDLib 版对齐):
      - `_set_state` 是**唯一**写 `self._state` 的入口;`_state_event` 同步 set,
        `_wait_for_state` 等它推进。
      - `submit_*` 错误经 `AuthErrorOccurred` 事件通知 UI,**不**改顶层状态
        (让用户原地重试);顶层 `error` 只给启动失败 / 网络不可达。
      - `_check_alive()` 是事务性方法的 entry guard;best-effort 方法
        (`list_joined_channels`)自己 catch `ClientClosingError` 返占位。
      - `stop()` 断网保留 session(可再 start);`close()` 设 `_closing=True`
        不可逆(app exit)。
    """

    # 启动阻塞预算(SOCKS5 冷启动连 DC 通常 15-25s,留余量)。
    _BOOT_TIMEOUT = 30.0
    # list_joined_channels 非 ready 时等 ready 的最大秒数(best-effort)。
    _READY_WAIT_TIMEOUT = 8.0
    # 单文件下载 hard cap(与 TDLib 版一致)。
    _DOWNLOAD_TIMEOUT = 1800.0
    # watchdog 轮询间隔。
    _WATCHDOG_INTERVAL = 5.0

    def __init__(self, settings: Settings, *, event_bus: Any | None = None) -> None:
        """`settings` = Telegram / 代理等配置;`event_bus` = 可选 UI 事件总线。

        凭据预检 — 未配置时抛 `TelegramNotConfiguredError`(用户可读中文),
        由 app.py 启动失败弹窗展示。client 实例延迟到 `start()` 才构造。
        """
        missing = _missing_credentials(settings)
        if missing:
            raise TelegramNotConfiguredError(
                "未配置 Telegram 凭据:"
                + "、".join(missing)
                + "。请在 .env 或 设置… 中填写后再启动。"
            )
        self._settings = settings
        self._bus = event_bus
        self._client: TelegramClient | None = None
        self._me: dict | None = None
        self._streams: list[_TelethonUpdateStream] = []
        self._closing = False
        self._watchdog_task: asyncio.Task | None = None
        # 频道头像去重缓存(chat_id → photo.id),避免重复下载同一头像。
        self._photo_cache: dict[int, int] = {}

        # 顶层状态机 — 初值 uninit,真值由 start() 后的授权检查决定。
        self._state: str = "uninit"
        self._state_detail: str = ""
        self._state_event = asyncio.Event()

        # 鉴权流程暂存(request/response 模式,无队列)。
        self._phone: str = ""
        self._phone_code_hash: str = ""
        self._email_flow: str = ""  # "login"(已有账号邮箱验证) | "setup"(新设邮箱)

    # ============================================================
    # 状态管理:唯一写路径
    # ============================================================

    def _set_state(self, new_state: str, *, detail: str = "") -> None:
        """唯一允许写 `self._state` 的入口。唤醒 `_state_event` + 发 LoginStateChanged。"""
        if new_state == self._state and detail == self._state_detail:
            return
        prev = self._state
        self._state = new_state
        self._state_detail = detail
        log.info("state: %s → %s%s", prev, new_state, f" ({detail})" if detail else "")
        self._state_event.set()
        self._publish(LoginStateChanged(state=new_state, detail=detail))

    def _publish(self, event: Any) -> None:
        """fire-and-forget 发布到 EventBus(注册到 `_inflight` 供测试 flush)。"""
        if self._bus is None:
            return
        try:
            self._bus.publish_async(event)
        except Exception:  # noqa: BLE001
            log.exception("publish %s failed", type(event).__name__)

    def _auth_error(self, source: str, exc: BaseException) -> None:
        """transient 鉴权错误 → AuthErrorOccurred 事件(不改顶层状态)。"""
        detail = _extract_error_detail(exc)
        log.warning("auth error (%s): %s", source, detail)
        self._publish(AuthErrorOccurred(source=source, message=detail, exception=exc))

    def _check_alive(self) -> None:
        """事务性方法 entry guard — close() 已调时抛 ClientClosingError。"""
        if self._closing:
            raise ClientClosingError()

    async def _wait_for_state(self, target: str, *, timeout: float) -> None:  # noqa: ASYNC109 — `timeout` 是状态机推进的最大等待时间,命名直白可用
        """等 `_state` 推进到 target(或超时)。

        `_state_event` 是 set-only — 一旦 set 后续 wait() 立即返回(不真 yield),
        纯 polling 会 spin 吃满 loop。所以 event 已 set 时主动 sleep(0.05) 让出
        CPU 再重读 `_state`;未 set 时 wait_for(event.wait(), 0.5) 真等。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._state == target:
                return
            if self._state_event.is_set():
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
        """当前顶层状态。"""
        return self._state

    @property
    def me(self) -> dict | None:
        """当前登录用户 {id, username, first_name};未登录 None。"""
        return self._me

    # ============================================================
    # client 构造 / session 管理
    # ============================================================

    @property
    def _session_path(self) -> Path:
        """Telethon session 基名(自动补 `.session` 后缀)。"""
        return self._settings.session_dir / "tgmonitor"

    def _build_client(self) -> TelegramClient:
        """构造 Telethon client + 注册全部 update handler。"""
        self._settings.session_dir.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(
            str(self._session_path),
            self._settings.api_id,
            self._settings.api_hash,
            proxy=parse_socks5_proxy(self._settings.proxy),
            # 0 → FloodWaitError 总是抛出,由 iter_chat_history 翻译成
            # TelegramRateLimitError 给 ChannelSyncService 显式退避。
            flood_sleep_threshold=0,
            connection_retries=5,
            auto_reconnect=True,
            device_model="tgmonitor",
            system_version=platform.platform(),
            app_version=__version__,
        )
        self._register_handlers(client)
        return client

    def _register_handlers(self, client: TelegramClient) -> None:
        """注册实时更新 handler(在 connect 前调,Telethon 允许)。"""
        client.add_event_handler(self._on_new_message, events.NewMessage())
        client.add_event_handler(self._on_message_edited, events.MessageEdited())
        client.add_event_handler(self._on_message_deleted, events.MessageDeleted())
        client.add_event_handler(self._on_chat_action, events.ChatAction())
        client.add_event_handler(
            self._on_views, events.Raw(types.UpdateChannelMessageViews)
        )
        client.add_event_handler(
            self._on_reactions, events.Raw(types.UpdateMessageReactions)
        )
        client.add_event_handler(
            self._on_pinned_channel, events.Raw(types.UpdatePinnedChannelMessages)
        )
        client.add_event_handler(
            self._on_pinned, events.Raw(types.UpdatePinnedMessages)
        )
        client.add_event_handler(self._on_channel_updated, events.Raw(types.UpdateChannel))
        client.add_event_handler(self._on_chat_updated, events.Raw(types.UpdateChat))

    def _delete_session_files(self) -> None:
        """删 session 主文件 + sqlite 副文件(journal/wal/shm)。"""
        base = str(self._session_path) + ".session"
        for suffix in ("", "-journal", "-wal", "-shm"):
            p = Path(base + suffix)
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                log.warning("failed to remove session file %s", p)

    async def _disconnect_quietly(self) -> None:
        """best-effort 断开当前 client(不抛)。"""
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        except Exception:  # noqa: BLE001
            log.debug("disconnect failed", exc_info=True)

    async def _reset_session(self) -> None:
        """401 / session 损坏 — 删 session 文件 + 重建 client(未连接)。"""
        await self._cancel_watchdog()
        await self._disconnect_quietly()
        self._delete_session_files()
        self._me = None
        self._client = self._build_client()

    # ============================================================
    # 启动
    # ============================================================

    async def start(self) -> tuple[str, str | None]:
        """应用启动入口。preflight 代理 → connect → 授权检查 → ready / phone_required。"""
        self._check_alive()
        if self._state == "ready":
            return self._state, self._state_detail
        if self._settings.proxy:
            ok, msg = await probe_proxy(self._settings.proxy)
            if not ok:
                self._set_state("error", detail=msg)
                return self._state, self._state_detail
        try:
            await asyncio.wait_for(self._do_start(), timeout=self._BOOT_TIMEOUT)
        except TimeoutError:
            log.error("start: timed out after %.0fs", self._BOOT_TIMEOUT)
            await self._disconnect_quietly()
            self._set_state("error", detail=f"启动超时({self._BOOT_TIMEOUT:.0f}s)— 网络或代理不可达")
        except Exception as e:  # noqa: BLE001
            log.exception("start: unexpected")
            await self._disconnect_quietly()
            self._set_state("error", detail=_extract_error_detail(e))
        return self._state, self._state_detail

    async def _do_start(self) -> None:
        """connect + 授权检查;401(session 损坏)删 session 重试一次。"""
        if self._client is None:
            self._client = self._build_client()
        try:
            await self._connect_and_check()
        except errors.UnauthorizedError:
            log.warning("session auth key invalid — resetting session and retrying")
            await self._reset_session()
            await self._connect_and_check()

    async def _connect_and_check(self) -> None:
        """connect → is_user_authorized → ready(拉 me + 起 watchdog)/ phone_required。"""
        assert self._client is not None
        await self._client.connect()
        if await self._client.is_user_authorized():
            await self._on_login_success()
        else:
            self._set_state("phone_required")

    async def _on_login_success(self) -> None:
        """授权成功 — 拉 me、转 ready、起连接 watchdog。"""
        assert self._client is not None
        me = await self._client.get_me()
        self._me = {
            "id": me.id,
            "username": me.username,
            "first_name": me.first_name,
        }
        self._set_state("ready")
        self._start_watchdog()

    # ============================================================
    # 连接 watchdog(合成 ConnectionStateChanged)
    # ============================================================

    def _start_watchdog(self) -> None:
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def _cancel_watchdog(self) -> None:
        t = self._watchdog_task
        self._watchdog_task = None
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _watchdog_loop(self) -> None:
        """5s 轮询 is_connected,变化时发 ConnectionStateChanged;断线主动 reconnect。

        只在 ready 期间运行(stop/close/logout 会 cancel)。首次迭代立即发当前状态。
        """
        last: str | None = None
        try:
            while not self._closing and self._state == "ready":
                connected = bool(self._client and self._client.is_connected())
                cur = "ready" if connected else "waiting_for_network"
                if cur != last:
                    self._publish(ConnectionStateChanged(state=cur))
                    last = cur
                if not connected and self._client is not None:
                    try:
                        await self._client.connect()
                    except Exception:  # noqa: BLE001
                        log.debug("watchdog reconnect failed", exc_info=True)
                await asyncio.sleep(self._WATCHDOG_INTERVAL)
        except asyncio.CancelledError:
            pass

    # ============================================================
    # 鉴权(request/response)
    # ============================================================

    async def submit_phone(self, phone: str) -> tuple[str, str | None]:
        """提交手机号 → send_code_request → 按 SentCode.type 分流。

        401(is_user_authorized 把死 session 误判为"未授权")→ 删 session 重试一次。
        其它错误 → AuthErrorOccurred,不改顶层状态。
        """
        self._check_alive()
        assert self._client is not None
        self._phone = phone
        try:
            sent = await self._client.send_code_request(phone)
        except errors.UnauthorizedError:
            await self._reset_session()
            assert self._client is not None
            try:
                sent = await self._client.send_code_request(phone)
            except Exception as e:  # noqa: BLE001
                self._auth_error("phone", e)
                return self._state, self._state_detail
        except Exception as e:  # noqa: BLE001
            self._auth_error("phone", e)
            return self._state, self._state_detail
        self._phone_code_hash = sent.phone_code_hash
        return self._inspect_sent_code(sent)

    def _inspect_sent_code(self, sent: types.auth.SentCode) -> tuple[str, str | None]:
        """按 SentCode.type 决定下一状态(email 验证 / 设邮箱 / 普通验证码)。"""
        t = sent.type
        if isinstance(t, types.auth.SentCodeTypeEmailCode):
            self._email_flow = "login"
            self._set_state(
                "email_code_required",
                detail=f"验证码已发送至邮箱 {t.email_pattern or ''}",
            )
        elif isinstance(t, types.auth.SentCodeTypeSetUpEmailRequired):
            self._email_flow = "setup"
            self._set_state("email_required")
        else:
            self._set_state("code_required")
        return self._state, self._state_detail

    async def submit_code(self, code: str) -> tuple[str, str | None]:
        """提交验证码 → sign_in。2FA → password_required;未注册 → registration_required。"""
        self._check_alive()
        assert self._client is not None
        try:
            await self._client.sign_in(
                self._phone, code, phone_code_hash=self._phone_code_hash
            )
        except errors.SessionPasswordNeededError:
            self._set_state("password_required")
        except errors.PhoneNumberUnoccupiedError:
            self._set_state("registration_required")
        except Exception as e:  # noqa: BLE001
            self._auth_error("code", e)
        else:
            await self._on_login_success()
        return self._state, self._state_detail

    async def submit_password(self, password: str) -> tuple[str, str | None]:
        """提交 2FA 密码 → sign_in(password=)(内部走 CheckPasswordRequest)。"""
        self._check_alive()
        assert self._client is not None
        try:
            await self._client.sign_in(password=password)
        except Exception as e:  # noqa: BLE001
            self._auth_error("password", e)
        else:
            await self._on_login_success()
        return self._state, self._state_detail

    async def submit_email(self, email: str) -> tuple[str, str | None]:
        """`email_required`(新设邮箱)时提交邮箱 → 发邮箱验证码 → email_code_required。"""
        self._check_alive()
        assert self._client is not None
        try:
            await self._client(
                functions.account.SendVerifyEmailCodeRequest(
                    purpose=types.EmailVerifyPurposeLoginSetup(
                        phone_number=self._phone,
                        phone_code_hash=self._phone_code_hash,
                    ),
                    email=email,
                )
            )
        except Exception as e:  # noqa: BLE001
            self._auth_error("email", e)
        else:
            self._email_flow = "setup"
            self._set_state("email_code_required")
        return self._state, self._state_detail

    async def submit_email_code(self, code: str) -> tuple[str, str | None]:
        """提交邮箱验证码。login 流 → sign_in;setup 流 → VerifyEmailRequest → 新 SentCode。"""
        self._check_alive()
        assert self._client is not None
        try:
            if self._email_flow == "login":
                await self._client.sign_in(
                    self._phone, code, phone_code_hash=self._phone_code_hash
                )
                await self._on_login_success()
            else:
                result = await self._client(
                    functions.account.VerifyEmailRequest(
                        purpose=types.EmailVerifyPurposeLoginSetup(
                            phone_number=self._phone,
                            phone_code_hash=self._phone_code_hash,
                        ),
                        verification=types.EmailVerificationCode(code=code),
                    )
                )
                if isinstance(result, types.account.EmailVerifiedLogin):
                    self._phone_code_hash = result.sent_code.phone_code_hash
                    self._inspect_sent_code(result.sent_code)
                else:
                    self._set_state("code_required")
        except errors.SessionPasswordNeededError:
            self._set_state("password_required")
        except Exception as e:  # noqa: BLE001
            self._auth_error("email_code", e)
        return self._state, self._state_detail

    async def submit_registration(
        self, first_name: str, last_name: str = ""
    ) -> tuple[str, str | None]:
        """注册新账号 — 裸 auth.SignUpRequest(库 sign_up() 被 ValueError 阻断,#4050)。"""
        self._check_alive()
        assert self._client is not None
        try:
            result = await self._client(
                functions.auth.SignUpRequest(
                    phone_number=self._phone,
                    phone_code_hash=self._phone_code_hash,
                    first_name=first_name,
                    last_name=last_name,
                )
            )
        except Exception as e:  # noqa: BLE001
            self._auth_error("registration", e)
        else:
            user = getattr(result, "user", None)
            if user is not None:
                self._me = {
                    "id": user.id,
                    "username": user.username,
                    "first_name": user.first_name,
                }
            self._set_state("ready")
            self._start_watchdog()
        return self._state, self._state_detail

    async def logout(self) -> None:
        """登出 — log_out() + 删 session + 重建 client + connect → phone_required。

        Telethon `log_out()` 后 `session=None`,旧 client 不可复用,必须重建。
        """
        self._check_alive()
        await self._cancel_watchdog()
        old = self._client
        self._client = None
        if old is not None:
            try:
                await old.log_out()
            except Exception:  # noqa: BLE001
                log.warning("log_out failed; forcing local session reset", exc_info=True)
                try:
                    await old.disconnect()
                except Exception:  # noqa: BLE001
                    pass
        self._delete_session_files()
        self._me = None
        self._phone = ""
        self._phone_code_hash = ""
        self._email_flow = ""
        self._client = self._build_client()
        try:
            await self._client.connect()
            self._set_state("phone_required", detail="已登出,请重新登录")
        except Exception as e:  # noqa: BLE001
            self._set_state("error", detail=_extract_error_detail(e))

    # ============================================================
    # 清理
    # ============================================================

    async def stop(self) -> None:
        """暂停监听 — 断网但保留 session + client 实例,后续 start() 可复用。"""
        await self._cancel_watchdog()
        for s in list(self._streams):
            try:
                await s.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._streams.clear()
        await self._disconnect_quietly()
        self._state = "uninit"
        self._state_detail = ""
        self._state_event.clear()

    async def close(self) -> None:
        """app exit — `_closing=True` 不可逆 + 关流 + 断网。"""
        self._closing = True
        await self._cancel_watchdog()
        for s in list(self._streams):
            try:
                await s.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._streams.clear()
        await self._disconnect_quietly()

    # ============================================================
    # 实时更新 handler
    # ============================================================

    async def _on_new_message(self, event: Any) -> None:
        try:
            msg = event.message
            if msg is None:
                return
            dto = map_message(msg)
            for s in list(self._streams):
                await s.push(dto)
        except Exception:  # noqa: BLE001
            log.exception("NewMessage handling failed")

    async def _on_message_edited(self, event: Any) -> None:
        try:
            msg = event.message
            if msg is None:
                return
            dto = map_message(msg)
            for s in list(self._streams):
                await s.push(dto)
        except Exception:  # noqa: BLE001
            log.exception("MessageEdited handling failed")

    async def _on_message_deleted(self, event: Any) -> None:
        try:
            chat_id = event.chat_id
            if chat_id is None:
                return
            for mid in event.deleted_ids or []:
                self._publish(
                    MessageDeleted(channel_id=chat_id, telegram_msg_id=int(mid))
                )
        except Exception:  # noqa: BLE001
            log.exception("MessageDeleted handling failed")

    async def _on_chat_action(self, event: Any) -> None:
        """频道改名 / 头像变化(pin 走 Raw UpdatePinned*,避免与此重复)。"""
        try:
            chat_id = event.chat_id
            if chat_id is None:
                return
            if event.new_title:
                self._publish(
                    ChannelTitleChanged(channel_id=chat_id, new_title=event.new_title)
                )
            if event.new_photo:
                await self._handle_chat_photo(chat_id, event.photo)
        except Exception:  # noqa: BLE001
            log.exception("ChatAction handling failed")

    async def _handle_chat_photo(self, chat_id: int, photo: Any) -> None:
        """头像变化 — 下载到 session_dir/photos/{cid}.jpg;None = 头像被删。"""
        if photo is None:
            self._photo_cache.pop(chat_id, None)
            self._publish(ChannelPhotoChanged(channel_id=chat_id, local_path=None))
            return
        pid = getattr(photo, "id", None)
        if pid is not None and self._photo_cache.get(chat_id) == pid:
            return
        photos_dir = self._settings.session_dir / "photos"
        photos_dir.mkdir(parents=True, exist_ok=True)
        path = photos_dir / f"{chat_id}.jpg"
        try:
            assert self._client is not None
            await self._client.download_media(photo, file=str(path))
            if pid is not None:
                self._photo_cache[chat_id] = pid
            self._publish(
                ChannelPhotoChanged(channel_id=chat_id, local_path=str(path))
            )
        except Exception:  # noqa: BLE001
            log.debug("chat photo download failed for %s", chat_id, exc_info=True)

    async def _on_views(self, update: types.UpdateChannelMessageViews) -> None:
        try:
            marked = utils.get_peer_id(types.PeerChannel(update.channel_id))
            self._publish(
                MessageInteractionsChanged(
                    channel_id=marked,
                    telegram_msg_id=int(update.id),
                    views=int(update.views),
                    reactions=None,
                )
            )
        except Exception:  # noqa: BLE001
            log.exception("UpdateChannelMessageViews handling failed")

    async def _on_reactions(self, update: types.UpdateMessageReactions) -> None:
        try:
            if update.peer is None:
                return
            marked = utils.get_peer_id(update.peer)
            self._publish(
                MessageInteractionsChanged(
                    channel_id=marked,
                    telegram_msg_id=int(update.msg_id),
                    views=None,
                    reactions=map_reactions(update.reactions),
                )
            )
        except Exception:  # noqa: BLE001
            log.exception("UpdateMessageReactions handling failed")

    async def _on_pinned_channel(self, update: types.UpdatePinnedChannelMessages) -> None:
        try:
            marked = utils.get_peer_id(types.PeerChannel(update.channel_id))
            pinned = bool(update.pinned)
            for mid in update.messages or []:
                self._publish(
                    MessagePinChanged(
                        channel_id=marked, telegram_msg_id=int(mid), is_pinned=pinned
                    )
                )
        except Exception:  # noqa: BLE001
            log.exception("UpdatePinnedChannelMessages handling failed")

    async def _on_pinned(self, update: types.UpdatePinnedMessages) -> None:
        try:
            if update.peer is None:
                return
            marked = utils.get_peer_id(update.peer)
            pinned = bool(update.pinned)
            for mid in update.messages or []:
                self._publish(
                    MessagePinChanged(
                        channel_id=marked, telegram_msg_id=int(mid), is_pinned=pinned
                    )
                )
        except Exception:  # noqa: BLE001
            log.exception("UpdatePinnedMessages handling failed")

    async def _on_channel_updated(self, update: types.UpdateChannel) -> None:
        marked = utils.get_peer_id(types.PeerChannel(update.channel_id))
        await self._refresh_channel_metadata(marked)

    async def _on_chat_updated(self, update: types.UpdateChat) -> None:
        marked = utils.get_peer_id(types.PeerChat(update.chat_id))
        await self._refresh_channel_metadata(marked)

    async def _refresh_channel_metadata(self, marked: int) -> None:
        """UpdateChannel/UpdateChat 只带 id — 重取 entity 发 ChannelMetadataChanged。"""
        try:
            assert self._client is not None
            entity = await self._client.get_entity(marked)
            dto = self._entity_to_dto(entity)
            if dto is None:
                return
            self._publish(
                ChannelMetadataChanged(
                    channel_id=dto.id,
                    title=dto.title,
                    username=dto.username,
                    member_count=dto.member_count,
                    is_verified=dto.is_verified,
                    is_scam=dto.is_scam,
                    is_fake=dto.is_fake,
                    has_protected_content=dto.has_protected_content,
                )
            )
        except Exception:  # noqa: BLE001
            log.debug("channel metadata refresh failed for %s", marked, exc_info=True)

    # ============================================================
    # 频道
    # ============================================================

    @staticmethod
    def _entity_to_dto(entity: Any) -> ChannelDTO | None:
        """Channel / Chat → ChannelDTO;User / 其它 → None(同步功能不覆盖私聊)。"""
        if isinstance(entity, types.Channel):
            username = entity.username
            if not username and entity.usernames:
                username = entity.usernames[0].username
            mc = entity.participants_count
            return ChannelDTO(
                id=utils.get_peer_id(types.PeerChannel(entity.id)),
                title=entity.title or "",
                username=username,
                kind="channel" if entity.broadcast else "supergroup",
                member_count=mc if mc else None,
                created_at=entity.date,
                is_verified=bool(entity.verified),
                is_scam=bool(entity.scam),
                is_fake=bool(entity.fake),
                has_protected_content=bool(entity.noforwards),
            )
        if isinstance(entity, types.Chat):
            mc = entity.participants_count
            return ChannelDTO(
                id=utils.get_peer_id(types.PeerChat(entity.id)),
                title=entity.title or "",
                username=None,
                kind="basic_group",
                member_count=mc if mc else None,
                created_at=entity.date,
            )
        return None

    async def list_joined_channels(self) -> list[ChannelDTO]:
        """已加入频道列表 — best-effort UX(closing / 未 ready 返 [],不抛)。"""
        if self._closing:
            log.info("list_joined_channels: client closing, returning []")
            return []
        if self._state != "ready":
            try:
                await self._wait_for_state("ready", timeout=self._READY_WAIT_TIMEOUT)
            except TimeoutError:
                log.debug(
                    "list_joined_channels: state=%r (未到 ready,%.1fs 超时)",
                    self._state,
                    self._READY_WAIT_TIMEOUT,
                )
                return []
            if self._state != "ready":
                return []
        result: list[ChannelDTO] = []
        try:
            assert self._client is not None
            async for dialog in self._client.iter_dialogs():
                self._check_alive()
                dto = self._entity_to_dto(dialog.entity)
                if dto is not None:
                    result.append(dto)
        except ClientClosingError:
            log.info("list_joined_channels: aborted (client closing)")
        except Exception:  # noqa: BLE001
            log.exception("list_joined_channels failed")
        log.info("list_joined_channels done: %d channels", len(result))
        return result

    async def get_channel_metadata(self, channel_id: int) -> ChannelDTO:
        """拉一个频道的最新元数据(ChannelSyncService 用)。"""
        self._check_alive()
        assert self._client is not None
        try:
            entity = await self._client.get_entity(channel_id)
        except (ValueError, TypeError):
            return ChannelDTO(id=channel_id, title=f"#{channel_id}")
        dto = self._entity_to_dto(entity)
        if dto is None:
            return ChannelDTO(id=channel_id, title=f"#{channel_id}")
        if isinstance(entity, types.Channel):
            try:
                full = await self._client(
                    functions.channels.GetFullChannelRequest(entity)
                )
                mc = getattr(full.full_chat, "participants_count", None)
                if mc:
                    dto.member_count = mc
            except Exception:  # noqa: BLE001
                log.debug("GetFullChannel failed for %s", channel_id, exc_info=True)
        return dto

    async def join_channel(self, identifier: str) -> ChannelDTO:
        """identifier: @username / t.me/... 链接 / 私有邀请链接(t.me/+hash)。"""
        self._check_alive()
        assert self._client is not None
        s = identifier.strip()
        hash_part: str | None = None
        if "/+" in s:
            hash_part = s.split("/+", 1)[1]
        elif "/joinchat/" in s:
            hash_part = s.split("/joinchat/", 1)[1]
        if hash_part:
            updates = await self._client(
                functions.messages.ImportChatInviteRequest(hash_part.strip("/"))
            )
            for ch in getattr(updates, "chats", None) or []:
                dto = self._entity_to_dto(ch)
                if dto is not None:
                    return dto
            raise RuntimeError(f"ImportChatInvite 返回无可用 chat: {identifier!r}")
        username = s
        if username.startswith("@"):
            username = username[1:]
        elif "t.me/" in username:
            username = username.split("t.me/", 1)[1]
        username = username.strip("/")
        entity = await self._client.get_entity(username)
        if isinstance(entity, types.Channel):
            await self._client(functions.channels.JoinChannelRequest(entity))
        dto = self._entity_to_dto(entity)
        if dto is None:
            raise RuntimeError(f"无法解析频道: {identifier!r}")
        return dto

    def iter_chat_history(
        self,
        channel_id: int,
        *,
        before_msg_id: int = 0,
        limit: int = 100,
    ) -> AsyncIterator[MessageDTO]:
        """分页拉取频道历史(向旧方向)。async generator 直接返回保 lazy。

        `limit` 仅作页大小提示 — Telethon `iter_messages` 内部自动分页,
        这里 yield 全量历史(ChannelSyncService 期望全量,自己做节流)。
        """
        return self._iter_chat_history(
            channel_id, before_msg_id=before_msg_id, limit=limit
        )

    async def _iter_chat_history(
        self, channel_id: int, *, before_msg_id: int, limit: int
    ) -> AsyncIterator[MessageDTO]:
        self._check_alive()
        assert self._client is not None
        # 预热:先把 chat 加载进缓存,把"频道不可访问"提前转成 ChatUnavailableError。
        try:
            entity = await self._client.get_entity(channel_id)
        except ClientClosingError:
            raise
        except (errors.ChannelPrivateError, errors.ChannelInvalidError) as e:
            raise ChatUnavailableError(
                channel_id, reason=_extract_error_detail(e)
            ) from e
        except (ValueError, TypeError) as e:
            raise ChatUnavailableError(channel_id, reason=str(e)) from e
        try:
            async for msg in self._client.iter_messages(
                entity, limit=None, offset_id=before_msg_id
            ):
                self._check_alive()
                yield map_message(msg)
        except errors.FloodWaitError as e:
            raise TelegramRateLimitError(e.seconds) from e

    # ============================================================
    # 媒体下载
    # ============================================================

    async def download_file(
        self,
        file_id: str,
        *,
        progress_callback: Callable[[int, int | None], Awaitable[None]] | None = None,
    ) -> bytes | None:
        """下载原文件 bytes;失败 / 超时返 None,**不抛**(ClientClosingError 除外)。

        file_id 是消息定位符(`tl:{chat_id}:{msg_id}[:thumb]`)。重取消息 →
        download_media(Telethon 自动刷新过期 file_reference)。30 min hard cap。
        """
        self._check_alive()
        parsed = parse_file_id(file_id)
        if parsed is None:
            log.warning("download_file: invalid file_id %r", file_id)
            return None
        chat_id, msg_id, is_thumb = parsed
        assert self._client is not None
        try:
            msg = await self._client.get_messages(chat_id, ids=msg_id)
        except ClientClosingError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("download_file: get_messages(%s,%s) failed: %s", chat_id, msg_id, e)
            return None
        if msg is None or getattr(msg, "media", None) is None:
            log.warning("download_file: message %s has no media", file_id)
            return None
        thumb = self._pick_thumb_arg(msg.media) if is_thumb else None
        wrapper = (
            self._make_progress_wrapper(progress_callback)
            if progress_callback is not None
            else None
        )
        try:
            data = await asyncio.wait_for(
                self._client.download_media(
                    msg, file=bytes, thumb=thumb, progress_callback=wrapper
                ),
                timeout=self._DOWNLOAD_TIMEOUT,
            )
        except ClientClosingError:
            raise
        except TimeoutError:
            log.warning("download_file(%s) timed out after 30 min", file_id)
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("download_file(%s) failed: %s", file_id, e)
            return None
        if data is None:
            return None
        if progress_callback is not None:
            try:
                await progress_callback(len(data), len(data))
            except Exception:  # noqa: BLE001
                log.exception("progress final emit failed")
        return data

    @staticmethod
    def _pick_thumb_arg(media: Any) -> Any:
        """缩略图下载参数:document → -1(最大 jpeg);photo → ~320px PhotoSize。"""
        if isinstance(media, types.MessageMediaDocument):
            return -1
        if isinstance(media, types.MessageMediaPhoto) and isinstance(
            media.photo, types.Photo
        ):
            return pick_thumb_size(media.photo) or 0
        return None

    @staticmethod
    def _make_progress_wrapper(
        cb: Callable[[int, int | None], Awaitable[None]],
    ) -> Callable[[int, int | None], None]:
        """Telethon 同步 progress_callback → 异步 cb(0.5s 节流,create_task 投递)。"""
        pending: set[asyncio.Task] = set()
        last = 0.0

        def wrapper(downloaded: int, total: int | None) -> None:
            nonlocal last
            now = time.monotonic()
            if now - last < 0.5:
                return
            last = now

            async def _emit() -> None:
                await cb(downloaded, total)

            try:
                task = asyncio.create_task(_emit())
                pending.add(task)
                task.add_done_callback(pending.discard)
            except RuntimeError:
                pass

        return wrapper

    # ============================================================
    # 订阅流
    # ============================================================

    def subscribe_updates(self) -> UpdateStream:
        """订阅实时更新流;`aclose()` 必调一次,否则 _streams 只增不减。"""
        s = _TelethonUpdateStream(on_close=self._remove_stream)
        self._streams.append(s)
        return s

    def _remove_stream(self, s: _TelethonUpdateStream) -> None:
        try:
            self._streams.remove(s)
        except ValueError:
            pass

    # ============================================================
    # 批量操作
    # ============================================================

    async def mark_messages_read(self, channel_id: int, msg_ids: list[int]) -> None:
        """批量标已读 — send_read_acknowledge(max_id=最大 id)。"""
        if not msg_ids:
            return
        self._check_alive()
        assert self._client is not None
        await self._client.send_read_acknowledge(channel_id, max_id=max(msg_ids))

    async def forward_messages(
        self, from_chat_id: int, to_chat_id: int, msg_ids: list[int]
    ) -> None:
        """批量转发 — forward_messages(int ids 必须带 from_peer)。"""
        if not msg_ids:
            return
        self._check_alive()
        assert self._client is not None
        await self._client.forward_messages(
            to_chat_id, sorted(msg_ids), from_peer=from_chat_id
        )

    async def pin_messages(
        self, channel_id: int, msg_ids: list[int], *, only_for_self: bool = True
    ) -> None:
        """批量钉选 — 一次一条 pin_message(notify=False, pm_oneside=only_for_self)。"""
        if not msg_ids:
            return
        self._check_alive()
        assert self._client is not None
        for mid in msg_ids:
            await self._client.pin_message(
                channel_id, mid, notify=False, pm_oneside=only_for_self
            )

    async def unpin_messages(self, channel_id: int, msg_ids: list[int]) -> None:
        """批量取消钉选 — 一次一条 unpin_message。"""
        if not msg_ids:
            return
        self._check_alive()
        assert self._client is not None
        for mid in msg_ids:
            await self._client.unpin_message(channel_id, mid)

    async def add_reaction(
        self, channel_id: int, msg_id: int, reaction: str, *, is_big: bool = False
    ) -> None:
        """emoji 回应 — SendReactionRequest(reaction=[parsed])。"""
        if not reaction:
            return
        self._check_alive()
        assert self._client is not None
        peer = await self._client.get_input_entity(channel_id)
        await self._client(
            functions.messages.SendReactionRequest(
                peer=peer,
                msg_id=msg_id,
                reaction=[self._parse_reaction(reaction)],
                big=is_big,
            )
        )

    async def remove_reaction(self, channel_id: int, msg_id: int, reaction: str) -> None:
        """取消回应 — Telethon 无单条 remove:重取消息 → 过滤掉目标 → 重发剩余。"""
        if not reaction:
            return
        self._check_alive()
        assert self._client is not None
        peer = await self._client.get_input_entity(channel_id)
        target = self._parse_reaction(reaction)
        msg = await self._client.get_messages(channel_id, ids=msg_id)
        remaining: list[Any] = []
        reactions = getattr(msg, "reactions", None) if msg is not None else None
        if reactions is not None:
            for rc in reactions.results or []:
                if rc.chosen_order is None:
                    continue  # 不是本端选的
                if self._reaction_matches(rc.reaction, target):
                    continue  # 丢弃要取消的那条
                remaining.append(rc.reaction)
        await self._client(
            functions.messages.SendReactionRequest(
                peer=peer, msg_id=msg_id, reaction=remaining
            )
        )

    @staticmethod
    def _parse_reaction(reaction: str) -> Any:
        """`"custom_emoji_id:<id>"` → ReactionCustomEmoji;其它 → ReactionEmoji。"""
        prefix = "custom_emoji_id:"
        if reaction.startswith(prefix):
            return types.ReactionCustomEmoji(document_id=int(reaction[len(prefix) :]))
        return types.ReactionEmoji(emoticon=reaction)

    @staticmethod
    def _reaction_matches(a: Any, b: Any) -> bool:
        if isinstance(a, types.ReactionEmoji) and isinstance(b, types.ReactionEmoji):
            return a.emoticon == b.emoticon
        if isinstance(a, types.ReactionCustomEmoji) and isinstance(
            b, types.ReactionCustomEmoji
        ):
            return a.document_id == b.document_id
        return False