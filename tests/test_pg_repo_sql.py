"""PostgresRepository SQL 字符串回归测试 — 2026-09-25 fix(media-bug)。

`media` 表没有 `media_idx` 列(2026-08-25 PR #3 把 media 拆成独立表时漏了),
原 `list_media` SQL 写了 `SELECT me.media_idx`,在真 PG 跑报
`UndefinedColumnError`,被 `run_coro` 静默吞掉 → Media Manager 页空表。
修法:把 SELECT 里的 `me.media_idx` 换成窗口函数
`ROW_NUMBER() OVER (PARTITION BY m.id ORDER BY me.id) - 1 AS media_idx`,
与 InMemory `enumerate` / SQLite `ROW_NUMBER` 顺序对齐。

本测试不依赖真 PG(集成测试在 `tests/integration/test_pg_repo_list_media.py`),
mock `_pool` 拦截传给 `conn.fetch(...)` 的 SQL,断言关键关键字 / 禁用符号。

回归若有人误把 SELECT 改回 `me.media_idx`,本测试会立刻 fail。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from tgmonitor.core.dto import MediaDownloadStatus, MediaType
from tgmonitor.core.storage.postgres_repo import PostgresRepository


class _FakeAcquire:
    """asyncpg pool acquire 的最小 mock — 捕获 SQL 字符串 + 返空 rows。"""

    def __init__(self) -> None:
        self.captured_sql: list[str] = []
        self.captured_params: list[tuple[Any, ...]] = []

    async def __aenter__(self) -> _FakeAcquire:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.captured_sql.append(sql)
        self.captured_params.append(args)
        return []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.captured_sql.append(sql)
        self.captured_params.append(args)
        return 0


class _PoolWrapper:
    """把 _FakeAcquire 暴露成 pool.acquire() 形式。"""

    def __init__(self, acquire: _FakeAcquire) -> None:
        self._acquire = acquire

    def acquire(self) -> _FakeAcquire:
        return self._acquire


@pytest.fixture
def fake_pool() -> _FakeAcquire:
    """挂一个可被 list_media / count_media 用的假 pool。"""
    acquire = _FakeAcquire()
    repo = PostgresRepository(dsn="postgresql://test/test")
    repo._pool = _PoolWrapper(acquire)  # type: ignore[assignment]
    # 让测试直接拿到 acquire 句柄以便断言 captured_sql
    acquire._repo = repo  # type: ignore[attr-defined]
    return acquire


def _repo(fake_pool: _FakeAcquire) -> PostgresRepository:
    return fake_pool._repo  # type: ignore[attr-defined]


# ---- list_media SQL ----


async def test_list_media_sql_no_me_media_idx(fake_pool: _FakeAcquire) -> None:
    """回归 2026-09-25 bug:`me.media_idx` 不能出现在 SELECT(列不存在)。"""
    await _repo(fake_pool).list_media()
    sql = fake_pool.captured_sql[0]
    assert "me.media_idx" not in sql, (
        f"SQL 含 SELECT me.media_idx — 2026-09-25 bug 又回来了:\n{sql}"
    )


async def test_list_media_sql_uses_row_number_window(fake_pool: _FakeAcquire) -> None:
    """修复要求:`ROW_NUMBER() OVER (PARTITION BY m.id ORDER BY me.id) - 1 AS media_idx`
    在 SELECT 列表里(等价 InMemory enumerate / SQLite ROW_NUMBER 顺序)。"""
    await _repo(fake_pool).list_media()
    sql = fake_pool.captured_sql[0]
    assert "ROW_NUMBER() OVER (PARTITION BY m.id ORDER BY me.id) - 1 AS media_idx" in sql, (
        f"SQL 缺窗口函数(运行时生成 media_idx):\n{sql}"
    )


async def test_list_media_sql_partition_by_msg_id(fake_pool: _FakeAcquire) -> None:
    """PARTITION BY 必须用 m.id(messages.id,每条 message 单独编 media 序号);
    错用 me.id 会导致全表单递增,等价于无 partition。"""
    await _repo(fake_pool).list_media()
    sql = fake_pool.captured_sql[0]
    assert "PARTITION BY m.id" in sql, f"窗口函数 PARTITION BY 必须用 messages.id:\n{sql}"


async def test_list_media_sql_order_by_window_alias(fake_pool: _FakeAcquire) -> None:
    """ORDER BY ... , m.id DESC, media_idx ASC — `media_idx` 用窗口别名,
    不能写成 `me.media_idx`(列不存在)。"""
    await _repo(fake_pool).list_media()
    sql = fake_pool.captured_sql[0]
    assert "m.id DESC, media_idx ASC" in sql, (
        f"ORDER BY tie-breaker 应是 m.id DESC, media_idx ASC:\n{sql}"
    )


async def test_list_media_sql_with_filters_keeps_window(fake_pool: _FakeAcquire) -> None:
    """带 filter 的 list_media SQL 也必须含窗口函数(filter 是 WHERE 子句,
    SELECT/ORDER BY 不变)。"""
    await _repo(fake_pool).list_media(
        channel_ids=[1, 2],
        status=MediaDownloadStatus.DONE,
        media_type=MediaType.PHOTO,
        search="cat",
    )
    sql = fake_pool.captured_sql[0]
    assert "ROW_NUMBER() OVER" in sql
    assert "me.media_idx" not in sql
    # filter 生效
    assert "channel_id" in sql
    assert "download_status" in sql
    assert "type" in sql
    assert "LOWER" in sql


# ---- count_media SQL ----


async def test_count_media_sql_unchanged(fake_pool: _FakeAcquire) -> None:
    """count_media 不 SELECT media_idx,本来就工作;确保本次修改没误伤它。"""
    await _repo(fake_pool).count_media()
    sql = fake_pool.captured_sql[0]
    assert "media_idx" not in sql, f"count_media 不该 SELECT media_idx 列:\n{sql}"
    assert "ROW_NUMBER" not in sql
    assert "count(*)" in sql


# ---- limit/offset 参数 ----


async def test_list_media_sql_limit_offset_appended(fake_pool: _FakeAcquire) -> None:
    """limit > 0 时 SQL 末尾追加 `LIMIT $N OFFSET $N+1`。"""
    await _repo(fake_pool).list_media(limit=10, offset=20)
    sql = fake_pool.captured_sql[0]
    # 默认 list_media(... limit=500): 但 default 是 1000 仍 > 0;测试用 limit=10 显式
    assert "LIMIT" in sql and "OFFSET" in sql
    assert "media_idx ASC" in sql
    # 参数也传了
    assert fake_pool.captured_params[0] == (10, 20)


async def test_list_media_sql_limit_zero_means_empty(fake_pool: _FakeAcquire) -> None:
    """P2-5 回归:limit=0 语义 = 空结果 — 仍追加 LIMIT/OFFSET(参数 0, 0)。

    旧实现 `if limit:` 把 0 当 False 跳过 LIMIT,意外返回全表。
    """
    await _repo(fake_pool).list_media(limit=0)
    sql = fake_pool.captured_sql[0]
    assert "LIMIT" in sql and "OFFSET" in sql, f"limit=0 应显式 LIMIT 0(空结果):\n{sql}"
    assert fake_pool.captured_params[0] == (0, 0)


# ---- sort 列 NULL 处理统一(P2-44:IS NULL 前置键,与 SQLite 逐字一致) ----


async def test_list_media_sql_sort_size_desc_nulls_last(
    fake_pool: _FakeAcquire,
) -> None:
    """sort=SIZE + sort_dir=DESC → `me.file_size IS NULL, me.file_size DESC`。

    2026-11-25 P2-44 回归:SQLite 用 NULLS LAST、PG 用 ASC(默认 NULLS FIRST)
    两后端 NULL 排序位置分叉;统一为 `{col} IS NULL` 前置键(NULL 行恒排最后)。
    旧 bug 形态(`NULLS LAST DESC` / 纯 `ASC`)不得再现。
    """
    from tgmonitor.core.dto import SortDir, SortKey

    await _repo(fake_pool).list_media(sort=SortKey.SIZE, sort_dir=SortDir.DESC)
    sql = fake_pool.captured_sql[0]
    assert "me.file_size IS NULL, me.file_size DESC" in sql, (
        f"sort=SIZE + DESC 应拼出 `me.file_size IS NULL, me.file_size DESC`:\n{sql}"
    )
    assert "NULLS" not in sql, f"NULLS LAST/NULLS FIRST 修饰符已废弃(双后端分叉来源):\n{sql}"


async def test_list_media_sql_sort_size_asc_nulls_last(
    fake_pool: _FakeAcquire,
) -> None:
    """sort=SIZE + sort_dir=ASC → `me.file_size IS NULL, me.file_size ASC`
    (NULL 行仍排最后,两方向一致)。"""
    from tgmonitor.core.dto import SortDir, SortKey

    await _repo(fake_pool).list_media(sort=SortKey.SIZE, sort_dir=SortDir.ASC)
    sql = fake_pool.captured_sql[0]
    assert "me.file_size IS NULL, me.file_size ASC" in sql, (
        f"sort=SIZE + ASC 应拼出 `me.file_size IS NULL, me.file_size ASC`:\n{sql}"
    )


async def test_list_media_sql_sort_date_no_nulls_clause(
    fake_pool: _FakeAcquire,
) -> None:
    """sort=DATE / STATUS 都没 NULLS 修饰(列 NOT NULL 或不需要 NULLS LAST)。"""
    from tgmonitor.core.dto import SortKey

    await _repo(fake_pool).list_media(sort=SortKey.DATE)
    sql = fake_pool.captured_sql[0]
    assert "NULLS" not in sql, f"DATE 排序不该有 NULLS 修饰:\n{sql}"


# ---- P1-12 回归(2026-11-25):list_media 必须整行还原 message 元数据 ----


class _FakeRowsConn:
    """返预置 rows 的 conn mock — fetch 返回伪 Record(dict 鸭子类型)。"""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        return self._rows


class _FakeRowsAcquire:
    async def __aenter__(self) -> _FakeRowsConn:
        return self._conn

    async def __aexit__(self, *args: object) -> bool:
        return False

    def __init__(self, conn: _FakeRowsConn) -> None:
        self._conn = conn


class _FakeRowsPool:
    def __init__(self, rows: list[dict]) -> None:
        self._acquire = _FakeRowsAcquire(_FakeRowsConn(rows))

    def acquire(self) -> _FakeRowsAcquire:
        return self._acquire


def _full_list_media_row() -> dict[str, Any]:
    """伪造 list_media 的 JOIN 行:messages(m.*)全部列 + media 别名列。

    列集与 `_media_pg.list_media` 的 SQL(`SELECT m.*, me.* AS 别名`)对齐。
    """
    return {
        # messages 表全部列(含 v1.7.2 用户元数据 + v1.4.0 互动字段)
        "id": 7,
        "channel_id": 100,
        "telegram_msg_id": 42,
        "author": "alice",
        "date": datetime(2026, 1, 1, 12, 0, 0),
        "text": "hello",
        "views": 123,
        "forwards": 4,
        "reply_to_msg_id": None,
        "edited": False,
        "raw": {"id": 42},
        "forward_origin": None,
        "via_bot_user_id": None,
        "media_album_id": None,
        "is_pinned": True,
        "reactions": None,
        "is_favorite": True,
        "tags": ["tech", "ai"],
        "notes": "需要 review",
        # media 别名列
        "media_id": 3,
        "media_type": "photo",
        "mime_type": "image/jpeg",
        "file_name": "pic.jpg",
        "file_size": 1234,
        "width": 800,
        "height": 600,
        "duration": None,
        "telegram_file_id": "11",
        "object_key": "media/abc.jpg",
        "object_backend": "local",
        "thumbnail_telegram_file_id": "33",
        "emoji": None,
        "media_dl_status": "done",
        "download_error": None,
        "media_idx": 0,
    }


async def test_list_media_restores_full_message_metadata() -> None:
    """P1-12 回归:list_media 把整行 Record 传给 _row_to_message,
    is_pinned / is_favorite / tags / notes / forward_origin / via_bot_user_id /
    media_album_id 必须被还原(与 SQLite `SELECT m.*` 整行还原对齐)。

    旧实现 msg_keys 白名单只挑 11 列,上述字段全被滤成默认值。
    """
    from tgmonitor.core.storage import _media_pg
    from tgmonitor.core.storage.postgres_repo import PostgresRepository

    repo = PostgresRepository(dsn="postgresql://test/test")
    repo._pool = _FakeRowsPool([_full_list_media_row()])  # type: ignore[assignment]

    result = await _media_pg.list_media(repo)
    assert len(result) == 1
    msg, media_idx, media = result[0]

    assert media_idx == 0
    assert msg.is_pinned is True
    assert msg.is_favorite is True
    assert msg.tags == ["tech", "ai"]
    assert msg.notes == "需要 review"
    assert msg.views == 123
    assert msg.forwards == 4
    assert msg.raw == {"id": 42}
    assert media.file_name == "pic.jpg"
    assert media.object_key == "media/abc.jpg"
    assert media.download_status is not None and media.download_status.value == "done"


async def test_list_media_restores_reactions_and_optional_fields() -> None:
    """reactions(JSONB list[dict])与 forward_origin / via_bot_user_id /
    media_album_id 也必须随整行还原,不得被白名单滤掉。"""
    from tgmonitor.core.storage import _media_pg
    from tgmonitor.core.storage.postgres_repo import PostgresRepository

    row = _full_list_media_row()
    row["reactions"] = [{"type": "emoji", "emoji": "👍", "count": 3, "is_chosen": False}]
    row["forward_origin"] = {"type": "channel", "chat_id": 555}
    row["via_bot_user_id"] = 999
    row["media_album_id"] = "album-1"

    repo = PostgresRepository(dsn="postgresql://test/test")
    repo._pool = _FakeRowsPool([row])  # type: ignore[assignment]

    result = await _media_pg.list_media(repo)
    msg = result[0][0]
    assert msg.reactions is not None and len(msg.reactions) == 1
    assert msg.reactions[0].emoji == "👍"
    assert msg.reactions[0].count == 3
    assert msg.forward_origin == {"type": "channel", "chat_id": 555}
    assert msg.via_bot_user_id == 999
    assert msg.media_album_id == "album-1"


# ---- P2-1(2026-11-25):媒体搜索 LIKE 通配符转义 ----


async def test_list_media_search_escapes_like_wildcards(fake_pool: _FakeAcquire) -> None:
    """P2-1 回归:list_media 的 search 必须转义 % / _ / \\ + ESCAPE 子句。

    旧实现直接拼 `%{search}%`,用户输入 `_` 当单字符通配符,
    与 list_messages 的正确实现不一致。
    """
    await _repo(fake_pool).list_media(search="a_c")
    sql = fake_pool.captured_sql[0]
    assert "ESCAPE" in sql, f"search LIKE 必须带 ESCAPE 子句:\n{sql}"
    params = fake_pool.captured_params[0]
    like_params = [p for p in params if isinstance(p, str) and p.startswith("%")]
    assert like_params and all(p == "%a\\_c%" for p in like_params), (
        f"search 通配符未转义: {like_params}"
    )


async def test_list_media_search_escapes_percent(fake_pool: _FakeAcquire) -> None:
    """`100%` 应只按字面匹配,`%` 不得当任意串通配符。"""
    await _repo(fake_pool).list_media(search="100%")
    like_params = [
        p for p in fake_pool.captured_params[0] if isinstance(p, str) and p.startswith("%")
    ]
    assert like_params and all(p == "%100\\%%" for p in like_params)


# ---- P2-7(2026-11-25):PG 写路径 naive datetime 统一视为 UTC ----


class _CaptureConn:
    """捕获 execute/fetchrow 调用的 conn mock — save_message 路径用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _CaptureConn:
        return self

    async def __aenter__(self) -> _CaptureConn:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def execute(self, sql: str, *args: Any) -> None:
        self.calls.append((sql, args))

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        self.calls.append((sql, args))
        return {"id": 1}


