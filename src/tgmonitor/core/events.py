"""EventBus — core 内部 + core↔UI 的事件通道。

设计:
- 全部 `async`,UI 在订阅回调里通过 Qt signal 转线程安全更新
- 事件载荷用 `Event` 子类,字段公开
- 订阅者抛异常被吞掉 + 日志,不互相影响
- 无第三方依赖,纯 asyncio(避免 aio-pika / redis 之类)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, TypeVar

from tgmonitor.core.dto import ChannelDTO, ExportResult, MessageDTO

log = logging.getLogger(__name__)

T = TypeVar("T", bound="Event")


# ---------- 领域事件 ----------

@dataclass
class Event:
    """所有领域事件的基类。"""

    occurred_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class LoginStateChanged(Event):
    """登录状态机状态变化。"""

    state: str = "unknown"   # phone_required | code_required | password_required | ready | error
    detail: str = ""


@dataclass
class ChannelDiscovered(Event):
    """TelegramClient 枚举到的新频道(尚未加入监听白名单)。"""

    channel: ChannelDTO | None = None


@dataclass
class ChannelSubscribed(Event):
    """用户将一个频道加入监听白名单。"""

    channel: ChannelDTO | None = None


@dataclass
class ChannelUnsubscribed(Event):
    channel_id: int = 0


@dataclass
class MessageReceived(Event):
    """一条新消息已成功落库(可被 UI 视为"立即可见")。"""

    message: MessageDTO | None = None


@dataclass
class MessageDeleted(Event):
    channel_id: int = 0
    telegram_msg_id: int = 0


@dataclass
class ExportProgress(Event):
    request_id: str = ""
    written: int = 0
    total: int | None = None


@dataclass
class ExportDone(Event):
    request_id: str = ""
    result: ExportResult | None = None
    error: str | None = None


@dataclass
class ErrorOccurred(Event):
    source: str = ""
    message: str = ""
    exception: BaseException | None = None


@dataclass
class AuthErrorOccurred(ErrorOccurred):
    """验证码 / 2FA 密码错误等 transient 鉴权错误。

    与顶层 `error` 状态的区别:验证码错不会把我们踢回 `phone_required` —
    aiotdlib 会自动重新进入 `WaitCode` 状态,用户在原地重输即可。
    UI 应该弹一个短暂的红色提示行(类似 toast),3 秒后自动消失。
    继承自 `ErrorOccurred`,这样订阅 `ErrorOccurred` 的代码也能收到。
    """

    # "code" | "password" | "phone" | "telegram_internal"
    source: str = "auth"


@dataclass
class SettingsChanged(Event):
    """设置已变更(已热重载的部分)。"""

    what: str = ""               # "storage" | "objectstore" | "credentials"
    new_settings: object | None = None  # Settings 实例(供 UI 同步)
    needs_relogin: bool = False  # True 表示 Telegram 凭据改了,需登出再登入
    needs_restart: bool = False  # 保留扩展


@dataclass
class ChannelSyncProgress(Event):
    """全量同步进度事件 — ChannelSyncService → UI(进度对话框)。

    stage 枚举:
      - "metadata"   : 拉取 / 刷新元数据
      - "history"    : 拉取历史消息
      - "backoff"    : 429 / FLOOD_WAIT 退避中
      - "done"       : 单频道完成
      - "failed"     : 单频道失败(error 字段非空)
    """
    channel_id: int = 0
    stage: str = ""
    progress: int = 0           # 已处理消息数
    total: int | None = None    # 总数(可空,history 全量无终点)
    detail: str = ""            # 退避秒数 / 错误消息等


@dataclass
class ChannelSyncDone(Event):
    """全量同步整轮结束 — UI 进度对话框据此自动关闭。"""
    result: object = None        # SyncResult(避免循环 import,用 object 占位)


# ---------- Bus ----------

Subscriber = Callable[[Any], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[type[Event], list[Subscriber]] = {}
        self._wild: list[Subscriber] = []  # 订阅所有事件

    def subscribe(self, event_type: type[T], fn: Subscriber) -> None:
        self._subs.setdefault(event_type, []).append(fn)

    def subscribe_all(self, fn: Subscriber) -> None:
        self._wild.append(fn)

    def unsubscribe(self, event_type: type[Event], fn: Subscriber) -> None:
        if event_type in self._subs:
            try:
                self._subs[event_type].remove(fn)
            except ValueError:
                pass

    async def publish(self, event: Event) -> None:
        # 基类匹配
        subs: list[Subscriber] = []
        for cls in type(event).__mro__:
            if cls is Event:
                break
            subs.extend(self._subs.get(cls, []))
        for fn in subs:
            try:
                await fn(event)
            except Exception:  # noqa: BLE001
                log.exception("event subscriber raised: %r", fn)
        for fn in self._wild:
            try:
                await fn(event)
            except Exception:  # noqa: BLE001
                log.exception("wildcard subscriber raised: %r", fn)

    def publish_threadsafe(self, loop: asyncio.AbstractEventLoop, event: Event) -> None:
        """从其它线程安全地发布事件(后台下载任务等用)。"""
        asyncio.run_coroutine_threadsafe(self.publish(event), loop)
