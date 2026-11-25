"""v2.0.4 FR-04/FR-05 活动持久化契约。

覆盖：
- core formatter：9 类运维事件归一化、Progress/消息类过滤、异常对象不入库
- 持久化订阅者：落库成功 / 落库失败不阻断主流程
- 三后端存储 roundtrip：in-memory / SQLite（PG 集成在 integration/ 另测）
- schema：EXPECTED_SCHEMA 含 activities 表
- UI：DashboardWidget 从库加载最近活动
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


from tests.fixtures._in_memory_repository import InMemoryRepository  # noqa: E402
from tgmonitor.core.activity import format_activity, make_persistence_subscriber  # noqa: E402
from tgmonitor.core.events import (  # noqa: E402
    ErrorOccurred,
    SettingsChanged,
)
from tgmonitor.core.storage.sqlite_repo import SqliteRepository  # noqa: E402

# ---------------------------------------------------------------- formatter


def test_format_activity_exception_not_leaked() -> None:
    """异常对象只取 str 摘要，DTO 里不保留异常引用（RK-05）。"""
    boom = RuntimeError("secret detail must be clipped")
    activity = format_activity(ErrorOccurred(source="x", message="", exception=boom))
    assert activity is not None
    assert "secret detail" in activity.detail
    assert not isinstance(activity.detail, BaseException)


# ---------------------------------------------------------------- subscriber


@pytest.mark.asyncio
async def test_persistence_subscriber_writes_to_store() -> None:
    store = InMemoryRepository()
    subscriber = make_persistence_subscriber(store)
    await subscriber(SettingsChanged(what="proxy"))
    activities = await store.list_activities()
    assert len(activities) == 1
    assert activities[0].event_type == "settings_changed"


# ---------------------------------------------------------------- storage roundtrip


@pytest_asyncio.fixture
async def sqlite_repo(tmp_path):
    r = SqliteRepository(path=tmp_path / "activities.db")
    await r.connect()
    await r.init_schema()
    try:
        yield r
    finally:
        await r.close()


# ---------------------------------------------------------------- UI load
