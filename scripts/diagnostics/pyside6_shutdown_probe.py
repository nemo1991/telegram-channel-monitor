"""PySide6 6.12 shutdown SIGSEGV 归因探针（诊断工具，不进默认 pytest 收集）。

背景：PR #35 实测 6.12.0 全套连跑 7 次有 4 次随机 SIGSEGV / Bus error，崩溃点
散布在 17% / 22% / 98% / shutdown。当时没有 C 层堆栈、没有分组实验，因此
「只是测试写法问题」始终只是推断。本脚本用来把「测试侧」与「生产侧」分开。

设计要点：
- **每次实验都是独立子进程**：SIGSEGV 是进程级崩溃，父进程必须存活才能统计。
- **worker 完全不跑 pytest、不 import conftest**：只创建 Qt 对象后让解释器自然
  退出。这样崩溃点必然落在 Qt / Shiboken 对象析构，而不是 pytest fixture 逻辑。
- 三档逐层加复杂度，用来定位是哪一层引入崩溃：
    bare        纯 Qt 基线：QApplication + 空 QMainWindow
    mainwindow  生产对象图：真 MainWindow（假后端）+ 生产 closeEvent + 干净清理
    threaded    生产对象图 + 后台 loop 跨线程调度 + **故意不清理**（模拟测试残留）
- 开启 faulthandler：崩溃时 stderr 会打印 Python/C 调用栈。

用法（需在装了 PySide6 6.12 的隔离环境里跑，勿在主工作区 .venv 上跑）：

    PYTHONPATH=src QT_QPA_PLATFORM=offscreen \
        python scripts/diagnostics/pyside6_shutdown_probe.py --tier mainwindow --repeat 20

退出码：全部子进程 rc=0 返回 0；出现任何非 0 返回 1（便于脚本化判断）。
Unix 下 rc=-11 是 SIGSEGV、rc=-10 是 SIGBUS、rc=-6 是 SIGABRT。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
TIERS = ("bare", "mainwindow", "threaded")


def _build_services(base: Path):
    """装配生产 AppService / MonitorService（真对象图，存储/客户端用测试替身）。

    复用 `tests/test_main_window_channels.py::_build_setup` 的构造顺序，避免在
    诊断脚本里重写一套装配逻辑而引入与生产不一致的差异。
    """
    from tests.fixtures._in_memory_repository import InMemoryRepository
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.config import Settings
    from tgmonitor.core.events import EventBus
    from tgmonitor.core.monitor.service import MonitorService
    from tgmonitor.core.objectstore.local_store import LocalObjectStore
    from tgmonitor.core.telegram.fake_client import FakeTelegramClient

    async def _go():
        storage = InMemoryRepository()
        objects = LocalObjectStore(root=base / "media")
        await storage.connect()
        await objects.connect()
        bus = EventBus()
        client = FakeTelegramClient()
        settings = Settings.for_test(
            phone="+8612345",
            session_dir=base / "s",
            db_root=base / "m",
            objectstore_root=base / "o",
        )
        monitor = MonitorService(bus, client, storage, objects, settings)
        app_svc = AppService(bus, client, storage, objects, settings)
        return app_svc, monitor

    return _go()


def _worker(tier: str) -> None:
    """单次实验：创建 Qt 对象 → 关闭 → 让解释器自然退出（不显式 quit / deleteLater）。"""
    import faulthandler

    faulthandler.enable()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    if tier == "bare":
        from PySide6.QtWidgets import QMainWindow

        win = QMainWindow()
        win.show()
        app.processEvents()
        win.close()
        app.processEvents()
        return

    import asyncio

    from tests.fixtures._loop_thread import LoopThread
    from tgmonitor.ui.main_window import MainWindow

    lt = LoopThread()
    base = Path(tempfile.mkdtemp(prefix="tgmonitor-probe-"))
    fut = asyncio.run_coroutine_threadsafe(_build_services(base), lt.loop)
    app_svc, monitor = fut.result(timeout=15.0)

    win = MainWindow(app_svc, monitor, lt.loop, env_path=None, objects_error=None)

    async def _shutdown_cb() -> None:
        await asyncio.sleep(0)

    # 走生产退出路径：closeEvent → run_shutdown_coro_sync → 泵 Qt 事件等协程。
    win.set_shutdown_callback(_shutdown_cb)
    win._truly_quit = True
    win._tray = None
    win.show()
    app.processEvents()

    if tier == "threaded":
        # 跨线程调度一次协程，制造 qloop 与 Qt 主线程的交互。
        async def _noop() -> None:
            await asyncio.sleep(0)

        f = asyncio.run_coroutine_threadsafe(_noop(), lt.loop)
        f.result(timeout=5.0)

    win.close()
    app.processEvents()

    if tier == "mainwindow":
        lt.stop()  # 干净清理：崩溃只可能来自 Qt 对象析构
    # threaded 档故意不 stop，模拟测试里 qloop / widget 残留到解释器退出。


def _run_parent(tier: str, repeat: int) -> int:
    """父进程：跑 repeat 次独立子进程，统计退出码并保留崩溃堆栈样本。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{_ROOT}{os.pathsep}{_ROOT / 'src'}"
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_LOGGING_RULES"] = "*.warning=false"
    env["PYTHONFAULTHANDLER"] = "1"

    results: Counter[int] = Counter()
    crash_samples: list[str] = []
    for i in range(1, repeat + 1):
        proc = subprocess.run(
            [
                sys.executable,
                "-X",
                "faulthandler",
                str(Path(__file__).resolve()),
                "--worker",
                tier,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )
        rc = proc.returncode
        results[rc] += 1
        marker = "OK" if rc == 0 else f"FAIL(rc={rc})"
        print(f"[{i:02d}/{repeat}] {tier:<10} {marker}", flush=True)
        if rc != 0 and len(crash_samples) < 3:
            tail = "\n".join(proc.stderr.strip().splitlines()[-30:])
            crash_samples.append(f"--- run {i} rc={rc} ---\n{tail}")

    print("\n=== 汇总 ===")
    for rc, count in sorted(results.items()):
        print(f"  rc={rc}: {count}/{repeat}")
    print(f"  档位={tier}  崩溃次数={repeat - results.get(0, 0)}")
    if crash_samples:
        print("\n=== 崩溃样本（stderr 末 30 行） ===")
        for sample in crash_samples:
            print(sample)
            print()
    return 0 if results.get(0, 0) == repeat else 1


def main() -> int:
    """解析参数，分派 worker / parent 两种运行模式。"""
    parser = argparse.ArgumentParser(description="PySide6 6.12 shutdown SIGSEGV 归因探针")
    parser.add_argument("--worker", choices=TIERS, help="内部使用：单次实验的子进程")
    parser.add_argument("--tier", choices=TIERS, default="mainwindow", help="实验档位")
    parser.add_argument("--repeat", type=int, default=20, help="重复次数（独立子进程）")
    args = parser.parse_args()

    if args.worker:
        _worker(args.worker)
        return 0
    return _run_parent(args.tier, args.repeat)


if __name__ == "__main__":
    raise SystemExit(main())