class _CapturePool:
    def __init__(self, conn: _CaptureConn) -> None:
        self._conn = conn

    def acquire(self) -> _CaptureConn:
        return self._conn


async def test_save_message_naive_date_treated_as_utc() -> None:
    """P2-7 回归:save_message 传 naive datetime → 参数必须带 UTC tzinfo。

    旧实现直接把 naive 传给 asyncpg → PG 按服务器时区解释 TIMESTAMPTZ,
    与 SQLite 端时间线分叉。
    """
    from datetime import datetime

    from tgmonitor.core.dto import MessageDTO
    from tgmonitor.core.storage import _messages_pg

    repo = PostgresRepository(dsn="postgresql://test/test")
    conn = _CaptureConn()
    repo._pool = _CapturePool(conn)  # type: ignore[assignment]

    msg = MessageDTO(
        id=0,
        channel_id=100,
        telegram_msg_id=1,
        text="x",
        date=datetime(2026, 1, 1, 12, 0, 0),  # naive
    )
    await _messages_pg.save_message(repo, msg)

    insert_calls = [(s, a) for s, a in conn.calls if "INSERT INTO messages" in s]
    assert insert_calls, "未捕获 messages INSERT"
    date_param = insert_calls[0][1][3]
    assert getattr(date_param, "tzinfo", None) is not None, f"naive date 未打 tzinfo: {date_param}"
    assert date_param.utcoffset().total_seconds() == 0


