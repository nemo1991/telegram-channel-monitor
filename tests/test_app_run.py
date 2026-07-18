"""`app.run()` 的回归测试,聚焦事件循环生命周期。

不像 TDLib / UI 组件有清晰接口可以 mock,`run()` 是端到端入口(必须 Qt
event loop + qasync 跑起来才能验)。所以这一组做的是"源码结构":

  - 不能再用 `loop.run_until_complete(...)` 然后 `loop.run_forever()` —
    中间窗口里 qasync 的 `__is_running` 被 qasync 自己改 False,
    aiotdlib thread IO 在这段时间 wake asyncio Task 就会撞
    「RuntimeError: loop ... is not the running loop」(2026-07-18 08:00 实测)。
  - setup 必须用 `asyncio.ensure_future(..., loop=loop)` 单 loop 持续 running。

这是 **结构性** 测试。如果有人重构 `run()` 改回老模式,这几个断言会失败。
不强引用具体任务名 — `run_forever` / `qasync.QEventLoop` 仍允许出现。
"""
from __future__ import annotations

import ast
import inspect
import re

import pytest

import tgmonitor.app as app_module


def _run_body_without_docstring() -> str:
    """走 AST 拿 app.run() 的 FunctionDef 节点 — 避开 docstring 里也写
    `run_until_complete` 这种 false-positive。
    """
    source = inspect.getsource(app_module)
    tree = ast.parse(source)
    run_node: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            run_node = node
            break
    assert run_node is not None, "app.run() not found via AST"
    # 文档字符串是第一条 stmt 且是 Expr(Constant);临时把它替换成
    # `pass`,再用 `ast.unparse` 把整个 function dump 出来 —
    # 这样测试对 docstring 里的 `run_until_complete` 描述免疫
    if (
        run_node.body
        and isinstance(run_node.body[0], ast.Expr)
        and isinstance(run_node.body[0].value, ast.Constant)
    ):
        saved = run_node.body[0]
        run_node.body[0] = ast.Pass()
        try:
            return ast.unparse(run_node)
        finally:
            run_node.body[0] = saved
    return ast.unparse(run_node)


@pytest.fixture(scope="module")
def run_body() -> str:
    return _run_body_without_docstring()


def test_no_run_until_complete_in_app_run() -> None:
    """`run()` 不能调 `loop.run_until_complete(...)` — 这正是 qasync
    paused 窗口的源头。

    走 AST 看 Call 节点,而不是裸字符串匹配(否则 docstring / 注释里解释
    "为什么不用" 也会被误判)。
    """
    source = inspect.getsource(app_module)
    tree = ast.parse(source)
    for func_node in ast.walk(tree):
        if not (isinstance(func_node, ast.FunctionDef) and func_node.name == "run"):
            continue
        for sub in ast.walk(func_node):
            if isinstance(sub, ast.Call):
                callee = sub.func
                # 方法调用 `loop.run_until_complete(...)`
                if isinstance(callee, ast.Attribute) and callee.attr == "run_until_complete":
                        raise AssertionError(
                            "app.run() uses loop.run_until_complete(...) — "
                            "qasync pauses the loop between run_until_complete "
                            "and run_forever, causing "
                            "`RuntimeError: loop ... is not the running loop` "
                            "from aiotdlib task wakeups. Use ensure_future(..., "
                            "loop=loop) and a single run_forever() instead."
                        )
        # 只查顶层 run 函数,嵌套 def 不会被这个 break 提前退出
        break


def test_setup_is_scheduled_as_future_on_loop(run_body: str) -> None:
    """setup 协程必须以 `asyncio.ensure_future(..., loop=loop)` 形式调度
    到 qasync 的 loop 上;它会跟 Qt 事件 tick 交错执行,不在外面 `await` 它。
    """
    matches = re.findall(
        r"ensure_future\(([^)]+(?:\([^)]*\)[^)]*)*),\s*loop=loop\)",
        run_body,
    )
    assert matches, (
        "app.run() should schedule setup with "
        "`asyncio.ensure_future(_setup_then_show(), loop=loop)` — single "
        "loop stays running, no paused window between setup and main loop.\n"
        f"Function body:\n{run_body}"
    )


def test_main_window_is_constructed_after_services_ready(run_body: str) -> None:
    """MainWindow 构造必须在 `_bootstrap` + `app.bootstrap` 之后,这样
    UI 不会错过启动期发出来的 LoginStateChanged 事件。
    """
    bootstrap_idx = run_body.find("await _bootstrap()")
    bootstrap_app_idx = run_body.find("await app_svc.bootstrap()")
    main_window_idx = run_body.find("MainWindow(")
    assert bootstrap_idx >= 0
    assert bootstrap_app_idx >= 0
    assert main_window_idx >= 0
    assert bootstrap_idx < main_window_idx, (
        "MainWindow 必须在 _bootstrap 之后构造,否则事件总线上的 service "
        "还没起来就被 wire 进 UI。"
    )
    assert bootstrap_app_idx < main_window_idx, (
        "MainWindow 必须在 app.bootstrap 之后构造,否则启动期的 "
        "LoginStateChanged 事件被 UI 错过。"
    )


