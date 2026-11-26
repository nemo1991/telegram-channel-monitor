"""插件 API — 宿主交给插件的唯一窄门面。

`PluginContext` 是 frozen dataclass,只暴露:
- `bus`:`EventBusView` — 订阅/发布事件,且**记账订阅**以便 deactivate 时统一退订
  (`EventBus.subscribe_all` 无对应 unsubscribe_all,宿主必须自己记,故对插件不开放
  subscribe_all);
- `api`:`PluginApi` — 有界写操作 + 只读查询,委托既有域服务;
- `config`:`PluginConfig` — 按插件 id 隔离的 KV;
- `settings`:`SettingsView` — 只读配置快照;
- `notify()` — 转 `NotificationRequested` 事件,复用托盘/状态栏既有订阅者。

**绝不交出** AppService 本体、Telethon client、Qt 对象。所有载荷是 DTO。
`PluginApi` 的方法集合由 `tests/test_plugin_api_surface.py` allowlist 锁死,
新增方法必须显式改测试并说明理由,防止门面腐烂成第二个 god facade。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol

from tgmonitor.core.dto import ChannelDTO, MediaDTO, MessageDTO
from tgmonitor.core.events import Event, EventBus, NotificationRequested
from tgmonitor.plugins.manifest import PluginManifest

Subscriber = Any  # 与 core.events.Subscriber 对齐,避免在此重复 Callable 别名


class EventBusView:
    """EventBus 的插件视图 — 只开放可退订的按类型订阅 + 发布。

    记账订阅(deactivate 时统一 `unsubscribe`),刻意不暴露 `subscribe_all`
    (core 的 `EventBus` 也没有 `unsubscribe_all`,开放即无法干净退订)。
    """

    def __init__(self, bus: EventBus, owner: str = "plugin") -> None:
        self._bus = bus
        self._owner = owner
        self._subs: list[tuple[type[Event], Subscriber]] = []

    def subscribe(self, event_type: type[Event], fn: Subscriber) -> None:
        """订阅一个领域事件;订阅者必须是 async callable(与 core 约定一致)。"""
        self._bus.subscribe(event_type, fn)
        self._subs.append((event_type, fn))

    def publish(self, event: Event) -> None:
        """fire-and-forget 发布 — 转 core 的 `publish_async`,不阻塞插件。"""
        self._bus.publish_async(event)

    async def publish_and_wait(self, event: Event) -> None:
        """需要等待订阅者完成的场合(测试 / 有序后处理)用这个。"""
        await self._bus.publish(event)

    def notify(self, level: str, title: str, body: str) -> None:
        """转 `NotificationRequested` — 托盘 / 状态栏既有订阅者负责呈现。"""
        self._bus.publish_async(NotificationRequested(level=level, title=title, body=body))

    def unsubscribe_all(self) -> None:
        """退订本视图登记过的所有订阅;幂等。宿主在 deactivate 时调用。"""
        for event_type, fn in self._subs:
            self._bus.unsubscribe(event_type, fn)
        self._subs.clear()

    def subscription_count(self) -> int:
        """已登记订阅数 — 守护测试 / 诊断用。"""
        return len(self._subs)


class _DelegatingPluginApi:
    """`PluginApi` 的生产实现 — 委托给 runtime 传入的域服务映射。

    不是公开 API(下划线开头),allowlist 守护只看 `PluginApi` Protocol 的
    方法集合。域服务由 runtime 显式传入,host 不依赖 AppService 本体。
    """

    def __init__(self, services: Mapping[str, Any]) -> None:
        self._messages = services["messages"]
        self._media = services["media"]
        self._channels = services["channels"]

    async def list_messages(
        self,
        channel_ids: list[int] | None = None,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int | None = 200,
        search: str = "",
    ) -> list[MessageDTO]:
        return await self._messages.list_messages(
            channel_ids, date_from=date_from, date_to=date_to, limit=limit, search=search
        )

    async def list_media(
        self,
        *,
        channel_id: int | None = None,
        limit: int = 1000,
    ) -> tuple[list[tuple[MessageDTO, int, MediaDTO]], int]:
        return await self._media.list_media(channel_id=channel_id, limit=limit)

    async def list_monitored_channels(self) -> list[ChannelDTO]:
        return await self._channels.list_monitored_channels()

    async def set_favorite(self, cid: int, mid: int, value: bool) -> None:
        await self._messages.set_favorite(cid, mid, value)

    async def set_tags(self, cid: int, mid: int, tags: list[str]) -> None:
        await self._messages.set_tags(cid, mid, tags)

    async def set_notes(self, cid: int, mid: int, notes: str) -> None:
        await self._messages.set_notes(cid, mid, notes)

    async def delete_message(self, cid: int, mid: int) -> None:
        await self._messages.delete_message(cid, mid)

    async def retry_media(self, cid: int, mid: int, idx: int) -> None:
        await self._media.retry_media(cid, mid, idx)

    async def download_media(self, cid: int, mid: int, idx: int) -> None:
        await self._media.download_media(cid, mid, idx)


class PluginApi(Protocol):
    """插件可调用的域操作 — 有界集合,委托既有域服务。

    读方法返回 DTO;写方法复用域服务原有的事件发布与幂等语义,
    不另开数据通路。方法集合被 `test_plugin_api_surface.py` 锁死。
    """

    async def list_messages(
        self,
        channel_ids: list[int] | None = None,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int | None = 200,
        search: str = "",
    ) -> list[MessageDTO]: ...

    async def list_media(
        self,
        *,
        channel_id: int | None = None,
        limit: int = 1000,
    ) -> tuple[list[tuple[MessageDTO, int, MediaDTO]], int]: ...

    async def list_monitored_channels(self) -> list[ChannelDTO]: ...

    async def set_favorite(self, cid: int, mid: int, value: bool) -> None: ...

    async def set_tags(self, cid: int, mid: int, tags: list[str]) -> None: ...

    async def set_notes(self, cid: int, mid: int, notes: str) -> None: ...

    async def delete_message(self, cid: int, mid: int) -> None: ...

    async def retry_media(self, cid: int, mid: int, idx: int) -> None: ...

    async def download_media(self, cid: int, mid: int, idx: int) -> None: ...


@dataclass(frozen=True)
class SettingsView:
    """只读配置快照 — 故意只暴露少量无害字段,不整包交出 Settings。"""

    data_root: Path
    media_policy: str
    db_backend: str
    objectstore_backend: str

    @classmethod
    def from_settings(cls, settings: Any) -> SettingsView:
        """从 Settings 抽只读快照;枚举转 value 字符串。"""
        return cls(
            data_root=Path(settings.data_root),
            media_policy=_enum_value(settings.media_policy),
            db_backend=_enum_value(settings.db_backend),
            objectstore_backend=_enum_value(settings.objectstore_backend),
        )


def _enum_value(value: Any) -> str:
    """Enum → value 字符串;普通值转 str。"""
    return str(getattr(value, "value", value))


@dataclass(frozen=True)
class PluginContext:
    """宿主交给插件的完整上下文 — 唯一可访问的宿主能力面。

    `activate()` 收到本对象后不应长期持有裸 `EventBus` / `AppService`;
    订阅走 `bus`,域操作走 `api`,自有配置走 `config`。
    """

    bus: EventBusView
    api: PluginApi
    config: Any  # PluginConfig — 避免与 config 模块同名导致的循环 import
    settings: SettingsView
    manifest: PluginManifest

    def notify(self, level: str, title: str, body: str) -> None:
        """转 `NotificationRequested` — 便捷入口,等价于 `bus.notify`。"""
        self.bus.notify(level, title, body)

    def deactivate_hooks(self) -> None:
        """宿主停用插件时调用 — 目前只做订阅退订;未来扩展钩子集中在此。"""
        self.bus.unsubscribe_all()


class Plugin(Protocol):
    """插件实现契约 — 第三方包提供,宿主通过本协议调用。

    约定:`manifest` 是类属性(发现阶段无需实例化即可读);
    `activate` / `deactivate` 同步(插件内部自己起后台任务,host 只管生命周期钩子)。
    """

    manifest: PluginManifest

    def activate(self, ctx: PluginContext) -> None: ...

    def deactivate(self) -> None: ...
