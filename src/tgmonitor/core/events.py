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
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, TypeVar

from tgmonitor.core.dto import ChannelDTO, ExportResult, MediaDTO, MessageDTO

log = logging.getLogger(__name__)

T = TypeVar("T", bound="Event")


# ---------- 领域事件 ----------


@dataclass
class Event:
    """所有领域事件的基类。"""

    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class LoginStateChanged(Event):
    """登录状态机状态变化。"""

    state: str = "unknown"  # phone_required | code_required | password_required | ready | error
    detail: str = ""


@dataclass
class ConnectionStateChanged(Event):
    """TDLib 网络连接状态变化(updateConnectionState)。"""

    state: str = "unknown"  # waiting_for_network | connecting | updating | ready | unknown


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
    """用户将一个频道从监听白名单移除。"""

    channel_id: int = 0


@dataclass
class MessageReceived(Event):
    """一条新消息已成功落库(可被 UI 视为"立即可见")。"""

    message: MessageDTO | None = None


@dataclass
class MessageEdited(Event):
    """消息文本 / 媒体被编辑(TDLib updateMessageContent)。

    编辑不走 updateNewMessage,所以不能依赖 live skip-if-stored 自动吸收 —
    必须有独立事件让 UI 知道「同一条 cell 内容变了」。
    """

    message: MessageDTO | None = None


@dataclass
class MessageDeleted(Event):
    """一条消息被删除(由 monitor 收到 `DeleteMessages` 更新)。"""

    channel_id: int = 0
    telegram_msg_id: int = 0


@dataclass
class MessageInteractionsChanged(Event):
    """2026-08-27 v1.4.0 PR #10:reactions / views 增量更新。

    TDLib `updateMessageInteractionInfo` 推送时,只携带 changed 的字段
    (可能是新 views、可能删/加 reaction),`views=None` 表示本次 update
    没新 view,`reactions=None` 表示没动 reactions。

    UI 订阅后实时刷新详情面板的 reactions badge / views 数字。
    """

    channel_id: int = 0
    telegram_msg_id: int = 0
    views: int | None = None
    reactions: object = None  # list[ReactionDTO] | None(避免循环 import,object 占位)


@dataclass
class MediaDownloaded(Event):
    """一条媒体下载结束(成功或失败都发;`media.download_status` 区分)。

    UI 据此刷新对应消息的展示(下载中 → 已下载 / 失败+原因)。
    """

    channel_id: int = 0
    telegram_msg_id: int = 0
    media: MediaDTO | None = None


@dataclass
class MediaDeleted(Event):
    """2026-08-24 Media Manager:一条 media 被用户从某 message 删除。

    UI 据此从 LIVE view 移除对应 cell(Media Manager 自己 reload)。
    """

    channel_id: int = 0
    telegram_msg_id: int = 0
    media_idx: int = -1


@dataclass
class MediaRetried(Event):
    """2026-08-24 Media Manager:一条 FAILED media 被用户触发重下。

    UI 收到可更新对应 cell 的状态显示(DOWNLOADING → 等下一次 MediaDownloaded)。
    """

    channel_id: int = 0
    telegram_msg_id: int = 0
    media_idx: int = -1


@dataclass
class MediaReconcileFinished(Event):
    """2026-08-24:orphan reconcile 一次扫描结束(无论 dry_run / 真删)。

    UI 据此更新 Media Manager footer 的「Prune Orphans」按钮状态 + 计数。
    """

    backend: str = ""  # 'local' / 'folder' / 's3'
    scanned: int = 0  # ObjectStore keys
    referenced: int = 0  # storage 命中
    orphans: int = 0  # 孤儿数(scanned - referenced)
    deleted: int = 0  # 实际 delete 数(dry_run 时为 0)
    dry_run: bool = True


@dataclass
class ExportProgress(Event):
    """导出进度事件 — ExportService 每拉一批 messages 发一次。"""

    request_id: str = ""
    written: int = 0
    total: int | None = None


@dataclass
class ExportDone(Event):
    """导出完成(成功 / 失败都发;失败时 `error` 字段非空)。"""

    request_id: str = ""
    result: ExportResult | None = None
    error: str | None = None


