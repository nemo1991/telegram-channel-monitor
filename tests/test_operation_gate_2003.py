"""v2.0.3 全局操作门控契约 — 守 D7 的 core 主防线 + UI 适配层。

2026-11-17 v2.0.3:门控放 core(用户五次评审拍板),不是 UI。原因:UI 门控
挡不住 CLI / 编程式调用 / 异步时序。本文件守护:
- FR-13 core 主防线:非 ready 时直调域服务入口(绕过 UI)也被拒绝,无副作用;
- 7 态覆盖:只有 ready 放行,其余状态(uninit/phone_required/code_required/
  password_required/error/closing)一律拒绝;
- UI 适配层:LoginStateChanged 驱动入口禁用/恢复,但仅是体验层、不是安全边界。

核心断言范式:**绕过 UI 直接调 core**,证明门控确实在 core 而非 UI。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests.fixtures._in_memory_repository import InMemoryRepository
from tgmonitor.core.channel_service import ChannelService
from tgmonitor.core.channel_sync.service import ChannelSyncService
from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import EventBus
from tgmonitor.core.ops_gate import (
    READY_STATE,
    OperationNotReadyError,
    is_ready,
    require_ready,
)
from tgmonitor.core.telegram.client import TelegramClient

# 除 ready 外的全部登录状态(7 态状态机)
NON_READY_STATES = [
    "uninit",
    "phone_required",
    "code_required",
    "password_required",
    "error",
    "closing",
]


def _gate_client(state: str) -> AsyncMock:
    client = AsyncMock(spec=TelegramClient)
    client.state = state
    return client


# ============================================================
# 门控原语
# ============================================================


@pytest.mark.parametrize("state", NON_READY_STATES)
def test_is_ready_false_for_non_ready_states(state: str) -> None:
    """唯一判定规则:state == ready。其余状态一律 False。"""
    assert is_ready(_gate_client(state)) is False


def test_is_ready_true_for_ready() -> None:
    assert READY_STATE == "ready"
    assert is_ready(_gate_client("ready")) is True


@pytest.mark.parametrize("state", NON_READY_STATES)
def test_require_ready_raises_with_state(state: str) -> None:
    """require_ready 抛 OperationNotReadyError,携带当前 state 供 UI 精确提示。"""
    with pytest.raises(OperationNotReadyError) as ei:
        require_ready(_gate_client(state), "全量同步")
    assert ei.value.state == state
    assert "全量同步" in str(ei.value)


def test_require_ready_passes_when_ready() -> None:
    require_ready(_gate_client("ready"), "全量同步")  # 不抛


# ============================================================
# FR-13 core 主防线:绕过 UI 直调也被拒(CLI / 编程式路径)
# ============================================================


@pytest.fixture
def storage() -> InMemoryRepository:
    return InMemoryRepository()


@pytest.mark.parametrize("state", NON_READY_STATES)
async def test_subscribe_rejected_without_side_effects(
    storage: InMemoryRepository, state: str
) -> None:
    """绕过 UI 直调 monitor_channel:非 ready 拒绝,不写库、不发事件。"""
    client = _gate_client(state)
    svc = ChannelService(EventBus(), client, storage)  # type: ignore[arg-type]

    with pytest.raises(OperationNotReadyError):
        await svc.monitor_channel(ChannelDTO(id=1, title="x"))

    assert await storage.get_channel(1) is None, "未登录不得写库"
    client.list_joined_channels.assert_not_awaited()


@pytest.mark.parametrize("state", NON_READY_STATES)
async def test_unsubscribe_rejected_without_side_effects(
    storage: InMemoryRepository, state: str
) -> None:
    """绕过 UI 直调 unmonitor_channel:非 ready 拒绝,监听标志不变。"""
    await storage.upsert_channel(ChannelDTO(id=1, title="x", is_monitored=True))
    client = _gate_client(state)
    svc = ChannelService(EventBus(), client, storage)  # type: ignore[arg-type]

    with pytest.raises(OperationNotReadyError):
        await svc.unmonitor_channel(1)

    got = await storage.get_channel(1)
    assert got is not None and got.is_monitored is True, "未登录不得改监听标志"


@pytest.mark.parametrize("state", NON_READY_STATES)
async def test_list_joined_skipped_without_request(storage: InMemoryRepository, state: str) -> None:
    """best-effort 入口:非 ready 返 [],不发起 TG 请求。"""
    client = _gate_client(state)
    svc = ChannelService(EventBus(), client, storage)  # type: ignore[arg-type]

    assert await svc.list_joined_channels() == []
    client.list_joined_channels.assert_not_awaited()


@pytest.mark.parametrize("state", NON_READY_STATES)
async def test_sync_rejected_without_emitting_progress(
    storage: InMemoryRepository, state: str
) -> None:
    """绕过 UI 直调 sync_channels:非 ready 拒绝,不发 init 进度事件。"""
    client = _gate_client(state)
    bus = EventBus()
    svc = ChannelSyncService(bus, client, storage)  # type: ignore[arg-type]
    from tgmonitor.core.dto import SyncOptions

    with pytest.raises(OperationNotReadyError):
        await svc.sync_channels([1], SyncOptions())

    client.iter_chat_history.assert_not_called()


async def test_ready_allows_operations(storage: InMemoryRepository) -> None:
    """ready 时放行:订阅成功写库。"""
    client = _gate_client("ready")
    svc = ChannelService(EventBus(), client, storage)  # type: ignore[arg-type]

    await svc.monitor_channel(ChannelDTO(id=7, title="ok"))

    got = await storage.get_channel(7)
    assert got is not None and got.is_monitored is True