async def test_list_messages_naive_date_filters_treated_as_utc(fake_pool: _FakeAcquire) -> None:
    """P2-7 回归:list_messages 的 date_from / date_to naive 参数同样视为 UTC。"""
    from datetime import datetime

    await _repo(fake_pool).list_messages(
        [1],
        date_from=datetime(2026, 1, 1, 0, 0, 0),
        date_to=datetime(2026, 1, 2, 0, 0, 0),
    )
    params = fake_pool.captured_params[0]
    date_from, date_to = params[1], params[2]
    for p in (date_from, date_to):
        assert getattr(p, "tzinfo", None) is not None, f"过滤边界 naive 未打 tzinfo: {p}"
        assert p.utcoffset().total_seconds() == 0


async def test_list_messages_tag_filter_pushed_to_sql(fake_pool: _FakeAcquire) -> None:
    """P2-14 回归:list_messages 支持 tag= 精确匹配,SQL 下推(`= ANY(tags)`)。"""
    await _repo(fake_pool).list_messages([1], limit=10, tag="tech")
    sql = fake_pool.captured_sql[0]
    assert "= ANY(tags)" in sql, f"tag 过滤必须下推 SQL WHERE 层:\n{sql}"
    params = fake_pool.captured_params[0]
    assert "tech" in params