def test_run_forever_pattern_uses_with_loop(run_body: str) -> None:
    """最后一行必须是 `with loop: loop.run_forever()`,由 `with` 退出钩子
    负责 close loop。
    """
    assert "with loop:" in run_body, (
        "app.run() should end with `with loop: loop.run_forever()` — "
        "the `with` ctx-manager closes the loop on exit, preventing "
        "'Event loop is closed' leaks."
    )
    assert "loop.run_forever()" in run_body

    def test_no_run_until_complete_in_app_run(self) -> None:
        """`run()` 不能用 `loop.run_until_complete(...)` — 这正是 qasync
        paused 窗口的源头。

        如果有人切回老的 `run_until_complete(_setup_async)` 后再 `run_forever`,
        立即会冒 `RuntimeError: loop ... is not the running loop`。
        """
        # 提取 `run()` 函数体做检查,避免误伤 module 其它地方
        match = re.search(
            r"def run\(\).*?(?=^def |\Z)",
            self.source,
            re.MULTILINE | re.DOTALL,
        )
        assert match, "run() function not found in app.py"
        run_body = match.group(0)
        assert "run_until_complete" not in run_body, (
            "app.run() uses loop.run_until_complete(...) — qasync pauses the "
            "loop between run_until_complete and run_forever, causing "
            "`RuntimeError: loop ... is not the running loop` from aiotdlib "
            "task wakeups. Use ensure_future(..., loop=loop) and a single "
            "run_forever() instead."
        )

    def test_setup_is_scheduled_as_future_on_loop(self) -> None:
        """setup 协程必须以 `asyncio.ensure_future(..., loop=loop)` 形式调度
        到 qasync 的 loop 上;它会跟 Qt 事件 tick 交错执行,不在外面 `await` 它。
        """
        match = re.search(
            r"def run\(\).*?(?=^def |\Z)",
            self.source,
            re.MULTILINE | re.DOTALL,
        )
        assert match, "run() function not found in app.py"
        run_body = match.group(0)
        # 必须看到 ensure_future + loop= 的形式
        assert re.search(
            r"asyncio\.ensure_future\(_setup_then_show\(\)[^)]*\)[^,)]*,[^)]*loop=loop",
            run_body,
        ), (
            "app.run() should schedule setup with "
            "`asyncio.ensure_future(_setup_then_show(), loop=loop)` — single "
            "loop stays running, no paused window between setup and main loop."
        )

    def test_main_window_is_constructed_after_services_ready(self) -> None:
        """MainWindow 构造必须在 `_bootstrap` + `app.bootstrap` 之后,这样
        UI 不会错过启动期发出来的 LoginStateChanged 事件。
        """
        run_match = re.search(
            r"def run\(\).*?(?=^def |\Z)",
            self.source,
            re.MULTILINE | re.DOTALL,
        )
        assert run_match, "run() function not found"
        run_body = run_match.group(0)

        setup_match = re.search(
            r"async def _setup_then_show.*?(?=    async def _shutdown|\Z)",
            run_body,
            re.DOTALL,
        )
        assert setup_match, "_setup_then_show not found"
        setup_body = setup_match.group(0)

        bootstrap_idx = setup_body.find("await _bootstrap()")
        bootstrap_app_idx = setup_body.find("await app_svc.bootstrap()")
        main_window_idx = setup_body.find("MainWindow(")
        assert bootstrap_idx >= 0
        assert bootstrap_app_idx >= 0
        assert main_window_idx >= 0
        assert bootstrap_idx < main_window_idx, (
            "MainWindow 必须在 _bootstrap 之后构造,否则事件总线上的 service "
            "还没起来就被 wire 进 UI。"
        )
        assert bootstrap_app_idx < main_window_idx, (
            "MainWindow 必须在 app.bootstrap 之后构造,否则启动期的 "
            "LoginStateChanged 事件被 UI 错过。"
        )

    def test_run_forever_pattern_uses_with_loop(self) -> None:
        """最后一行必须是 `with loop: loop.run_forever()`,由 `with` 退出钩子
        负责 close loop。
        """
        run_match = re.search(
            r"def run\(\).*?(?=^def |\Z)",
            self.source,
            re.MULTILINE | re.DOTALL,
        )
        assert run_match
        run_body = run_match.group(0)
        assert "with loop:" in run_body, (
            "app.run() should end with `with loop: loop.run_forever()` — "
            "the `with` ctx-manager closes the loop on exit, preventing "
            "'Event loop is closed' leaks."
        )
        assert "loop.run_forever()" in run_body
