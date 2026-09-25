"""PostgresRepository SQL 字符串回归测试 — 2026-09-25 fix(media-bug)。

`media` 表没有 `media_idx` 列(2026-08-25 PR #3 把 media 拆成独立表时漏了),
原 `list_media` SQL 写了 `SELECT me.media_idx`,在真 PG 跑报
`UndefinedColumnError`,被 `run_coro` 静默吞掉 → Media Manager 页空表。
修法:把 SELECT 里的 `me.media_idx` 换成窗口函数
`ROW_NUMBER() OVER (PARTITION BY m.id ORDER BY me.id) - 1 AS media_idx`,
与 Jsonl `enumerate` / Mongo `$unwind includeArrayIndex` 顺序对齐。

本测试不依赖真 PG(集成测试在 `tests/integration/test_pg_repo_list_media.py`),
mock `_pool` 拦截传给 `conn.fetch(...)` 的 SQL,断言关键关键字 / 禁用符号。

回归若有人误把 SELECT 改回 `me.media_idx`,本测试会立刻 fail。
"""

from __future__ import annotations

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
    在 SELECT 列表里(等价 Jsonl enumerate / Mongo unwind 顺序)。"""
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


async def test_list_media_sql_no_limit_when_zero(fake_pool: _FakeAcquire) -> None:
    """limit=0 → 不加 LIMIT/OFFSET(全量模式)。"""
    await _repo(fake_pool).list_media(limit=0)
    sql = fake_pool.captured_sql[0]
    assert "LIMIT" not in sql
    assert "OFFSET" not in sql


# ---- sort 列 NULLS LAST 修复 ----


async def test_list_media_sql_sort_size_desc_nulls_last(
    fake_pool: _FakeAcquire,
) -> None:
    """sort=SIZE + sort_dir=DESC → `me.file_size DESC NULLS LAST`,不是
    `me.file_size NULLS LAST DESC`(后者 PG 语法错误)。

    回归保护:之前 `_MEDIA_SORT_COLUMN[SortKey.SIZE] = "me.file_size NULLS LAST"`
    plain string 拼出无效 SQL,Media Manager UI 选「按大小降序」直接挂。
    """
    from tgmonitor.core.dto import SortDir, SortKey

    await _repo(fake_pool).list_media(sort=SortKey.SIZE, sort_dir=SortDir.DESC)
    sql = fake_pool.captured_sql[0]
    assert "me.file_size DESC NULLS LAST" in sql, (
        f"sort=SIZE + DESC 应拼出 `me.file_size DESC NULLS LAST`:\n{sql}"
    )
    # 反向 — 旧 bug 形态不能出现
    assert "NULLS LAST DESC" not in sql, (
        f"PG 语法错误:`NULLS LAST` 是修饰符,必须放在 direction 之后:\n{sql}"
    )


async def test_list_media_sql_sort_size_asc_nulls_last(
    fake_pool: _FakeAcquire,
) -> None:
    """sort=SIZE + sort_dir=ASC → `me.file_size ASC NULLS LAST`。"""
    from tgmonitor.core.dto import SortDir, SortKey

    await _repo(fake_pool).list_media(sort=SortKey.SIZE, sort_dir=SortDir.ASC)
    sql = fake_pool.captured_sql[0]
    assert "me.file_size ASC NULLS LAST" in sql, (
        f"sort=SIZE + ASC 应拼出 `me.file_size ASC NULLS LAST`:\n{sql}"
    )


async def test_list_media_sql_sort_date_no_nulls_clause(
    fake_pool: _FakeAcquire,
) -> None:
    """sort=DATE / STATUS 都没 NULLS 修饰(列 NOT NULL 或不需要 NULLS LAST)。"""
    from tgmonitor.core.dto import SortKey

    await _repo(fake_pool).list_media(sort=SortKey.DATE)
    sql = fake_pool.captured_sql[0]
    assert "NULLS" not in sql, f"DATE 排序不该有 NULLS 修饰:\n{sql}"