@dataclass
class ErrorOccurred(Event):
    """通用错误事件 — UI 显示错误提示用。

    鉴权错误(验证码 / 2FA)有专门子类 `AuthErrorOccurred`,它继承本类。
    """

    source: str = ""
    message: str = ""
    exception: BaseException | None = None


@dataclass
class AuthErrorOccurred(ErrorOccurred):
    """验证码 / 2FA 密码错误等 transient 鉴权错误。

    与顶层 `error` 状态的区别:验证码错不会把我们踢回 `phone_required` —
    TDLib 会自动重新进入 `WaitCode` 状态,用户在原地重输即可。
    UI 应该弹一个短暂的红色提示行(类似 toast),3 秒后自动消失。
    继承自 `ErrorOccurred`,这样订阅 `ErrorOccurred` 的代码也能收到。
    """

    # "code" | "password" | "phone" | "telegram_internal"
    source: str = "auth"


@dataclass
class NotificationRequested(Event):
    """2026-08-30 v1.5.0 PR #A4:通知请求 — UI 决定怎么呈现。

    level = "info" | "warning" | "error"
    click_action = None | "show_main" — 单击通知时 main_window 是否弹到顶。
    """

    level: str = "info"
    title: str = ""
    body: str = ""
    click_action: str | None = None


@dataclass
class QuitRequested(Event):
    """2026-08-30 v1.5.0 PR #A4:用户从 tray menu 选「退出」/「暂停监听」。

    pause=True → 仅暂停 monitor(留作 v1.5.1,目前仅 emit 事件,无订阅)
    pause=False → 真退出,UI 走 shutdown_then_quit 路径
    """

    pause: bool = False


@dataclass
class SettingsChanged(Event):
    """设置已变更(已热重载的部分)。"""

    what: str = ""  # "storage" | "objectstore" | "credentials"
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
    progress: int = 0  # 已处理消息数
    total: int | None = None  # 总数(可空,history 全量无终点)
    detail: str = ""  # 退避秒数 / 错误消息等


@dataclass
class ChannelSyncDone(Event):
    """全量同步整轮结束 — UI 进度对话框据此自动关闭。"""

    result: object = None  # SyncResult(避免循环 import,用 object 占位)


@dataclass
class ChannelMetadataChanged(Event):
    """2026-08-27 v1.4.0 PR #14:TDLib `updateChannel` / `updateSupergroup`
    → 频道元数据变更(title / username / member_count)。

    部分字段语义:
    - 任一字段为 None 表示本 update 没动该字段(只更新其它字段)
    - `supergroup_id`:TG 内部 supergroup id(对应 TDLib `updateSupergroup`),
      MonitorService 据此查 `channels.username` 拿到 channel_id
    """

    channel_id: int = 0
    title: str | None = None
    username: str | None = None
    member_count: int | None = None
    # 2026-08-27 PR #14:`updateSupergroup` 推送时只有 supergroup_id,
    # channel_id 由 monitor 通过 username 关联得到。
    supergroup_id: int | None = None


# ---------- Bus ----------

Subscriber = Callable[[Any], Awaitable[None]]


class EventBus:
    """异步事件总线 — 单进程 in-memory,无第三方依赖。"""

    def __init__(self) -> None:
        """空订阅表;按事件类型 + 通配订阅。"""
        self._subs: dict[type[Event], list[Subscriber]] = {}
        self._wild: list[Subscriber] = []  # 订阅所有事件

    def subscribe(self, event_type: type[T], fn: Subscriber) -> None:
        """订阅指定事件类型;订阅者抛异常被吞 + 日志,不互相影响。"""
        self._subs.setdefault(event_type, []).append(fn)

    def subscribe_all(self, fn: Subscriber) -> None:
        """通配订阅(收所有事件类型)。"""
        self._wild.append(fn)

    def unsubscribe(self, event_type: type[Event], fn: Subscriber) -> None:
        """退订;不存在 idempotent 不抛。"""
        if event_type in self._subs:
            try:
                self._subs[event_type].remove(fn)
            except ValueError:
                pass

    async def publish(self, event: Event) -> None:
        """广播一个事件:按类型 + MRO 父类匹配订阅者 + 通知所有 wildcard 订阅者。

        订阅者抛异常被吞 + 日志,不互相影响。

        # 基类匹配
        """
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
