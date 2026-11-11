"""存储抽象 — 按域切分的小接口 + 组合门面。

v2.0.0 重构:原 42 方法单一 `StorageRepository` ABC 按域拆成 7 个小接口
(Interface Segregation)— 领域服务按需依赖小接口,不再被迫面对全量方法。
`StorageRepository` 保留为 7 接口的组合,现有装配/调用方不变。

设计原则(沿用):
- 接口全部 `async`,core 异步到底。
- 接收 / 返回 DTO,不暴露 ORM 行对象。
- `save_message` 幂等(以 `(channel_id, telegram_msg_id)` 为唯一键)。
- 各后端(SQLite / Postgres)必须提供**等价**的查询语义。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from tgmonitor.core.dto import (
    ChannelDTO,
    ChannelStats,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    MessageDTO,
    SortDir,
    SortKey,
    ThumbnailDTO,
)
from tgmonitor.core.storage.schema_report import SchemaReport


class SchemaLifecycle(ABC):
    """连接生命周期 + schema 自检/修复。"""

    @abstractmethod
    async def connect(self) -> None:
        """建连接 / 加载 schema;幂等。"""
        ...

    @abstractmethod
    async def init_schema(self) -> None:
        """建表 / 建索引 — 幂等;启动时跑一次,跟 connect 解耦以便测试 mock。"""
        ...

    @abstractmethod
    async def introspect_schema(self) -> SchemaReport:
        """启动期 schema 自检 — 比对实际 schema 与期望定义,返回 SchemaReport
        列出 drift(missing tables / columns / wrong_types / extra columns)。

        各 backend 实现:
        - SQLite / Postgres:查系统表(`sqlite_master` / `information_schema`)
          比对表 + 列类型
        """
        ...

    @abstractmethod
    async def repair_schema(self, report: SchemaReport) -> None:
        """基于 SchemaReport 跑幂等修复。

        策略(保守):
        - missing_columns → `ALTER TABLE ADD COLUMN`(幂等,无锁)
        - missing_tables → 拒绝(避免 DROP+CREATE 丢数据),让运维手动跑 init_schema
        - wrong_types → 仅 log,不动(类型 ALTER 重写表对大表 lock-heavy)
        - extra_columns → 仅 log,不删

        必须幂等:连续跑两次不抛,二次 introspect 后仍 ok=True。
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """刷缓存 / 关连接 / 释放资源。"""
        ...

    @abstractmethod
    async def ping(self) -> bool:
        """轻量探活。"""
        ...


class ChannelStore(ABC):
    """频道元数据 + 订阅标志。"""

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
    async def update_channel_metadata(
        self,
        channel_id: int,
        *,
        title: str | None = None,
        username: str | None = None,
        member_count: int | None = None,
        photo_local_key: str | None = None,
        is_verified: bool | None = None,
        is_scam: bool | None = None,
        is_fake: bool | None = None,
        has_protected_content: bool | None = None,
    ) -> None:
        """部分更新频道元数据。

        TDLib `updateChannel` / `updateSupergroup` / `updateChatTitle` /
        `updateChatPhoto` 推送可能只动 1 个字段(如 member_count 改、title
        不变);`title=None` 表示不动 title。`photo_local_key=None` 表示
        不动(不是「删头像」 — 删头像语义需走显式参数)。

        与 `upsert_channel_metadata` 不同:本方法**只**动传入字段(其它字段
        保留旧值);`upsert_channel_metadata` 是覆盖式写入全字段。

        不存在频道 idempotent 不抛(TDLib 偶发对陈年 channel 推 metadata
        update,落库时机晚于本 update 时跳过即可)。

        4 个 spammer 过滤字段(`is_verified` / `is_scam` / `is_fake` /
        `has_protected_content`)partial update 语义同其他字段(`None` = 不动)。
        """
        ...

    @abstractmethod
    async def set_channel_subscribed(self, channel_id: int, subscribed: bool) -> None:
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


class MetaStore(ABC):
    """全局单值元数据(同步 checkpoint 等)。"""

    @abstractmethod
    async def get_meta(self, key: str) -> str | None:
        """全局单值元数据;不存在返 None。"""
        ...

    @abstractmethod
    async def set_meta(self, key: str, value: str) -> None:
        """upsert 语义。"""
        ...