# ---- P2-2 / P2-3(2026-11-25):init_schema 拆 try + 媒体 done 迁移 one-shot ----


class _SchemaConn:
    """init_schema 路径的 conn mock — 可按 SQL 片段注入失败,meta 探测可定制。"""

    def __init__(
        self,
        fail_on: tuple[str, ...] = (),
        meta_marker: str | None = None,
    ) -> None:
        self.fail_on = fail_on
        self.meta_marker = meta_marker
        self.executed: list[str] = []
        self.executed_args: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> _SchemaConn:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "FROM meta WHERE key" in sql:
            return self.meta_marker
        return None  # information_schema 探测:无旧列

    async def execute(self, sql: str, *args: Any) -> None:
        for pat in self.fail_on:
            if pat in sql:
                raise RuntimeError(f"permission denied: {pat}")
        self.executed.append(sql)
        if args:
            self.executed_args.append((sql, args))


class _SchemaPool:
    def __init__(self, conn: _SchemaConn) -> None:
        self._conn = conn

    def acquire(self) -> _SchemaConn:
        return self._conn


def _schema_repo(conn: _SchemaConn) -> PostgresRepository:
    repo = PostgresRepository(dsn="postgresql://test/test")
    repo._pool = _SchemaPool(conn)  # type: ignore[assignment]
    return repo


