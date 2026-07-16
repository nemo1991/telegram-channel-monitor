"""StorageRepository — 消息数据持久化抽象。

两套实现(Postgres / Mongo)必须提供**等价**的查询语义,
通过 `StorageRepository` 这一接口对上层透明。

设计原则:
- 接口全部 `async`,core 异步到底。
- 接收 / 返回 DTO,不暴露 ORM 行对象。
- `save_message` 幂等(以 `(channel_id, telegram_msg_id)` 为唯一键)。
- `delete_message` 支持消息撤回;`update_message` 支持编辑。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from tgmonitor.core.dto import ChannelDTO, MediaDTO, MessageDTO


class StorageRepository(ABC):
    """消息仓储抽象。"""

    # ---- 生命周期 ----

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def init_schema(self) -> None:
        """创建表/集合 + 索引(Mongo 用 ensureIndex)。幂等。"""
        ...

    # ---- 频道 ----

    @abstractmethod
    async def upsert_channel(self, channel: ChannelDTO) -> None:
        """全 upsert — 写所有字段(包括 subscribed)。
        兼容旧调用方。**新代码用 upsert_channel_metadata + set_channel_subscribed**
        以避免 sync 误改订阅标志。
        """
        ...

    @abstractmethod
    async def upsert_channel_metadata(self, channel: ChannelDTO) -> None:
        """只写元数据字段(title/username/kind/member_count/created_at/last_synced_at),
        不碰 is_subscribed 标志。ChannelSyncService 用。
        """
        ...

    @abstractmethod
    async def set_channel_subscribed(
        self, channel_id: int, subscribed: bool
    ) -> None:
        """只设订阅标志,不动其它字段。"""
        ...

    @abstractmethod
    async def list_channels(self) -> list[ChannelDTO]: ...

    @abstractmethod
    async def list_subscribed_channels(self) -> list[ChannelDTO]:
        """只返回 is_subscribed=True 的频道。"""
        ...

    @abstractmethod
    async def get_channel(self, channel_id: int) -> ChannelDTO | None: ...

    @abstractmethod
    async def delete_channel(self, channel_id: int) -> None:
        """删除频道及其所有消息与媒体引用(不删对象存储里的二进制)。

        注:用户退订不应调这个 — 退订走 `set_channel_subscribed(id, False)`,
        保留元数据 + 历史消息,只是不再喂给 monitor。
        """
        ...

    @abstractmethod
    async def get_max_telegram_msg_id(self, channel_id: int) -> int | None:
        """续拉历史用 — 返回该频道已落库的最大 `telegram_msg_id`;None 表示无历史。"""
        ...

    # ---- 全局元数据(meta 表 / 单独文件) ----

    @abstractmethod
    async def get_meta(self, key: str) -> str | None:
        """全局单值元数据(同步 checkpoint 等)。"""
        ...

    @abstractmethod
    async def set_meta(self, key: str, value: str) -> None:
        """upsert 语义。"""
        ...

    # ---- 消息 ----

    @abstractmethod
    async def save_message(self, message: MessageDTO) -> int:
        """幂等 upsert。返回 DB 内部 id。"""
        ...

    @abstractmethod
    async def update_message(self, message: MessageDTO) -> None: ...

    @abstractmethod
    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None: ...

    @abstractmethod
    async def get_message(
        self, channel_id: int, telegram_msg_id: int
    ) -> MessageDTO | None: ...

    @abstractmethod
    async def list_messages(
        self,
        channel_ids: list[int],
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int | None = None,
    ) -> list[MessageDTO]:
        """按时间升序返回。两实现必须排序一致。"""
        ...

    @abstractmethod
    async def count_messages(self, channel_id: int) -> int: ...

    # ---- 健康检查 ----

    @abstractmethod
    async def ping(self) -> bool:
        """轻量探活。"""
        ...


# MediaDTO 在此包内被引用,显式 re-export 避免循环
__all__ = ["StorageRepository", "ChannelDTO", "MessageDTO", "MediaDTO"]
