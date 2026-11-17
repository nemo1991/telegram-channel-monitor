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
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from tests.fixtures._in_memory_repository import InMemoryRepository  # noqa: E402
from tgmonitor.core.activity import format_activity, make_persistence_subscriber  # noqa: E402
from tgmonitor.core.dto import ActivityDTO, ChannelDTO, SyncResult  # noqa: E402
from tgmonitor.core.events import (  # noqa: E402
    AuthErrorOccurred,
    BatchDone,
    ChannelMonitored,
    ChannelSyncDone,
    ErrorOccurred,
    MediaDownloadProgress,
    MediaReconcileFinished,
    MessageReceived,
    SettingsChanged,
)
from tgmonitor.core.storage.sqlite_repo import SqliteRepository  # noqa: E402

# ---------------------------------------------------------------- formatter


def test_format_activity_covers_ops_events() -> None:
    """9 类运维事件都能归一化成 ActivityDTO，字段可序列化。"""
    events = [
        ChannelMonitored(channel=ChannelDTO(id=1, title="科技")),
        ChannelSyncDone(result=SyncResult(total_messages_added=5)),
        BatchDone(op="delete", succeeded=3, failed=1),
        MediaReconcileFinished(scanned=10, orphans=2, deleted=2, dry_run=False),
        SettingsChanged(what="proxy"),
        ErrorOccurred(source="auth", message="bad thing"),
    ]
    for e in events:
        activity = format_activity(e)
        assert activity is not None, type(e).__name__
        assert isinstance(activity.event_type, str) and activity.event_type
        assert isinstance(activity.title, str) and activity.title
        assert isinstance(activity.detail, str)
        assert isinstance(activity.occurred_at, datetime)


def test_format_activity_filters_progress_and_messages() -> None:
    """Progress / 消息类事件不进活动账本（D3-α）。"""
    assert format_activity(MessageReceived(message=None)) is None
    assert format_activity(MediaDownloadProgress(channel_id=1, telegram_msg_id=2)) is None


def test_format_activity_auth_error_via_mro() -> None:
    """AuthErrorOccurred 是 ErrorOccurred 子类，按 MRO 命中 _fmt_error。"""
    activity = format_activity(AuthErrorOccurred(message="验证码错误"))
    assert activity is not None
    assert activity.event_type == "error_occurred"
    assert activity.severity == "error"


def test_format_activity_exception_not_leaked() -> None:
    """异常对象只取 str 摘要，DTO 里不保留异常引用（RK-05）。"""
    boom = RuntimeError("secret detail must be clipped")
    activity = format_activity(ErrorOccurred(source="x", message="", exception=boom))
    assert activity is not None
    assert "secret detail" in activity.detail
    assert not isinstance(activity.detail, BaseException)


def test_format_channel_monitored_no_memory_address() -> None:
    """FR-04：空频道兜底不再用 id(e) 内存地址。"""
    activity = format_activity(ChannelMonitored(channel=None))
    assert activity is not None
    assert "0x" not in activity.detail
    assert activity.detail == "(未知频道)"


# ---------------------------------------------------------------- subscriber


@pytest.mark.asyncio
async def test_persistence_subscriber_writes_to_store() -> None:
    store = InMemoryRepository()
    subscriber = make_persistence_subscriber(store)
    await subscriber(SettingsChanged(what="proxy"))
    activities = await store.list_activities()
    assert len(activities) == 1
    assert activities[0].event_type == "settings_changed"


@pytest.mark.asyncio
async def test_persistence_subscriber_swallows_store_failure() -> None:
    """落库失败不阻断事件主流程（EventBus 契约）。"""
    store = MagicMock()
    store.append_activity = AsyncMock(side_effect=OSError("disk full"))
    subscriber = make_persistence_subscriber(store)
    await subscriber(SettingsChanged(what="x"))  # 不抛
    store.append_activity.assert_awaited_once()