class MessageStore(ABC):
    """消息 CRUD + 查询 + 聚合 + 互动增量。"""

    @abstractmethod
    async def save_message(self, message: MessageDTO) -> int:
        """幂等 upsert。返回 DB 内部 id。"""
        ...

    @abstractmethod
    async def update_message(self, message: MessageDTO) -> None:
        """按 (channel_id, telegram_msg_id) 覆盖式更新;不存在等效 save。"""
        ...

    @abstractmethod
    async def update_message_interactions(
        self,
        channel_id: int,
        telegram_msg_id: int,
        *,
        views: int | None = None,
        reactions: list[Any] | None = None,
    ) -> None:
        """增量更新一条消息的 views / reactions。

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

    @abstractmethod
    async def update_message_pin(
        self,
        channel_id: int,
        telegram_msg_id: int,
        is_pinned: bool,
    ) -> None:
        """更新一条消息的 pin 状态。

        TDLib `updateMessageIsPinned` 推送触发;MonitorService 订阅后调
        本方法落库,再 re-fetch DTO + publish `MessageEdited` 让 UI 行 📌
        图标刷新。不存在消息 idempotent 不抛(与 `update_message_interactions`
        一致 — 落库时机早于本 update 的旧历史消息)。
        """
        ...

    @abstractmethod
    async def delete_message(self, channel_id: int, telegram_msg_id: int) -> None:
        """删单条消息;不存在不抛(idempotent)。"""
        ...

    @abstractmethod
    async def delete_messages(self, channel_id: int, msg_ids: list[int]) -> None:
        """批量删单频道 N 条消息。

        不存在不抛(idempotent)。子 media 通过各后端固有机制清理
        (SQL FK CASCADE / InMemory 内联)。
        """
        ...

    @abstractmethod
    async def get_message(self, channel_id: int, telegram_msg_id: int) -> MessageDTO | None:
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
        search: str = "",
        favorite_only: bool = False,
        tag_only: bool = False,
        pinned_only: bool = False,
    ) -> list[MessageDTO]:
        """按时间升序返回。两实现必须排序一致。

        `limit`:只返回**最近** N 条(取排序尾部,仍按时间升序)。
        `offset`:跳过前 N 条(export 真分页)。
        `offset` 与 `limit` 组合:跳过 offset 后取 limit 条(从尾取仍升序)。
        注意:`offset` 是相对排序后的位置,与 channel_ids 顺序无关。

        `search`:子串过滤(大小写不敏感),匹配 `text` 或 `media.file_name`
        任一字段即命中。默认 `""` 不过滤,向后兼容所有现有调用方;
        SQL LIKE 通配符必须 escape 防止注入。

        `favorite_only` / `tag_only` / `pinned_only`:3 个用户元数据过滤
        (对应 SearchBar ★/🏷/📌 3 toggle)。默认 False 不过滤;True 时仅返回
        满足条件的消息 — UI AND 语义:任一 True 字段必须命中。
        `tag_only=True` 表示「有任意 tag 即可」。
        """
        ...

    @abstractmethod
    async def count_messages(self, channel_id: int) -> int:
        """该频道已落库消息数(忽略 date_from/to)。"""
        ...

    @abstractmethod
    async def aggregate_per_channel(self, channel_ids: list[int]) -> dict[int, ChannelStats]:
        """一次拉多频道聚合统计(messages / media / done_media / last_date),
        消除 Dashboard N+1 round-trip。

        实现要点:
        - SQLite / Postgres:`GROUP BY channel_id` 单 SELECT + JOIN media
        - InMemory:扫订阅 channel 一次,聚合 4 个 count
        - 缺失 channel_id 在返 dict 里**不**存在(调用方按需 default zero)

        大 channel_ids 列表(>1000)性能:SQL `IN` 是 O(N) scan;
        MVP 不优化,docstring 注明。
        """
        ...


class MediaStore(ABC):
    """媒体引用查询(二进制在 ObjectStore)。"""

    @abstractmethod
    async def find_media_by_file_id(self, telegram_file_id: str) -> MediaDTO | None:
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
        """列媒体 — 应用层扁平 + 后端下沉到此处。

        返回 `(msg, idx, media)` 三元组 list;`idx` 是 media 在 message.media 数组
        中的位置(0-based)。所有 filter 都是 AND 关系,空值视作不过滤。

        排序:`sort` 选 3 key(`DATE`/`SIZE`/`STATUS`),`sort_dir` 选 `ASC`/`DESC`。
        默认 `DATE DESC`。tie-breaker:`m.id DESC, me.media_idx ASC` 保证结果稳定。

        分页:`offset` 跳过 N 条;`limit` 限制返数。MVP 阶段各后端实现可简化
        (InMemory 顺序扫 + slice;SQL 用 LIMIT/OFFSET)。
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
        """数媒体 — 与 list_media 用同一组 filter,但不带 sort/limit/offset。
        供分页 UI 显示 "total"。

        各后端实现要点:
        - InMemory:复用 list_media 的 filter helper,只数不取
        - SQL:同样 JOIN,去掉 ORDER/LIMIT/OFFSET,换 `SELECT count(*)`
        """
        ...

    @abstractmethod
    async def count_media_by_object_key(self, object_key: str) -> int:
        """同 `object_key` 引用次数 — refcount 用。

        SQL 走 count;InMemory 走顺序扫。
        """
        ...

    @abstractmethod
    async def count_media_by_channel(self, channel_id: int) -> int:
        """该频道全部媒体数 — 含 PENDING/FAILED。

        用途:`ClearChannelPreview.media_count`。比 `list_media(
        channel_ids=[id]) + len()` 更便宜,各后端走 SQL count
        而非把 row 拉回内存。
        """
        ...