async def test_init_schema_core_ddl_failure_raises() -> None:
    """P2-2 回归:核心 DDL(CREATE TABLE)失败必须上抛,不得吞异常带病启动。

    旧实现单一 `except Exception` 把 SCHEMA_SQL 的失败也吞了。
    """
    from tgmonitor.core.storage import _lifecycle_pg

    conn = _SchemaConn(fail_on=("CREATE TABLE",))
    with pytest.raises(RuntimeError, match="permission denied"):
        await _lifecycle_pg.init_schema(_schema_repo(conn))


async def test_init_schema_trgm_failure_degrades_to_warning() -> None:
    """P2-2:pg_trgm 权限不足只降级 warning,不抛、不影响核心 DDL。"""
    from tgmonitor.core.storage import _lifecycle_pg

    conn = _SchemaConn(fail_on=("pg_trgm",))
    await _lifecycle_pg.init_schema(_schema_repo(conn))  # 不抛
    assert any("CREATE TABLE" in s for s in conn.executed), "核心 DDL 必须已执行"


async def test_init_schema_media_done_migration_is_one_shot() -> None:
    """P2-3 回归:meta 标记存在时,init_schema 不得再执行 media done 迁移 UPDATE。

    旧实现把 UPDATE 内联在 SCHEMA_SQL 里每次启动无条件重跑。
    """
    from tgmonitor.core.storage import _lifecycle_pg

    conn = _SchemaConn(meta_marker="1")
    await _lifecycle_pg.init_schema(_schema_repo(conn))
    assert not any("UPDATE media SET download_status = 'done'" in s for s in conn.executed), (
        "标记已存在,不应重跑迁移"
    )


async def test_init_schema_media_done_migration_runs_when_unmarked() -> None:
    """P2-3:无 meta 标记(首次升级/全新库)→ 执行一次迁移并写标记。"""
    from tgmonitor.core.storage import _lifecycle_pg

    conn = _SchemaConn(meta_marker=None)
    await _lifecycle_pg.init_schema(_schema_repo(conn))
    assert any("UPDATE media SET download_status = 'done'" in s for s in conn.executed)
    assert any(
        "INSERT INTO meta" in s
        and any("media_object_key_done" in str(a) for _, a in conn.executed_args)
        for s in conn.executed
    ), f"迁移后必须写 meta 标记: {conn.executed}"
