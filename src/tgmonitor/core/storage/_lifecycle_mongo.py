"""MongoDB 生命周期子模块 — 2026-10-16 plan-batch8-1.1。

持 6 个 lifecycle 异步方法(`connect` / `close` / `init_schema` /
`introspect_schema` / `repair_schema` / `ping`)与
`from_client_factory(client, database)` 工厂函数。

落地模式:free function + `self` 透传(与 plan-batch6 media_service
1:1 对齐)。`MongoRepository` facade 持 `self._client` / `self._db`,
本 module 每个 method 走 `self.db` / `self._client` 反射 facade 句柄。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from motor.motor_asyncio import AsyncIOMotorClient

from tgmonitor.core.storage.schema_report import SchemaReport

if TYPE_CHECKING:
    from tgmonitor.core.storage.mongo_repo import MongoRepository

log = logging.getLogger(__name__)


def _index_key_fields(key: object) -> set[str]:
    """mongomock_motor 把 `key` 返成 `[(field, dir), ...]` 列表,真 motor
    用 SON / OrderedDict。统一抽字段名集合。
    """
    if isinstance(key, dict):
        return {str(k) for k in key}
    if isinstance(key, (list, tuple)):
        return {str(item[0]) for item in key if item}
    return set()


def from_client_factory(
    client: AsyncIOMotorClient,
    database: str,
) -> MongoRepository:
    """2026-08-31 v1.5.0 PR #A7:用预构造 client 建实例(测试用)— 2026-10-16 plan-batch8-1.1 落本子模块。

    生产代码仍走 `__init__(dsn, database)`,走 `connect()` 自建 motor client。
    测试(mongomock_motor / 真 testcontainers mongodb)可传已构造好的对象,
    跳过 motor 的 DSN 解析。

    `from_client` 构造的实例:`close()` 不会关 client(测试可能多个 repo
    共享同一 client;client 生命周期由 fixture 管理)— 用空 `dsn` 作
    sentinel,`close()` 据此跳过。
    """
    from tgmonitor.core.storage.mongo_repo import MongoRepository

    instance = MongoRepository.__new__(MongoRepository)
    instance._dsn = ""  # sentinel:from_client 路径
    instance._db_name = database
    instance._client = client
    instance._db = client[database]
    return instance


# ---- 6 lifecycle method ----


async def connect(self: MongoRepository) -> None:
    """建 motor 客户端 + 拿 db 句柄(不立即 ping)。"""
    if self._client is None:
        self._client = AsyncIOMotorClient(self._dsn)
        self._db = self._client[self._db_name]


async def close(self: MongoRepository) -> None:
    """关 motor 客户端;幂等。`from_client` 构造的实例不会关 client。"""
    if self._dsn and self._client is not None:
        self._client.close()
    self._client = None
    self._db = None


async def init_schema(self: MongoRepository) -> None:
    """建索引:(channel_id, telegram_msg_id) 唯一 / (channel_id, date) /
    (date) / media.message_id / media.telegram_file_id / media.file_name
    (case-insensitive collation) — 幂等(create_index 同名 no-op)。

    2026-08-31 v1.5.0 PR #A7:加 `media.telegram_file_id` 子文档索引 —
    `find_media_by_file_id` 修后走 `$unwind + $match` 路径,需此索引
    走 multikey 命中避免全表扫。旧 `db.media.telegram_file_id` 索引
    保留向后兼容(空集合不影响 query planner)。

    2026-09-03 v1.6.0 PR #Q1:加 `media.file_name` case-insensitive
    collation 索引 — list_messages(search=...) 命中 `$or` 走
    `media.file_name regex` 路径,collation 索引让大小写不敏感匹配
    走 IXSCAN 而非全表扫。mongomock_motor collation 部分支持,集成测试
    见 `test_mongo_repo_parity.py`。
    """
    # 唯一索引
    await self.db.messages.create_index([("channel_id", 1), ("telegram_msg_id", 1)], unique=True)
    await self.db.messages.create_index([("channel_id", 1), ("date", 1)])
    await self.db.messages.create_index([("date", 1)])
    # 子文档 multikey 索引 — find_media_by_file_id 用(2026-08-31 PR #A7)
    await self.db.messages.create_index([("media.telegram_file_id", 1)])
    # 2026-09-03 v1.6.0 PR #Q1:file_name collation 索引 — strength=2
    # 表示大小写不敏感、重音敏感,覆盖「搜索大小写不敏感」的 list_messages
    # 行为。collation 名 `en_US` 跟随业务默认(locale 影响排序 / 索引,
    # 与既有 `db.messages` 主索引一致)。
    try:
        await self.db.messages.create_index(
            [("media.file_name", 1)],
            name="idx_messages_media_file_name_ci",
            collation={"locale": "en", "strength": 2},
        )
    except Exception:
        # mongomock_motor 部分版本不支持 collation 参数;集成测试 mongomock
        # 路径可能 IndexOptionsConflict。吞掉,功能仍可用(只是扫表)。
        log.warning(
            "create_index(file_name collation) failed, fallback to table scan",
            exc_info=True,
        )
    # 旧 db.media 索引保留(空集合,无害)
    await self.db.media.create_index([("message_id", 1)])
    await self.db.media.create_index([("telegram_file_id", 1)])
    # 2026-09-29:缩略图独立 collection。`_id` 是复合 string 主键
    # (自带 unique),这里只加 `object_key` 索引给 orphan reconcile 用
    # (不强制 unique — 跨消息同 thumb bytes 复用是合法状态)。
    await self.db.thumbnails.create_index([("object_key", 1)])


async def introspect_schema(self: MongoRepository) -> SchemaReport:
    """2026-09-23 v1.8.x:Mongo schema-less,只检查期望索引是否齐全。

    缺失唯一索引 `(channel_id, telegram_msg_id)` 时记入
    `missing_columns`,运维提示(可能是 mongomock 跑出来的无索引集合)。
    """
    assert self._db is not None
    indexes = await self._db.messages.index_information()

    have_unique = any(
        bool(idx.get("unique"))
        and _index_key_fields(idx["key"]) == {"channel_id", "telegram_msg_id"}
        for idx in indexes.values()
    )
    if not have_unique:
        return SchemaReport(
            missing_columns=[("messages", "(channel_id, telegram_msg_id)", "UNIQUE INDEX")]
        )
    return SchemaReport()


async def repair_schema(self: MongoRepository, report: SchemaReport) -> None:
    """2026-09-23 v1.8.x:若 report 指明唯一索引缺失,重建。

    `create_index(..., unique=True)` 在已有重复 `(channel_id, telegram_msg_id)`
    数据上会抛 `DuplicateKeyError` — wrap 在 try/except,失败 log warning
    不阻塞启动(运维需要清理数据后手动重建)。
    """
    if not report.missing_columns:
        return
    if self._db is None:
        return
    try:
        await self._db.messages.create_index(
            [("channel_id", 1), ("telegram_msg_id", 1)],
            unique=True,
            name="uq_channel_msg",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("mongo repair: recreate unique index failed — %s", exc)


async def ping(self: MongoRepository) -> bool:
    """`db.command("ping")` 探活;任何异常返 False。"""
    try:
        await self.db.command("ping")
        return True
    except Exception:
        return False