class ThumbnailStore(ABC):
    """缩略图独立存储。

    关联键 `(channel_id, telegram_msg_id, media_idx)` 三元组 — 与父
    `MessageDTO.media[media_idx]` 1:1。各后端各自存:SQL `thumbnails`
    表,InMemory 内嵌 `thumbnails` dict。ObjectStore 端 key 走独立顶层
    prefix `thumb/<sha256>.<ext>`,跟 `media/<sha256>.<ext>` 平行。
    """

    @abstractmethod
    async def save_thumbnail(self, thumb: ThumbnailDTO) -> None:
        """upsert thumb 行(以三 key 为主键)。

        `download_status` / `download_error` / `object_key` 等任意字段更新都
        通过本方法;幂等 — 多次调以最新一次为准。media 行不动。
        """
        ...

    @abstractmethod
    async def get_thumbnail(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> ThumbnailDTO | None:
        """单条 thumb;不存在返 None(不抛)。

        与 `find_media_by_file_id` 对齐:`get_*` 找不到返 None 不抛。
        """
        ...

    @abstractmethod
    async def delete_thumbnail(
        self,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> None:
        """删 thumb 行;不存在不抛(idempotent)。

        调用方:SQL `delete_message` 走 FK CASCADE 自动调(InMemory 没
        FK 概念,在 `delete_message` 内手工调本方法级联)。
        """
        ...

    @abstractmethod
    async def list_thumbnails_for_message(
        self,
        channel_id: int,
        telegram_msg_id: int,
    ) -> list[ThumbnailDTO]:
        """一条消息的全部 thumb — 通常 0 或 1 条。

        HTML / ZIP exporter 用:列消息的所有 thumb,跟 media 循环对齐。空 list
        表示该消息没 thumb(voice_note / 无 thumbnail 的 media type)。
        """
        ...

    @abstractmethod
    async def count_thumbnails_by_object_key(self, object_key: str) -> int:
        """同 `object_key` 引用次数 — `reconcile_orphans` refcount 用。

        应用层:`reconcile_orphans` 把 ObjectStore key 减去 referenced 集合,
        剩下的才是真孤儿(没 thumb 或 storage 引用)。语义跟
        `count_media_by_object_key` 平行。
        """
        ...


class UserMetaStore(ABC):
    """用户元数据(收藏 / 标签 / 备注)— 本地存储,不与 TG 同步。"""

    @abstractmethod
    async def set_favorite(self, channel_id: int, telegram_msg_id: int, value: bool) -> None:
        """单条消息设收藏 — 不存在消息 idempotent no-op(消息撤回后被 set 不报错)。"""
        ...

    @abstractmethod
    async def set_tags(self, channel_id: int, telegram_msg_id: int, tags: list[str]) -> None:
        """单条消息覆盖式设标签列表(空 list 即清空)。idempotent 同上。"""
        ...

    @abstractmethod
    async def set_notes(self, channel_id: int, telegram_msg_id: int, notes: str) -> None:
        """单条消息覆盖式设备注(空串即清空)。idempotent 同上。"""
        ...

    @abstractmethod
    async def list_favorites(self) -> list[MessageDTO]:
        """列所有 `is_favorite=True` 的消息 — 跨频道,按 date DESC。"""
        ...

    @abstractmethod
    async def list_by_tag(self, tag: str) -> list[MessageDTO]:
        """按 `tag` 查消息(标签精确匹配) — 跨频道,按 date DESC。"""
        ...


class StorageRepository(
    SchemaLifecycle,
    ChannelStore,
    MetaStore,
    MessageStore,
    MediaStore,
    ThumbnailStore,
    UserMetaStore,
    ABC,
):
    """组合门面 — 7 个小接口的全集,现有装配/调用方继续用此类型。

    新代码(领域服务)应按需依赖单个小接口,而不是本组合。
    """


# MediaDTO 在此包内被引用,显式 re-export 避免循环
__all__ = [
    "SchemaLifecycle",
    "ChannelStore",
    "MetaStore",
    "MessageStore",
    "MediaStore",
    "ThumbnailStore",
    "UserMetaStore",
    "StorageRepository",
    "ChannelDTO",
    "MessageDTO",
    "MediaDTO",
]
