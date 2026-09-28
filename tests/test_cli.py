"""CLI 入口 smoke + 子命令路由 + 不加载 Qt 的核心断言。

2026-09-27 refactor 守门测试:
  - `--help` / `sync --help` / `monitor --help` 退出码 0
  - CLI 模式永不加载 PySide6 / qasync / MainWindow(核心架构约束)
  - `__main__` argv 分流正确(子命令走 cli.main,不走 app.run)
  - `bootstrap()` / `shutdown()` 从 `core.runtime` 可独立 import

子命令逻辑(sync / monitor)在 `test_cli_sync.py` / `test_cli_monitor.py`
单独覆盖,本文件只测入口 + 架构断言。
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

PYTHONPATH = "src"


def _run_subprocess(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """子进程跑 CLI 命令,统一加 PYTHONPATH=src。"""
    env = kwargs.pop("env", None)
    base_env = {**os.environ, "PYTHONPATH": PYTHONPATH}
    if env:
        base_env.update(env)
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        env=base_env,
        **kwargs,
    )


# ============================================================
# `--help` smoke
# ============================================================


def test_cli_help_exits_zero() -> None:
    """`python -m tgmonitor --help` 退出码 0,且包含 sync / monitor 子命令。"""
    r = _run_subprocess(["-m", "tgmonitor", "--help"])
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "sync" in r.stdout
    assert "monitor" in r.stdout


def test_cli_sync_help() -> None:
    r = _run_subprocess(["-m", "tgmonitor", "sync", "--help"])
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "--no-metadata" in r.stdout
    assert "--resume" in r.stdout
    assert "--media-policy" in r.stdout


def test_cli_monitor_help() -> None:
    r = _run_subprocess(["-m", "tgmonitor", "monitor", "--help"])
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "--no-resume" in r.stdout


# ============================================================
# 核心架构约束:CLI 模式不加载 PySide6 / qasync / MainWindow
# ============================================================


def test_cli_does_not_import_qt_or_qasync() -> None:
    """CLI 入口子进程断言:导入 cli 包 + 解析 argv 后,PySide6 / qasync /
    tgmonitor.app / MainWindow 都未加载。

    这是 2026-09-27 refactor 的核心契约 — CLI 不能因为模块顶 import 链
    而被迫加载 GUI 框架(冷启动 ~300ms → 1-3s,headless 环境不能有)。

    2026-09-28 扩展:除了 `cli.main`,也直接 import `cli.sync.run_sync` 与
    `cli.monitor.run_monitor`(子命令实现)。原来 `tests/test_cli_sync.py`
    在进程内断言,被 conftest 提前加载的 PySide6 污染失效;改用同一
    subprocess 模式,完整覆盖 cli 包全部模块。
    """
    code = textwrap.dedent("""
        import sys

        # 入口 + 三个子命令模块都触发 import(模拟 CLI 启动后所有代码路径)
        from tgmonitor.cli.main import build_parser
        from tgmonitor.cli.sync import run_sync
        from tgmonitor.cli.monitor import run_monitor

        parser = build_parser()
        parser.parse_args(['sync', '123', '--no-metadata'])

        # 函数引用即触发 — 拿到的是模块本身,如果模块顶 import 了 Qt,
        # 此刻已经泄漏到 sys.modules
        del run_sync, run_monitor, build_parser

        # 关键断言
        leaked = []
        for mod in list(sys.modules):
            if mod == 'PySide6' or mod.startswith('PySide6.'):
                leaked.append(mod)
            if mod == 'qasync':
                leaked.append(mod)
            if mod == 'tgmonitor.app':
                leaked.append(mod)
            if mod == 'tgmonitor.ui.main_window':
                leaked.append(mod)
        if leaked:
            raise AssertionError(f'CLI 不应加载: {leaked}')
    """)
    r = _run_subprocess(["-c", code])
    assert r.returncode == 0, f"stderr={r.stderr}\nstdout={r.stdout}"


def test_cli_dispatch_subcommand_routes_to_cli() -> None:
    """`__main__` 收到 `sync` 时走 `cli.main`,**不**走 `app.run`。

    通过 sentinel:子进程把 `tgmonitor.app` 替换成会在 `run()` 调起时抛
    AssertionError 的占位模块,然后 `__main__.main()` 应不抛 — 证明子
    命令路径根本不 import `tgmonitor.app`。
    """
    code = textwrap.dedent("""
        import sys

        # 占位 app:若被调用证明 CLI 路径错了
        class FakeApp:
            @staticmethod
            def run():
                raise AssertionError('GUI 模式被错误触发')

        sys.modules['tgmonitor.app'] = FakeApp
        sys.argv = ['tgmonitor', 'sync', '--help']

        from tgmonitor.__main__ import main

        try:
            rc = main()
        except SystemExit as e:
            # argparse --help 走 SystemExit(0)
            rc = e.code if isinstance(e.code, int) else 0
        if rc != 0:
            raise AssertionError(f'__main__ 返非零退出码 {rc}')
    """)
    r = _run_subprocess(["-c", code])
    assert r.returncode == 0, f"stderr={r.stderr}\nstdout={r.stdout}"


def test_cli_dispatch_default_routes_to_gui() -> None:
    """`__main__` 无参数 → 走 GUI 路径(尝试 import `tgmonitor.app`)。

    不真正调起 GUI(没 QApplication / 显示设备),只验证 argv 分流边界 —
    当占位 app 的 `run()` 抛 RuntimeError("GUI not available") 时,
    `__main__` 应捕获并返非零退出码(默认错误处理),证明 GUI 路径被触达。
    """
    code = textwrap.dedent("""
        import sys

        class FakeApp:
            @staticmethod
            def run():
                raise RuntimeError("GUI not available in test")

        sys.modules['tgmonitor.app'] = FakeApp
        sys.argv = ['tgmonitor']  # 无参数 → 默认 GUI

        from tgmonitor.__main__ import main

        rc = main()
        # 默认错误处理:RuntimeError 走 except → 返 1
        if rc != 1:
            raise AssertionError(f'期望退出码 1(GUI 路径错误捕获),实际 {rc}')
    """)
    r = _run_subprocess(["-c", code])
    assert r.returncode == 0, f"stderr={r.stderr}\nstdout={r.stdout}"


# ============================================================
# composition root 可独立 import(GUI / CLI 共享)
# ============================================================


def test_core_runtime_bootstrap_importable() -> None:
    """`tgmonitor.core.runtime:bootstrap` 与 `shutdown` 可独立 import —
    CLI 子命令的契约,GUI / CLI 共用 composition root。

    注:不调 bootstrap()(会触发 storage / objectstore connect,需要真环境);
    只测 import 路径。
    """
    from tgmonitor.core.runtime import bootstrap, shutdown

    assert callable(bootstrap)
    assert callable(shutdown)
