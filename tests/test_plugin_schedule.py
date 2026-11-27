"""schedule 能力 — 宿主统一任务表的完整契约测试。

重点不是「任务能跑起来」,而是**跑起来之后能不能干净停下**:
- activate → start → pause → resume → shutdown 全链的任务物化 / 取消 / 回收;
- 单次任务完成态、周期任务失败隔离与快照;
- 显式取消跨 pause/resume 不被复活;
- 激活失败不留幽灵任务(与 exporter / 订阅回滚同级);
- 同步上下文(无事件循环)调用宿主钩子不炸、不留 Task。

插件用本地目录通道真实加载,回调写文件作为可观察副作用 —— 不用 mock 任务表,
断言的是宿主与插件之间的实际契约。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tgmonitor.core.config import Settings
from tgmonitor.core.events import EventBus
from tgmonitor.plugins.host import PluginHost, PluginState
from tgmonitor.plugins.manifest import PluginManifest
from tgmonitor.plugins.schedule import JobStatus, SchedulerView, TaskTable


def _make_plugin_dir(
    root: Path,
    plugin_id: str,
    *,
    body: str,
    caps: tuple[str, ...] = ("events", "schedule"),
) -> Path:
    d = root / plugin_id
    d.mkdir(parents=True, exist_ok=True)
    caps_str = ", ".join(f'"{c}"' for c in caps)
    (d / "plugin.toml").write_text(
        f'id = "{plugin_id}"\nname = "{plugin_id}"\nversion = "1.0.0"\n'
        f'min_app_version = "3.0.0"\ncapabilities = [{caps_str}]\n',
        encoding="utf-8",
    )
    (d / "plugin.py").write_text(body, encoding="utf-8")
    return d


def _enable(root: Path, *ids: str) -> None:
    (root / "enabled.json").write_text(json.dumps({"enabled": list(ids)}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_entry_points(monkeypatch):
    monkeypatch.setattr("tgmonitor.plugins.loader.discover_entry_points", lambda: [])


@pytest.fixture
def services():
    return {"messages": AsyncMock(), "media": AsyncMock(), "channels": AsyncMock()}


def _activate(root: Path, services) -> PluginHost:
    host = PluginHost(root)
    host.activate_all(EventBus(), app_services=services, settings=Settings.for_test())
    return host


def _interval_body(marker: Path, *, seconds: float = 0.02, name: str = "tick") -> str:
    return (
        "from pathlib import Path\n"
        f"MARKER = Path({str(marker)!r})\n"
        "class P:\n"
        "    manifest = None\n"
        "    def activate(self, ctx):\n"
        f"        ctx.scheduler.every({seconds!r}, self._tick, name={name!r}, "
        "run_immediately=True)\n"
        "    def _tick(self):\n"
        "        with MARKER.open('a', encoding='utf-8') as f: f.write('x')\n"
        "    def deactivate(self): pass\n"
    )


async def _settle(host: PluginHost, seconds: float = 0.1) -> None:
    """给事件循环实际运行任务的时间,并在测试结束时回收。"""
    await asyncio.sleep(seconds)
    host.deactivate_all()
    await host.drain_tasks()


# ---------- 能力门禁 ----------


def test_schedule_requires_declared_capability(tmp_path, services):
    """未声明 schedule 却注册任务 → 激活失败,且不留任务(幽灵能力防线)。"""
    body = (
        "class P:\n"
        "    manifest = None\n"
        "    def activate(self, ctx):\n"
        "        ctx.scheduler.every(1.0, lambda: None, name='tick')\n"
        "    def deactivate(self): pass\n"
    )
    _make_plugin_dir(tmp_path, "nocap", body=body, caps=("events",))
    _enable(tmp_path, "nocap")
    host = _activate(tmp_path, services)

    rec = host.records()["nocap"]
    assert rec.state is PluginState.FAILED
    assert "未声明 schedule" in (rec.error or "")
    assert host.job_status("nocap") == ()


def test_schedule_registration_validation(tmp_path):
    """参数非法 / 同名重复必须显式抛错 —— 插件契约错误不应被静默吞掉。"""
    table = TaskTable()
    manifest = PluginManifest(id="p", name="p", capabilities=("schedule",))
    view = SchedulerView(table, "p", manifest, lambda: False)

    with pytest.raises(ValueError, match="正有限数"):
        view.every(0, lambda: None, name="zero")
    with pytest.raises(ValueError, match="正有限数"):
        view.every(float("nan"), lambda: None, name="nan")
    with pytest.raises(ValueError, match="正有限数"):
        view.after(-1.0, lambda: None, name="negative")
    with pytest.raises(TypeError, match="callback"):
        view.every(1.0, 42, name="bad")  # type: ignore[arg-type]

    view.every(1.0, lambda: None, name="ok")
    with pytest.raises(ValueError, match="重复"):
        view.after(1.0, lambda: None, name="ok")
    assert len(view.status()) == 1


# ---------- 周期任务全链 ----------


async def test_schedule_interval_start_stop_resume(tmp_path, services):
    """activate 只登记 → start 物化 → pause 取消 → resume 重启 → shutdown 清空。"""
    marker = tmp_path / "ticks.txt"
    _make_plugin_dir(tmp_path, "ticker", body=_interval_body(marker))
    _enable(tmp_path, "ticker")
    host = _activate(tmp_path, services)

    assert host.job_status("ticker")[0].status is JobStatus.REGISTERED
    assert not marker.exists()  # 未 start 前一次都不跑

    host.start_all()
    assert host.job_status("ticker")[0].status is JobStatus.RUNNING
    await asyncio.sleep(0.1)
    first = len(marker.read_text(encoding="utf-8"))
    assert first >= 2  # run_immediately + 至少一个周期

    host.stop_all()
    assert host.job_status("ticker")[0].status is JobStatus.REGISTERED
    stopped = len(marker.read_text(encoding="utf-8"))
    await asyncio.sleep(0.1)
    assert len(marker.read_text(encoding="utf-8")) == stopped  # 暂停后不再触发

    host.start_all()
    await asyncio.sleep(0.1)
    assert len(marker.read_text(encoding="utf-8")) > stopped  # 恢复后重新跑

    await _settle(host)
    assert host.job_status("ticker") == ()
    after = len(marker.read_text(encoding="utf-8"))
    await asyncio.sleep(0.05)
    assert len(marker.read_text(encoding="utf-8")) == after  # deactivate 后彻底停


async def test_schedule_once_completes_exactly_once(tmp_path, services):
    """单次任务完成后保持 COMPLETED,pause/resume 不重复执行。"""
    marker = tmp_path / "once.txt"
    body = (
        "from pathlib import Path\n"
        f"MARKER = Path({str(marker)!r})\n"
        "class P:\n"
        "    manifest = None\n"
        "    def activate(self, ctx):\n"
        "        ctx.scheduler.after(0.02, self._tick, name='once')\n"
        "    def _tick(self):\n"
        "        MARKER.write_text('x')\n"
        "    def deactivate(self): pass\n"
    )
    _make_plugin_dir(tmp_path, "once", body=body)
    _enable(tmp_path, "once")
    host = _activate(tmp_path, services)

    host.start_all()
    await asyncio.sleep(0.1)
    snap = host.job_status("once")[0]
    assert snap.status is JobStatus.COMPLETED
    assert snap.runs == 1
    assert marker.read_text(encoding="utf-8") == "x"

    host.stop_all()
    host.start_all()
    await asyncio.sleep(0.05)
    assert host.job_status("once")[0].status is JobStatus.COMPLETED
    assert len(marker.read_text(encoding="utf-8")) == 1
    await _settle(host)


# ---------- 失败隔离 ----------


async def test_schedule_callback_exception_isolated_and_recorded(tmp_path, services):
    """坏任务失败只记快照,不拖垮其它插件的任务。"""
    bad_body = (
        "class P:\n"
        "    manifest = None\n"
        "    def activate(self, ctx):\n"
        "        ctx.scheduler.every(0.02, self._boom, name='bad', run_immediately=True)\n"
        "    def _boom(self): raise RuntimeError('task boom')\n"
        "    def deactivate(self): pass\n"
    )
    marker = tmp_path / "good.txt"
    _make_plugin_dir(tmp_path, "bad", body=bad_body)
    _make_plugin_dir(tmp_path, "good", body=_interval_body(marker))
    _enable(tmp_path, "bad", "good")
    host = _activate(tmp_path, services)

    host.start_all()
    await asyncio.sleep(0.1)
    bad = host.job_status("bad")[0]
    good = host.job_status("good")[0]
    assert bad.failures >= 1
    assert bad.last_error == "task boom"
    assert bad.status is JobStatus.RUNNING  # 周期任务失败后继续下一轮
    assert good.failures == 0
    assert good.runs >= 2
    assert marker.exists()
    assert host.records()["bad"].state is PluginState.ACTIVE
    await _settle(host)


async def test_activate_failure_drops_scheduled_jobs(tmp_path, services):
    """activate 中途登记任务后抛错 → FAILED 且不留幽灵 Task。"""
    body = (
        "class P:\n"
        "    manifest = None\n"
        "    def activate(self, ctx):\n"
        "        ctx.scheduler.every(1.0, lambda: None, name='ghost')\n"
        "        raise RuntimeError('activate boom')\n"
        "    def deactivate(self): pass\n"
    )
    _make_plugin_dir(tmp_path, "ghost", body=body)
    _enable(tmp_path, "ghost")
    host = _activate(tmp_path, services)

    assert host.records()["ghost"].state is PluginState.FAILED
    assert host.job_status("ghost") == ()
    assert not [t for t in asyncio.all_tasks() if (t.get_name() or "").startswith("plugin:")]


# ---------- 显式取消 / 热停用 / 同步边界 ----------


async def test_schedule_explicit_cancel_survives_pause_resume(tmp_path, services):
    """插件显式 cancel 的任务不能被 pause/resume 复活。"""
    marker = tmp_path / "ticks.txt"
    _make_plugin_dir(tmp_path, "ticker", body=_interval_body(marker))
    _enable(tmp_path, "ticker")
    host = _activate(tmp_path, services)
    ctx = host.records()["ticker"].context
    assert ctx is not None

    host.start_all()
    await asyncio.sleep(0.05)
    assert ctx.scheduler.cancel("tick") is True
    assert ctx.scheduler.cancel("missing") is False
    stopped = len(marker.read_text(encoding="utf-8"))

    host.stop_all()
    host.start_all()
    await asyncio.sleep(0.1)
    assert len(marker.read_text(encoding="utf-8")) == stopped
    assert host.job_status("ticker")[0].status is JobStatus.CANCELLED
    await _settle(host)


async def test_schedule_hot_disable_drops_jobs(tmp_path, services):
    """管理页热停用后,任务登记与副作用都必须消失。"""
    marker = tmp_path / "ticks.txt"
    _make_plugin_dir(tmp_path, "ticker", body=_interval_body(marker))
    _enable(tmp_path, "ticker")
    host = _activate(tmp_path, services)

    host.start_all()
    await asyncio.sleep(0.05)
    assert host.disable("ticker") is True
    assert host.job_status("ticker") == ()
    stopped = len(marker.read_text(encoding="utf-8"))
    await asyncio.sleep(0.1)
    assert len(marker.read_text(encoding="utf-8")) == stopped
    await host.drain_tasks()


async def test_schedule_register_during_running_starts_immediately(tmp_path, services):
    """插件在 start() 钩子内登记 → 宿主已在运行期,任务立即物化。"""
    marker = tmp_path / "late.txt"
    body = (
        "from pathlib import Path\n"
        f"MARKER = Path({str(marker)!r})\n"
        "class P:\n"
        "    manifest = None\n"
        "    ctx = None\n"
        "    def activate(self, ctx): self.ctx = ctx\n"
        "    def start(self):\n"
        "        self.ctx.scheduler.every(0.02, self._tick, name='late', "
        "run_immediately=True)\n"
        "    def _tick(self): MARKER.write_text('x')\n"
        "    def deactivate(self): pass\n"
    )
    _make_plugin_dir(tmp_path, "late", body=body)
    _enable(tmp_path, "late")
    host = _activate(tmp_path, services)

    host.start_all()
    await asyncio.sleep(0.1)
    assert host.job_status("late")[0].status is JobStatus.RUNNING
    assert marker.read_text(encoding="utf-8") == "x"
    await _settle(host)


def test_schedule_sync_host_calls_without_loop_are_safe(tmp_path, services):
    """同步钩子在无事件循环时只翻转期望状态,不创建 Task、不抛错。"""
    marker = tmp_path / "ticks.txt"
    _make_plugin_dir(tmp_path, "ticker", body=_interval_body(marker))
    _enable(tmp_path, "ticker")
    host = _activate(tmp_path, services)

    host.start_all()  # 同步上下文:无 loop → 不物化
    assert host.job_status("ticker")[0].status is JobStatus.REGISTERED
    host.start_all()
    host.stop_all()
    host.stop_all()
    host.deactivate_all()
    assert host.job_status("ticker") == ()
    assert not marker.exists()


def test_schedule_snapshot_is_readonly(tmp_path, services):
    """快照冻结 —— 诊断数据不能被插件或 UI 反向改写。"""
    _make_plugin_dir(tmp_path, "ticker", body=_interval_body(tmp_path / "ticks.txt"))
    _enable(tmp_path, "ticker")
    host = _activate(tmp_path, services)
    snap = host.job_status("ticker")[0]

    with pytest.raises(FrozenInstanceError):
        snap.status = JobStatus.CANCELLED  # type: ignore[misc]
