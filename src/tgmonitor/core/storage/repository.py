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
from typing import Any

from tgmonitor.core.dto import (
    ChannelDTO,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    SortDir,
    SortKey,
)


class StorageRepository(ABC):
    """消息仓储抽象。"""

    # ---- 生命周期 ----

    @abstractmethod
    async def connect(self) -> None:
        """建连接 / 加载 schema;幂等。"""
        ...

    @abstractmethod
    async def init_schema(self) -> None:
        """建表 / 建索引 — 幂等;启动时跑一次,跟 connect 解耦以便测试 mock。"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """刷缓存 / 关连接 / 释放资源。"""
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
    async def list_channels(self) -> list[ChannelDTO]:
        """所有频道(包含未订阅的);顺序无要求。"""
        ...

    @abstractmethod
    async def list_subscribed_channels(self) -> list[ChannelDTO]:
        """只返回 is_subscribed=True 的频道。"""
        ...

    @abstractmethod
    async def get_channel(self, channel_id: int) -> ChannelDTO | None:
        """不存在返 None(不抛)。"""
        ...

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
    async def update_message(self, message: MessageDTO) -> None:
        """按 (channel_id, telegram_msg_id) 覆盖式更新;不存在等效 save。"""
        ...

    @abstractmethod
    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        """删单条消息;不存在不抛(idempotent)。"""
        ...

    @abstractmethod
    async def get_message(
        self, channel_id: int, telegram_msg_id: int
    ) -> MessageDTO | None:
        """单条消息;不存在返 None。"""
        ...

    @abstractmethod
    async def list_messages(
        self,
        channel_ids: list[int],
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[MessageDTO]:
        """按时间升序返回。两实现必须排序一致。

        `limit`:只返回**最近** N 条(取排序尾部,仍按时间升序)。
        `offset`:跳过前 N 条(2026-08-27 v1.4.0 PR #12 — export 真分页)。
        `offset` 与 `limit` 组合:跳过 offset 后取 limit 条(从尾取仍升序)。
        注意:`offset` 是相对排序后的位置,与 channel_ids 顺序无关。
        """
        ...

    @abstractmethod
    async def count_messages(self, channel_id: int) -> int:
        """该频道已落库消息数(忽略 date_from/to)。"""
        ...

    @abstractmethod
    async def find_media_by_file_id(
        self, telegram_file_id: str
    ) -> MediaDTO | None:
        """查任意 prior MediaDTO 同 `telegram_file_id`(任意 channel)且下载已完成。

        用于跨消息媒体去重:同一 file_id 在 channel A 已下载成功 → channel B 再
        出现时直接复用 `object_key` / `object_backend` / `file_size`,不发起 TDLib
        `GetFile` 请求。

        命中条件:`download_status == DONE` 且 `object_key` 非 None。命中时返回的
        DTO 至少含上述三字段;`download_status != DONE` 视为未下,返 None。
        不抛,找不到返 None。
        """
        ...

    @abstractmethod
    async def list_media(
        self,
        *,
        channel_ids: list[int] | None = None,
        status: MediaDownloadStatus | None = None,
        media_type: MediaType | None = None,
        search: str = "",
        limit: int = 1000,
        offset: int = 0,
        sort: SortKey = SortKey.DATE,
        sort_dir: SortDir = SortDir.DESC,
    ) -> list[tuple[MessageDTO, int, MediaDTO]]:
        """列媒体(2026-08-25 PR #3)— 应用层扁平 + 后端下沉到此处。

        返回 `(msg, idx, media)` 三元组 list;`idx` 是 media 在 message.media 数组
        中的位置(0-based)。所有 filter 都是 AND 关系,空值视作不过滤。

        排序(2026-08-25 v1.3.0 PR #6 新增):`sort` 选 3 key(`DATE`/`SIZE`/
        `STATUS`),`sort_dir` 选 `ASC`/`DESC`。默认 `DATE DESC`(v1.2.0 行为)。
        tie-breaker:`m.id DESC, me.media_idx ASC` 保证结果稳定。

        分页:`offset` 跳过 N 条;`limit` 限制返数。MVP 阶段各后端实现可简化
        (InMemory / Jsonl 顺序扫 + slice;Postgres / Mongo 用 SQL/aggregate 偏移)。
        """
        ...

    @abstractmethod
    async def count_media(
        self,
        *,
        channel_ids: list[int] | None = None,
        status: MediaDownloadStatus | None = None,
        media_type: MediaType | None = None,
        search: str = "",
    ) -> int:
        """数媒体(2026-08-25 v1.3.0 PR #6)— 与 list_media 用同一组 filter,
        但不带 sort/limit/offset。供分页 UI 显示 "total"。

        各后端实现要点:
        - InMemory / Jsonl:复用 list_media 的 filter helper,只数不取
        - Postgres:同样 JOIN,去掉 ORDER/LIMIT/OFFSET,换 `SELECT count(*)`
        - Mongo:独立 aggregate,同 match 条件但无 `$sort`/`$skip`/`$limit`,
          末尾 `$count`
        """
        ...

    @abstractmethod
    async def count_media_by_object_key(self, object_key: str) -> int:
        """同 `object_key` 引用次数(2026-08-25 PR #3)— refcount 用。

        应用层 `_count_media_with_object_key` 在此 PR 删除,改调本方法。
        Postres/Mongo 用 SQL/aggregate count,O(1);InMemory/Jsonl 走顺序扫
        (与原来应用层实现一致)。
        """
        ...

    @abstractmethod
    async def count_media_by_channel(self, channel_id: int) -> int:
        """该频道全部媒体数(2026-08-25 v1.3.0 PR #8)— 含 PENDING/FAILED。

        用途:`ClearChannelPreview.media_count`。比 `list_media(
        channel_ids=[id]) + len()` 更便宜,4 后端各自走 SQL/aggregate count
        而非把 row 拉回内存。
        """
        ...

    # ---- 互动增量(2026-08-27 v1.4.0 PR #10) ----

    @abstractmethod
    async def update_message_interactions(
        self,
        channel_id: int,
        telegram_msg_id: int,
        *,
        views: int | None = None,
        reactions: list[Any] | None = None,
    ) -> None:
        """增量更新一条消息的 views / reactions(2026-08-27 PR #10)。

        TDLib `updateMessageInteractionInfo` 推送的字段可能只更新一部分
        (比如只有 reactions 没新 view) — `views=None` 表示不动 views,
        `reactions=None` 表示不动 reactions。`reactions=[]` 表示清空
        (推了空 list 表示该消息所有 reaction 都被撤回)。

        不存在消息 idempotent 不抛 — TDLib 偶尔会推陈年历史消息的 view
        更新,落库时机早于本 update,跳过即可,UI 不感知。

        `reactions` 元素类型:可以是 `ReactionDTO`(新代码)或 dict(兼容老
        序列化格式);实现内部统一 `ReactionDTO.from_dict()` 处理。
        """
        ...

    # ---- 健康检查 ----

    @abstractmethod
    async def ping(self) -> bool:
        """轻量探活。"""
        ...


# MediaDTO 在此包内被引用,显式 re-export 避免循环
__all__ = ["StorageRepository", "ChannelDTO", "MessageDTO", "MediaDTO"]