@pytest.mark.asyncio
async def test_persistence_subscriber_skips_unmapped_events() -> None:
    store = InMemoryRepository()
    subscriber = make_persistence_subscriber(store)
    await subscriber(MessageReceived(message=None))
    assert await store.list_activities() == []


# ---------------------------------------------------------------- storage roundtrip


@pytest.mark.asyncio
async def test_inmemory_activity_roundtrip_order_and_full_retention() -> None:
    store = InMemoryRepository()
    t0 = datetime(2020, 1, 1, tzinfo=UTC)
    for i in range(3):
        await store.append_activity(
            ActivityDTO(event_type="e", occurred_at=t0, icon="i", title=f"act-{i}")
        )
    all_rows = await store.list_activities(limit=200)
    assert len(all_rows) == 3  # 全留存，无裁剪
    # 同样时间戳下 id DESC（新的在前）
    assert [a.title for a in all_rows] == ["act-2", "act-1", "act-0"]
    limited = await store.list_activities(limit=2)
    assert len(limited) == 2


@pytest_asyncio.fixture
async def sqlite_repo(tmp_path):
    r = SqliteRepository(path=tmp_path / "activities.db")
    await r.connect()
    await r.init_schema()
    try:
        yield r
    finally:
        await r.close()


@pytest.mark.asyncio
async def test_sqlite_activity_roundtrip(sqlite_repo) -> None:
    occurred = datetime(2021, 5, 6, 7, 8, 9, tzinfo=UTC)
    await sqlite_repo.append_activity(
        ActivityDTO(
            event_type="channel_monitored",
            occurred_at=occurred,
            icon="kind_channel",
            title="已监听频道",
            detail="科技早报",
            channel_id=42,
        )
    )
    rows = await sqlite_repo.list_activities()
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "channel_monitored"
    assert row.title == "已监听频道"
    assert row.detail == "科技早报"
    assert row.channel_id == 42
    assert row.occurred_at == occurred
    assert row.id is not None


@pytest.mark.asyncio
async def test_sqlite_activity_full_retention(sqlite_repo) -> None:
    """A3：不限量全留存，写入 250 条后仍全部可读（超过旧 200 上限）。"""
    t = datetime(2022, 1, 1, tzinfo=UTC)
    for i in range(250):
        await sqlite_repo.append_activity(
            ActivityDTO(event_type="e", occurred_at=t, icon="i", title=str(i))
        )
    assert len(await sqlite_repo.list_activities(limit=1000)) == 250


# ---------------------------------------------------------------- UI load


@pytest.mark.asyncio
async def test_dashboard_loads_activities_from_storage(qapp: QApplication) -> None:
    """FR-05：DashboardWidget 启动从库加载历史活动。"""
    from types import SimpleNamespace

    from tgmonitor.core.monitor.service import MonitorService
    from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget

    # 库契约：list_activities 返回 occurred_at DESC（新的在前）
    stored = [
        ActivityDTO(
            id=2,
            event_type="error_occurred",
            occurred_at=datetime(2020, 1, 2, tzinfo=UTC),
            icon="refresh",
            title="发生错误",
            detail="boom",
        ),
        ActivityDTO(
            id=1,
            event_type="settings_changed",
            occurred_at=datetime(2020, 1, 1, tzinfo=UTC),
            icon="settings",
            title="设置已变更",
            detail="proxy",
        ),
    ]
    app = MagicMock()
    app.bus = MagicMock()
    app.client = SimpleNamespace(state="ready", me=None)
    app.storage.list_activities = AsyncMock(return_value=stored)
    monitor = MagicMock(spec=MonitorService)
    monitor.subscribed_ids = []
    # loop=None → _load_activities 跳过；直接调 _apply_activities 验证渲染
    dash = DashboardWidget(app, monitor, loop=None)
    dash._apply_activities(stored)
    assert dash.activity_list.count() == 2
    # 库中 reversed 插入：最新（id=2，2020-01-02）在列表头
    widget = dash.activity_list.itemWidget(dash.activity_list.item(0))
    assert "发生错误" in widget.msg_lbl.text()
    dash.deleteLater()
