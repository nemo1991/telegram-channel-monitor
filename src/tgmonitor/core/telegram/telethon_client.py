# mypy: disable-error-code="misc,assignment"
"""TelethonTelegramClient — v2.0.0 生产实现(Telethon 后端,替换 TDLib)。

实现 `client.TelegramClient` Protocol 全量方法。与 TDLib 版的关键差异:

# 鉴权:二维码扫码(唯一登录方式)
2026-11-21 移除手机号+验证码登录链(下发渠道不可控导致收不到码)。登录只走
`qr_login()` → `QRLogin.wait()`:生成二维码 → 已登录的 Telegram App 扫码
授权 → `_on_login_success()` 转 ready。无需手机号/短信/验证码,也就不需要
TDLib 式的状态机输入队列(`_code_queue` 等)。

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
from telethon.tl.custom.qrlogin import QRLogin

from tgmonitor import __version__
from tgmonitor.core.config import Settings
from tgmonitor.core.dto import ChannelDTO, MessageDTO
from tgmonitor.core.events import (
    AuthErrorOccurred,
    ConnectionStateChanged,
    LoginStateChanged,
)
from tgmonitor.core.telegram import _telethon_handlers as _handlers
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
    parse_file_id,
    pick_thumb_size,
)

log = logging.getLogger(__name__)


class _MaxBytesExceeded(Exception):
    """download_file_to 的 max_bytes 中止哨兵 — 在 Telethon progress_callback
    (每 chunk 同步调用)内抛出,沿分块循环冒泡实现下载中途止损。"""


class _TelethonUpdateStream(UpdateStream):
    """Telethon update handler → asyncio.Queue → UI。

    与 TDLib 版 `_TdlibJsonUpdateStream` 同契约:`aclose()` 幂等、塞 sentinel
    让 `__anext__` 抛 StopAsyncIteration、触发 `on_close` 把自己从
    `client._streams` 拿掉(防长会话列表只增不减)。

    反压策略(2026-11-25 修 P2-29,预决:宁慢勿丢):Queue 有界
    (`maxsize=2000`),生产端(handler task)`put` 满时 **await 阻塞**等待
    消费端消化 — 反压沿 Telethon update 分发链上游传导,不丢消息。
    代价是高频频道 + 消费慢时消息延迟上升;过半(>1500)时 warning
    一次提示消费端跟不上(只发一次防刷屏)。
    """

    _QUEUE_MAXSIZE = 2000
    _QUEUE_WARN_AT = 1500

    def __init__(self, on_close: Callable[[_TelethonUpdateStream], None] | None = None) -> None:
        self._queue: asyncio.Queue[MessageDTO | None] = asyncio.Queue(maxsize=self._QUEUE_MAXSIZE)
        self._closed = False
        self._on_close = on_close
        self._warned_full = False

    async def push(self, msg: MessageDTO) -> None:
        if not self._closed:
            if self._queue.qsize() > self._QUEUE_WARN_AT and not self._warned_full:
                self._warned_full = True
                log.warning(
                    "update stream backlog %d > %d — 消费端跟不上,生产端将阻塞反压 (宁慢勿丢)",
                    self._queue.qsize(),
                    self._QUEUE_WARN_AT,
                )
            # 满时阻塞等待消费端腾位(反压,不丢消息)。
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
      - 鉴权错误经 `AuthErrorOccurred` 事件通知 UI,**不**改顶层状态
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

        # watchdog 重连守卫(2026-11-25 修 P2-28):connect 进行中置 True,
        # 跳过后续检测并防并发重入;完成后清。
        self._reconnecting = False

        # 顶层状态机 — 初值 uninit,真值由 start() 后的授权检查决定。
        self._state: str = "uninit"
        self._state_detail: str = ""
        self._state_event = asyncio.Event()

        # 二维码登录会话(await qr_login() 拿 url → wait 持续等扫码)。
        self._qr_login: QRLogin | None = None

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
        client.add_event_handler(self._on_views, events.Raw(types.UpdateChannelMessageViews))
        client.add_event_handler(self._on_reactions, events.Raw(types.UpdateMessageReactions))
        client.add_event_handler(
            self._on_pinned_channel, events.Raw(types.UpdatePinnedChannelMessages)
        )
        client.add_event_handler(self._on_pinned, events.Raw(types.UpdatePinnedMessages))
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
            self._set_state(
                "error", detail=f"启动超时({self._BOOT_TIMEOUT:.0f}s)— 网络或代理不可达"
            )
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

        重连守卫(2026-11-25 修 P2-28):`_reconnecting` 标志保证同一时刻
        只有一个 watchdog 发起的 connect 在跑。Telethon 自身有内部锁
        (auto_reconnect 与手动 connect 理论上互斥),但这是 Telethon 内部
        实现细节、不写进公开契约 — 显式守卫不赌内部实现,杜绝 watchdog
        主动 connect 与 auto_reconnect 竞态。
        """
        last: str | None = None
        try:
            while not self._closing and self._state == "ready":
                if self._reconnecting:
                    # connect 进行中:跳过本轮检测,防并发重入。
                    await asyncio.sleep(self._WATCHDOG_INTERVAL)
                    continue
                connected = bool(self._client and self._client.is_connected())
                cur = "ready" if connected else "waiting_for_network"
                if cur != last:
                    self._publish(ConnectionStateChanged(state=cur))
                    last = cur
                if not connected and self._client is not None:
                    self._reconnecting = True
                    try:
                        await self._client.connect()
                    except Exception:  # noqa: BLE001
                        log.debug("watchdog reconnect failed", exc_info=True)
                    finally:
                        self._reconnecting = False
                await asyncio.sleep(self._WATCHDOG_INTERVAL)
        except asyncio.CancelledError:
            pass

    # ============================================================
    # 鉴权(二维码扫码 — 本版本唯一登录方式)
    # ============================================================

    async def start_qr_login(self) -> tuple[str, str | None]:
        """生成二维码登录 URL — 无需手机号,靠已登录的 Telegram App 扫码授权。

        - 不需要 TG_PHONE(只依赖 client 构造期的 api_id/api_hash)
        - 不经过短信/App 下发渠道,码不会"发不到"
        - URL 含一次性 token,**只走返回值给 UI 渲染**,不写进 detail
          (detail 会进日志)以免 token 落盘

        二维码有有效期(Telegram 通常 30 分钟);过期直接再次 `start_qr_login()`
        换新 token。
        """
        self._check_alive()
        assert self._client is not None
        try:
            # QR 需要 client 已连接(ExportLoginTokenRequest 是网络调用)。
            # bootstrap 后已连接;stop()/冷启动未连时补一次 connect。
            if not self._client.is_connected():
                await self._client.connect()
            # Telethon 的 qr_login() 是 coroutine,内部已 await recreate() 生成
            # token/url。必须 await 拿到 QRLogin 对象;漏 await 会在 coroutine
            # 上找 recreate 报 AttributeError(且 coroutine 永不 await 告警)。
            qr = await self._client.qr_login()
            self._qr_login = qr
            self._set_state("qr_required", detail="请用 Telegram App 扫码授权")
            # URL 含 token,只经返回值给 UI,不进 _set_state detail / 日志。
            return self._state, qr.url
        except Exception as e:  # noqa: BLE001
            self._qr_login = None
            self._auth_error("start_qr_login", e)
            return "error", _extract_error_detail(e)

    async def wait_qr_login(self) -> tuple[str, str | None]:
        """在二维码展示期间持续等待扫码 — 成功走既有 ready 推进路径。

        Telethon `QRLogin.wait()` 内部监听 `UpdateLoginToken`,扫码后完成
        session 保存;这里接 `_on_login_success()` 拉 me / 转 ready / 起 watchdog。
        Telethon 按二维码过期时间自动截止;到点抛 TimeoutError → 返回 error,
        UI 提示刷新二维码。
        """
        self._check_alive()
        qr = self._qr_login
        if qr is None:
            return "error", "二维码尚未生成,请先点击「二维码登录」"
        try:
            await qr.wait()
        except TimeoutError:
            self._qr_login = None
            return "error", "二维码已过期,请刷新后重新扫码"
        except asyncio.CancelledError:
            # UI 取消 / 关闭弹窗 → 清会话,异常上抛让调用方的 run_coro 收尾。
            self._qr_login = None
            raise
        except Exception as e:  # noqa: BLE001
            self._qr_login = None
            self._auth_error("wait_qr_login", e)
            return "error", _extract_error_detail(e)
        self._qr_login = None
        await self._on_login_success()
        return self._state, self._state_detail

    async def logout(self) -> None:
        """登出 — log_out() + 删 session + 重建 client + connect → phone_required(未登录)。

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
        self._client = self._build_client()
        try:
            await self._client.connect()
            self._set_state("phone_required", detail="已登出,请用二维码重新登录")
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
        # 2026-11-25 修 P2-25:走 _set_state 唯一写入口(原先直写 _state 绕过
        # 状态机、不发 LoginStateChanged,UI 可能停留 ready)。detail="" —
        # uninit 无附加信息,不会发假 ready 事件。
        self._set_state("uninit")

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

    # 2026-11-25 P2-N5a:handler 实现体抽到 `_telethon_handlers.py`(free function
    # + host 透传);下列方法保留原名(Telethon 注册 + 测试直接调用依赖它),
    # 只做 1 行薄委托。实现语义逐字不变。

    async def _on_new_message(self, event: Any) -> None:
        await _handlers.on_new_message(self, event)

    async def _on_message_edited(self, event: Any) -> None:
        await _handlers.on_message_edited(self, event)

    async def _on_message_deleted(self, event: Any) -> None:
        await _handlers.on_message_deleted(self, event)

    async def _on_chat_action(self, event: Any) -> None:
        await _handlers.on_chat_action(self, event)

    async def _handle_chat_photo(self, chat_id: int, photo: Any) -> None:
        await _handlers.handle_chat_photo(self, chat_id, photo)

    async def _on_views(self, update: types.UpdateChannelMessageViews) -> None:
        await _handlers.on_views(self, update)

    async def _on_reactions(self, update: types.UpdateMessageReactions) -> None:
        await _handlers.on_reactions(self, update)

    async def _on_pinned_channel(self, update: types.UpdatePinnedChannelMessages) -> None:
        await _handlers.on_pinned_channel(self, update)

    async def _on_pinned(self, update: types.UpdatePinnedMessages) -> None:
        await _handlers.on_pinned(self, update)

    async def _on_channel_updated(self, update: types.UpdateChannel) -> None:
        await _handlers.on_channel_updated(self, update)

    async def _on_chat_updated(self, update: types.UpdateChat) -> None:
        await _handlers.on_chat_updated(self, update)

    async def _refresh_channel_metadata(self, marked: int) -> None:
        await _handlers.refresh_channel_metadata(self, marked)

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
        """拉一个频道的最新元数据(ChannelSyncService 用)。

        失败(拿不到 entity / entity 无法映射 DTO)抛 `ChatUnavailableError`,
        不再返回 `#<id>` stub DTO(修 P1-8:stub 经覆盖式 upsert 会把
        真实频道标题 / username / member_count 清空)。
        """
        self._check_alive()
        assert self._client is not None
        try:
            entity = await self._client.get_entity(channel_id)
        except (ValueError, TypeError) as e:
            raise ChatUnavailableError(channel_id, reason=f"get_entity failed: {e}") from e
        dto = self._entity_to_dto(entity)
        if dto is None:
            raise ChatUnavailableError(
                channel_id, reason=f"entity type {type(entity).__name__} 无法映射 ChannelDTO"
            )
        if isinstance(entity, types.Channel):
            try:
                full = await self._client(functions.channels.GetFullChannelRequest(entity))
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
        limit: int | None = None,
    ) -> AsyncIterator[MessageDTO]:
        """分页拉取频道历史(向旧方向)。async generator 直接返回保 lazy。

        `limit=None`(默认)全量拉;`limit=N` 从起点向旧方向最多 N 条
        (2026-11-25 修 P2-23:此前 limit 收了不传、恒全量,与 Protocol
        文档"最新 N 条"矛盾)。节流仍由 caller(ChannelSyncService)负责。
        """
        return self._iter_chat_history(channel_id, before_msg_id=before_msg_id, limit=limit)

    async def _iter_chat_history(
        self, channel_id: int, *, before_msg_id: int, limit: int | None
    ) -> AsyncIterator[MessageDTO]:
        self._check_alive()
        assert self._client is not None
        # 预热:先把 chat 加载进缓存,把"频道不可访问"提前转成 ChatUnavailableError。
        try:
            entity = await self._client.get_entity(channel_id)
        except ClientClosingError:
            raise
        except (errors.ChannelPrivateError, errors.ChannelInvalidError) as e:
            raise ChatUnavailableError(channel_id, reason=_extract_error_detail(e)) from e
        except (ValueError, TypeError) as e:
            raise ChatUnavailableError(channel_id, reason=str(e)) from e
        try:
            async for msg in self._client.iter_messages(
                entity, limit=limit, offset_id=before_msg_id
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

        整文件进内存(2026-11-25 P2-27):只给缩略图等小文件用;主媒体
        大文件请用 `download_file_to`(流式落盘 + max_bytes 提前止损)。
        """
        fetched = await self._fetch_media_message(file_id, op="download_file")
        if fetched is None:
            return None
        msg, thumb = fetched
        assert self._client is not None  # fetch 成功即存在
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

    async def download_file_to(
        self,
        file_id: str,
        dest_path: Path,
        *,
        progress_callback: Callable[[int, int | None], Awaitable[None]] | None = None,
        max_bytes: int | None = None,
    ) -> Path | None:
        """流式下载媒体到 `dest_path`;失败 / 超时 / 超 max_bytes 返 None,**不抛**。

        契约见 Protocol docstring。实现要点:
          - `download_media(msg, file=dest_path)` 原生落盘,内存恒定。
          - `max_bytes` 超限**中途中止**:Telethon 的 progress_callback 每
            chunk 同步调用(telethon/client/downloads.py `_download_file`),
            回调内抛异常沿分块循环冒泡 → 下载停止、文件句柄经 finally 关闭;
            然后删掉半成品返 None(核实于 telethon downloads.py 源码)。
          - dest_path 无后缀时 Telethon `_get_proper_filename` 会自动补
            `.jpg` / `.mp4` 等 → 下载完把补名产物 rename 归位回 dest_path。
        """
        fetched = await self._fetch_media_message(file_id, op="download_file_to")
        if fetched is None:
            return None
        msg, thumb = fetched
        assert self._client is not None  # fetch 成功即存在
        wrapper = self._make_dl_to_progress(progress_callback, max_bytes)
        try:
            out = await asyncio.wait_for(
                self._client.download_media(
                    msg, file=dest_path, thumb=thumb, progress_callback=wrapper
                ),
                timeout=self._DOWNLOAD_TIMEOUT,
            )
        except ClientClosingError:
            raise
        except _MaxBytesExceeded:
            log.warning("download_file_to(%s) aborted: exceeds max_bytes=%d", file_id, max_bytes)
            self._cleanup_partial(dest_path)
            return None
        except TimeoutError:
            log.warning("download_file_to(%s) timed out after 30 min", file_id)
            self._cleanup_partial(dest_path)
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("download_file_to(%s) failed: %s", file_id, e)
            self._cleanup_partial(dest_path)
            return None
        if out is None:
            self._cleanup_partial(dest_path)
            return None
        return await self._finalize_download_to(out, dest_path, progress_callback)

    async def _fetch_media_message(self, file_id: str, *, op: str) -> tuple[Any, Any] | None:
        """解析 file_id → 取消息 → (msg, thumb 参数);任何失败返 None(不抛)。

        供 download_file / download_file_to 共用;失败细节进 warning 日志。
        """
        self._check_alive()
        parsed = parse_file_id(file_id)
        if parsed is None:
            log.warning("%s: invalid file_id %r", op, file_id)
            return None
        chat_id, msg_id, is_thumb = parsed
        assert self._client is not None
        try:
            msg = await self._client.get_messages(chat_id, ids=msg_id)
        except ClientClosingError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("%s: get_messages(%s,%s) failed: %s", op, chat_id, msg_id, e)
            return None
        if msg is None or getattr(msg, "media", None) is None:
            log.warning("%s: message %s has no media", op, file_id)
            return None
        thumb = self._pick_thumb_arg(msg.media) if is_thumb else None
        return msg, thumb

    @staticmethod
    def _make_dl_to_progress(
        progress_callback: Callable[[int, int | None], Awaitable[None]] | None,
        max_bytes: int | None,
    ) -> Callable[[int, int | None], None] | None:
        """download_file_to 的 Telethon 同步 progress_callback。

        max_bytes 非 None 时包一层逐 chunk 守卫 — 超限抛 `_MaxBytesExceeded`
        中止下载(Telethon 分块循环不吞回调异常);用户 callback 仍走 0.5s
        节流包装。
        """
        if max_bytes is None:
            return (
                TelethonTelegramClient._make_progress_wrapper(progress_callback)
                if progress_callback is not None
                else None
            )
        throttled = (
            TelethonTelegramClient._make_progress_wrapper(progress_callback)
            if progress_callback is not None
            else None
        )

        def guarded_progress(downloaded: int, total: int | None) -> None:
            if downloaded > max_bytes:
                raise _MaxBytesExceeded(f"{downloaded} > {max_bytes}")
            if throttled is not None:
                throttled(downloaded, total)

        return guarded_progress

    async def _finalize_download_to(
        self,
        out: Any,
        dest_path: Path,
        progress_callback: Callable[[int, int | None], Awaitable[None]] | None,
    ) -> Path | None:
        """下载成功后的收尾:补名归位 → 存在性校验 → 终值回调 → 返 dest_path。

        `out` 是 Telethon `download_media` 的返回值(落盘路径 str/Path 或 None)。
        """
        # Telethon 对无后缀 dest 会补扩展名写别处 — 把产物归位回契约 dest_path。
        # 本地小文件 exists/replace/stat 同步操作,与 export/ 各导出器同款豁免。
        if isinstance(out, (str, Path)):
            final = Path(out)
            if final != dest_path and final.exists():  # noqa: ASYNC240 — 本地产物改名,同步即可
                try:
                    final.replace(dest_path)  # noqa: ASYNC240 — 同上
                except OSError:
                    log.warning("download_file_to: rename %s → %s failed", final, dest_path)
                    self._cleanup_partial(final)
                    return None
        if not dest_path.exists():  # noqa: ASYNC240 — 本地文件存在性,同步即可
            self._cleanup_partial(dest_path)
            return None
        if progress_callback is not None:
            try:
                size = dest_path.stat().st_size  # noqa: ASYNC240 — 本地 stat,同步即可
                await progress_callback(size, size)
            except Exception:  # noqa: BLE001
                log.exception("progress final emit failed")
        return dest_path

    @staticmethod
    def _cleanup_partial(path: Path) -> None:
        """失败 / 超时 / 超限后删半成品(不存在则静默)。"""
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("download_file_to: failed to remove partial %s", path)

    @staticmethod
    def _pick_thumb_arg(media: Any) -> Any:
        """缩略图下载参数:document → -1(最大 jpeg);photo → ~320px PhotoSize。"""
        if isinstance(media, types.MessageMediaDocument):
            return -1
        if isinstance(media, types.MessageMediaPhoto) and isinstance(media.photo, types.Photo):
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

    async def forward_messages(
        self, from_chat_id: int, to_chat_id: int, msg_ids: list[int]
    ) -> None:
        """批量转发 — forward_messages(int ids 必须带 from_peer)。"""
        if not msg_ids:
            return
        self._check_alive()
        assert self._client is not None
        await self._client.forward_messages(to_chat_id, sorted(msg_ids), from_peer=from_chat_id)

    async def pin_messages(
        self, channel_id: int, msg_ids: list[int], *, only_for_self: bool = True
    ) -> None:
        """批量钉选 — 一次一条 pin_message(notify=False, pm_oneside=only_for_self)。"""
        if not msg_ids:
            return
        self._check_alive()
        assert self._client is not None
        for mid in msg_ids:
            await self._client.pin_message(channel_id, mid, notify=False, pm_oneside=only_for_self)

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
            functions.messages.SendReactionRequest(peer=peer, msg_id=msg_id, reaction=remaining)
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
        if isinstance(a, types.ReactionCustomEmoji) and isinstance(b, types.ReactionCustomEmoji):
            return a.document_id == b.document_id
        return False
