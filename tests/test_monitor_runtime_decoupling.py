"""P2-M4 解耦守护 — 5 组件不再反向持有 MonitorService,状态经共享 runtime 流转。

防止未来改动把 `self._svc: MonitorService` 双向耦合引回来(原 78 处引用)。
守护的是架构契约,不是实现细节:组件只依赖 `MonitorRuntime`,宿主保留
全部公开 / 测试 seam 的 property 与 delegate。
"""

from __future__ import annotations

import ast
from pathlib import Path

from tgmonitor.core.monitor._runtime import MonitorRuntime
from tgmonitor.core.monitor.backfill import BackfillRunner
from tgmonitor.core.monitor.delete import DeleteManager
from tgmonitor.core.monitor.event_handlers import EventHandlers
from tgmonitor.core.monitor.orchestrator import Orchestrator
from tgmonitor.core.monitor.whitelist import WhitelistManager

_COMPONENT_FILES = (
    "whitelist.py",
    "orchestrator.py",
    "backfill.py",
    "event_handlers.py",
    "delete.py",
)


def test_components_do_not_import_or_annotate_monitor_service() -> None:
    """组件源码零 import MonitorService / 零 `self._svc` 反向引用。"""
    root = Path(__file__).resolve().parents[1] / "src/tgmonitor/core/monitor"
    offenders: list[str] = []
    for name in _COMPONENT_FILES:
        src = (root / name).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and "monitor.service" in node.module
            ):
                offenders.append(f"{name}: imports MonitorService")
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr == "_svc"
            ):
                offenders.append(f"{name}: self._svc at line {node.lineno}")
    assert not offenders, "组件不得反向持有 MonitorService:\n" + "\n".join(offenders)


def test_components_accept_shared_runtime() -> None:
    """5 组件构造签名首参均为 runtime(经注解可核对),不再接宿主 svc。"""
    for cls in (WhitelistManager, Orchestrator, BackfillRunner, EventHandlers, DeleteManager):
        ann = cls.__init__.__annotations__.get("rt")
        assert ann is not None, f"{cls.__name__}.__init__ 缺少 rt 参数"
        assert "MonitorRuntime" in str(ann), f"{cls.__name__} 首参应持 MonitorRuntime,得 {ann}"


def test_runtime_owns_all_shared_state_fields() -> None:
    """runtime 持有全部原先散在宿主上的状态字段。"""
    fields = set(MonitorRuntime.__dataclass_fields__)
    required = {
        "bus",
        "client",
        "storage",
        "objects",
        "settings",
        "downloader",
        "heartbeat_interval",
        "backfill_interval",
        "backfill_limit",
        "backfill_max_page",
        "whitelist",
        "stop",
        "seen_ids",
        "handled",
        "last_heartbeat",
        "skip_next_tick",
        "unavailable_channels",
        "stream",
        "task",
        "backfill_task",
        "download_task",
        "download_queue",
        "handle_message",
        "handle_edited",
        "backfill_all",
        "download_worker",
        "delete_with_orphan_check",
        "handle_connection_state",
        "backfill_loop",
    }
    assert required <= fields, f"runtime 缺少字段: {sorted(required - fields)}"
