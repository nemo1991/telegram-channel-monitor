"""插件定时任务表 — 宿主统一登记、启停、取消与回收。

设计动机(ADR-001 决策 5):插件声明 `schedule` 后不应自己 `create_task()` 撒手不管
—— 那样 pause / shutdown 之后仍可能有任务在写库、占连接。宿主提供一张按
`plugin_id` 分区的任务表:插件只登记「做什么、多久一次」,何时跑、何时停、退出
时如何取消都由宿主决定。

**同步钩子 / 异步任务的边界**:宿主的 `start_all()` / `stop_all()` 是同步方法
(由 AppService 的生命周期钩子调用),而任务本身是 asyncio 协程。任务表只在
**有运行中的事件循环**时才物化 Task;纯同步调用(单测 / 循环尚未启动)只翻转
期望状态,不创建任务、不抛错。这样阶段 1~4 的同步测试与 CLI/GUI 的真实运行
路径共用同一套语义,不需要改宿主方法签名。

失败隔离:任务回调试抛异常只记日志并计入快照,不冒泡、不影响其它任务;被取消
的任务统一 `cancel()` + 有界 `gather()` 回收,超时只告警不卡死 shutdown。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from tgmonitor.plugins.manifest import PluginManifest

log = logging.getLogger(__name__)

# 任务回调:async callable 直接 await;同步 callable 也接受(结果不是 awaitable
# 就当已完成)。同步回调运行在事件循环线程 —— 插件作者需自行避免阻塞 IO。
TaskCallback = Callable[[], Awaitable[Any] | Any]

# 取消回收的单次上界 —— 与 AppService.shutdown 的 2s 阶段超时对齐。
DRAIN_TIMEOUT = 2.0


class JobKind(str, Enum):
    """调度语义。"""

    INTERVAL = "interval"  # 周期执行
    ONCE = "once"  # 延时执行一次


class JobStatus(str, Enum):
    """任务当前状态 — 快照只读,插件可据此做诊断。"""

    REGISTERED = "registered"  # 已登记,等宿主 start
    RUNNING = "running"  # 已物化 Task(周期任务;单次任务执行中也在此态)
    COMPLETED = "completed"  # 单次任务成功完成
    FAILED = "failed"  # 单次任务回调抛错
    CANCELLED = "cancelled"  # 插件显式取消,不再随 pause/resume 重启


@dataclass(frozen=True)
class JobSpec:
    """一次登记的不变描述。"""

    name: str
    callback: TaskCallback
    kind: JobKind
    seconds: float
    run_immediately: bool = False


@dataclass(frozen=True)
class JobSnapshot:
    """任务状态快照 — 只读,供诊断 / 管理页读取。"""

    name: str
    kind: JobKind
    status: JobStatus
    runs: int
    failures: int
    last_error: str | None
    last_run: float | None  # time.monotonic(),仅用于相对诊断


@dataclass
class _JobEntry:
    spec: JobSpec
    status: JobStatus = JobStatus.REGISTERED
    runs: int = 0
    failures: int = 0
    last_error: str | None = None
    last_run: float | None = None
    task: asyncio.Task[None] | None = None
    # 区分「pause 导致的临时取消」与「插件显式 cancel」—— 前者 resume 要重启,
    # 后者必须保持 CANCELLED,否则 pause/resume 会让已取消的任务复活。
    explicit_cancel: bool = False

    def snapshot(self) -> JobSnapshot:
        return JobSnapshot(
            name=self.spec.name,
            kind=self.spec.kind,
            status=self.status,
            runs=self.runs,
            failures=self.failures,
            last_error=self.last_error,
            last_run=self.last_run,
        )


class TaskTable:
    """宿主侧任务表 — 按 plugin_id 分区,不暴露 Task 本体给插件。"""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, _JobEntry]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reapers: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # 登记 / 启停
    # ------------------------------------------------------------------

    def register(self, plugin_id: str, spec: JobSpec) -> None:
        """登记一个任务。参数非法 / 同名重复抛错 —— 这是插件契约错误,应当可见。"""
        if not spec.name:
            raise ValueError("定时任务 name 不能为空")
        if not math.isfinite(spec.seconds) or spec.seconds <= 0:
            raise ValueError(f"定时任务 {spec.name} 的 seconds 必须是正有限数")
        if not callable(spec.callback):
            raise TypeError(f"定时任务 {spec.name} 的 callback 必须可调用")
        jobs = self._jobs.setdefault(plugin_id, {})
        if spec.name in jobs:
            raise ValueError(f"插件 {plugin_id} 已登记定时任务 {spec.name},名字不能重复")
        jobs[spec.name] = _JobEntry(spec=spec)

    def start_plugin(self, plugin_id: str) -> int:
        """物化该插件所有 REGISTERED 任务,返回本次新建的 Task 数。幂等。"""
        loop = self._current_loop()
        if loop is None:
            return 0
        spawned = 0
        for entry in self._jobs.get(plugin_id, {}).values():
            if entry.task is not None or entry.status is not JobStatus.REGISTERED:
                continue
            entry.status = JobStatus.RUNNING
            coro = (
                self._run_interval(plugin_id, entry)
                if entry.spec.kind is JobKind.INTERVAL
                else self._run_once(plugin_id, entry)
            )
            entry.task = loop.create_task(coro, name=f"plugin:{plugin_id}:{entry.spec.name}")
            spawned += 1
        return spawned

    def stop_plugin(self, plugin_id: str) -> int:
        """取消该插件全部 Task,状态回到「可重启」(显式取消除外)。幂等。"""
        entries = self._jobs.get(plugin_id, {})
        tasks = [entry.task for entry in entries.values() if entry.task is not None]
        for entry in entries.values():
            entry.task = None
            if entry.explicit_cancel:
                entry.status = JobStatus.CANCELLED
            elif entry.status in (JobStatus.RUNNING, JobStatus.FAILED):
                entry.status = JobStatus.REGISTERED
        for task in tasks:
            task.cancel()
        if tasks:
            self._schedule_reap(tasks)
        return len(tasks)

    def drop_plugin(self, plugin_id: str) -> int:
        """停用 / 激活失败时移除该插件全部登记,返回移除数量。"""
        self.stop_plugin(plugin_id)
        return len(self._jobs.pop(plugin_id, {}))

    def cancel_one(self, plugin_id: str, name: str) -> bool:
        """插件显式取消单个任务 —— 该取消跨 pause/resume 持久。"""
        entry = self._jobs.get(plugin_id, {}).get(name)
        if entry is None:
            return False
        entry.explicit_cancel = True
        entry.status = JobStatus.CANCELLED
        task = entry.task
        entry.task = None
        if task is not None:
            task.cancel()
            self._schedule_reap([task])
        return True

    def snapshot(self, plugin_id: str) -> tuple[JobSnapshot, ...]:
        """只读状态快照 — 不暴露 Task / callback,调用方改不动内部记账。"""
        return tuple(entry.snapshot() for entry in self._jobs.get(plugin_id, {}).values())

    async def drain(self) -> None:
        """等待所有取消回收结束(有界超时)—— shutdown 在关后端前 await 一次。"""
        reapers = list(self._reapers)
        if not reapers:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*reapers, return_exceptions=True), timeout=DRAIN_TIMEOUT
            )
        except TimeoutError:
            log.warning("插件任务回收整体超时(%.1fs),可能有未回收任务", DRAIN_TIMEOUT)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _current_loop(self) -> asyncio.AbstractEventLoop | None:
        """优先用当前运行循环;同步上下文回退到上次捕获的循环;都没有则不物化。"""
        loop: asyncio.AbstractEventLoop | None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = self._loop if self._loop is not None and not self._loop.is_closed() else None
        else:
            self._loop = loop
        return loop

    def _schedule_reap(self, tasks: list[asyncio.Task[None]]) -> None:
        loop = self._current_loop()
        if loop is None:
            return
        reaper = loop.create_task(self._reap(tasks), name="plugin-task-reap")
        self._reapers.add(reaper)
        reaper.add_done_callback(self._reapers.discard)

    async def _reap(self, tasks: list[asyncio.Task[None]]) -> None:
        """被取消的任务需要被 await 才算真正回收 —— gather 吞掉 CancelledError。"""
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=DRAIN_TIMEOUT
            )
        except TimeoutError:
            log.warning("插件任务取消超时(%.1fs),可能有任务未响应 cancel", DRAIN_TIMEOUT)

    async def _run_interval(self, plugin_id: str, entry: _JobEntry) -> None:
        if entry.spec.run_immediately:
            await self._invoke(plugin_id, entry)
        while True:
            await asyncio.sleep(entry.spec.seconds)
            await self._invoke(plugin_id, entry)

    async def _run_once(self, plugin_id: str, entry: _JobEntry) -> None:
        await asyncio.sleep(entry.spec.seconds)
        ok = await self._invoke(plugin_id, entry)
        entry.status = JobStatus.COMPLETED if ok else JobStatus.FAILED

    async def _invoke(self, plugin_id: str, entry: _JobEntry) -> bool:
        entry.last_run = time.monotonic()
        try:
            result = entry.spec.callback()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            entry.failures += 1
            entry.last_error = str(exc)
            # 完整栈对插件作者排错有价值;周期任务的失败计数留在快照里。
            log.exception("插件 %s 定时任务 %s 执行失败", plugin_id, entry.spec.name)
            return False
        entry.runs += 1
        entry.last_error = None
        return True


class SchedulerView:
    """插件侧定时任务门面 — 只能登记 / 取消**自己**的任务。

    刻意不放进 `PluginApi`:那是域服务写操作的窄门面,调度是另一类宿主能力,
    与 `EventBusView` 同构独立成视图。`PluginContext` 字段表面由
    `test_plugin_api_surface.py` 守护,新增字段必须显式改 allowlist。
    """

    def __init__(
        self,
        table: TaskTable,
        plugin_id: str,
        manifest: PluginManifest,
        is_running: Callable[[], bool],
    ) -> None:
        self._table = table
        self._plugin_id = plugin_id
        self._declared = "schedule" in manifest.capabilities
        self._is_running = is_running

    def every(
        self,
        seconds: float,
        callback: TaskCallback,
        *,
        name: str,
        run_immediately: bool = False,
    ) -> None:
        """登记周期任务。`run_immediately=True` 时 start 后先跑一次再等间隔。"""
        self._register(JobSpec(name, callback, JobKind.INTERVAL, seconds, run_immediately))

    def after(self, seconds: float, callback: TaskCallback, *, name: str) -> None:
        """登记延时一次性任务;完成 / 失败状态可从 `status()` 读。"""
        self._register(JobSpec(name, callback, JobKind.ONCE, seconds))

    def cancel(self, name: str) -> bool:
        """显式取消一个任务;跨 pause/resume 不再重启。返回是否命中。"""
        return self._table.cancel_one(self._plugin_id, name)

    def status(self) -> tuple[JobSnapshot, ...]:
        """本插件的任务状态快照。"""
        return self._table.snapshot(self._plugin_id)

    def _register(self, spec: JobSpec) -> None:
        self._guard()
        self._table.register(self._plugin_id, spec)
        # 宿主已在运行期(如插件 start() 里登记)→ 立即物化,不必等下一次 start_all。
        if self._is_running():
            self._table.start_plugin(self._plugin_id)

    def _guard(self) -> None:
        if not self._declared:
            raise PermissionError(f"插件 {self._plugin_id} 未声明 schedule 能力,禁止注册定时任务")


__all__ = [
    "DRAIN_TIMEOUT",
    "JobKind",
    "JobSnapshot",
    "JobSpec",
    "JobStatus",
    "SchedulerView",
    "TaskCallback",
    "TaskTable",
]
