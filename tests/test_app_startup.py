"""`app.py` 启动拆分的回归测试 — 2026-09-25 v1.8.x。

`_setup_then_show` 把 `monitor.start()` + `app.bootstrap()` 拆到 `_background_startup`
后台 task,`win.show()` 必须先于它们执行,这样双击 app 时主窗口立即可见,不
等 libtdjson 启动(冷启动 / 断网可达 5-10s)。

测试策略:静态分析 `app.py` 源码(与 `test_app_run.py` 同模式)— 验:
1. `_background_startup` 函数存在
2. `_setup_then_show` 里 `win.show()` 在 `_background_startup` 调用之前
3. `_setup_then_show` 不直接 `await monitor.start()` 或 `await app.bootstrap()`
4. `_background_startup` 顺序执行 list_subbed → set_whitelist → monitor.start → app.bootstrap
5. `_background_startup` 出错时 publish ErrorOccurred 事件

为何不用运行时测:`_setup_then_show` 强依赖 qasync + Qt + 真 storage 装配,
构造 fixture 太重;静态分析足以锁住结构,且未来重构时易追踪。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_PY = ROOT / "src" / "tgmonitor" / "app.py"


@pytest.fixture(scope="module")
def app_source() -> str:
    return APP_PY.read_text(encoding="utf-8")


def test_background_startup_function_defined(app_source: str) -> None:
    """`_background_startup` 必须存在,且是 async 函数。"""
    m = re.search(
        r"async\s+def\s+_background_startup\s*\([^)]*\)\s*->\s*[^:]+:",
        app_source,
    )
    assert m is not None, (
        "_background_startup 函数未找到 — 启动拆分被还原了?\n源码片段:\n" + app_source[:200]
    )


def test_setup_then_show_calls_background_startup(app_source: str) -> None:
    """`_setup_then_show` 必须 `asyncio.create_task(_background_startup(...))`。"""
    # 找 _setup_then_show 函数体
    body = _extract_function_body(app_source, "_setup_then_show")
    assert body is not None, "_setup_then_show 函数未找到"
    assert "asyncio.create_task(_background_startup" in body, (
        "_setup_then_show 未把后台启动拆到 create_task — win.show() 又被阻塞了\n"
        f"body 头 500 字符:\n{body[:500]}"
    )


def test_window_show_before_background_startup(app_source: str) -> None:
    """`win.show()` 必须在 `_background_startup` 调度之前 — 否则窗口还是被
    阻塞到 bootstrap 完成后才出现。

    用字符位置比 assert:`win.show()` 行的 index < `create_task(_background_startup` 行。
    """
    body = _extract_function_body(app_source, "_setup_then_show")
    assert body is not None

    # 找 `win.show()` 行
    show_match = re.search(r"win\.show\(\)", body)
    assert show_match is not None, "_setup_then_show 缺 win.show()"

    # 找 `_background_startup` 调度行
    bg_match = re.search(r"asyncio\.create_task\(\s*_background_startup\b", body)
    assert bg_match is not None, "_setup_then_show 缺 asyncio.create_task(_background_startup(...))"

    assert show_match.start() < bg_match.start(), (
        f"win.show() 必须在 _background_startup 之前;实际:\n"
        f"  win.show() 位置 {show_match.start()}\n"
        f"  _background_startup 位置 {bg_match.start()}"
    )


def test_setup_then_show_does_not_directly_await_monitor_start(
    app_source: str,
) -> None:
    """`_setup_then_show` 不能直接 `await monitor.start()` — 必须 defer 到
    `_background_startup`。否则就回到老的阻塞流程。
    """
    body = _extract_function_body(app_source, "_setup_then_show")
    assert body is not None
    assert "await monitor.start()" not in body, (
        "_setup_then_show 直接 await monitor.start() — 启动又被阻塞了"
    )


def test_setup_then_show_does_not_directly_await_bootstrap(
    app_source: str,
) -> None:
    """`_setup_then_show` 不能直接 `await app_svc.bootstrap()` / `await app.bootstrap()` —
    同上,必须 defer。"""
    body = _extract_function_body(app_source, "_setup_then_show")
    assert body is not None
    assert "await app_svc.bootstrap()" not in body
    assert "await app.bootstrap()" not in body


def test_background_startup_runs_steps_in_order(app_source: str) -> None:
    """`_background_startup` 必须按顺序执行:
    list_subscribed_channels → monitor.set_whitelist → monitor.start → app.bootstrap。
    顺序乱了会导致白名单未建好 monitor 就开始收消息。
    """
    body = _extract_function_body(app_source, "_background_startup")
    assert body is not None, "_background_startup 函数体未找到"

    # 提取关键调用位置
    steps = {
        "list_subscribed": re.search(r"list_subscribed_channels\(\)", body),
        "set_whitelist": re.search(r"monitor\.set_whitelist\(", body),
        "monitor_start": re.search(r"await\s+monitor\.start\(\)", body),
        "app_bootstrap": re.search(r"app_svc\.bootstrap\(\)", body),
    }
    for name, m in steps.items():
        assert m is not None, f"_background_startup 缺 {name} 调用"

    positions = [(name, m.start()) for name, m in steps.items()]
    positions.sort(key=lambda x: x[1])
    sorted_names = [p[0] for p in positions]
    assert sorted_names == [
        "list_subscribed",
        "set_whitelist",
        "monitor_start",
        "app_bootstrap",
    ], (
        f"_background_startup 步骤顺序错 — 实际 {sorted_names},"
        f"应为 ['list_subscribed', 'set_whitelist', 'monitor_start', 'app_bootstrap']"
    )


def test_background_startup_handles_paused(app_source: str) -> None:
    """`_background_startup` 必须处理 `app.is_paused` 分支 — 用户启动即暂停
    时跳过 libtdlib 启动,client 保持 uninit。
    """
    body = _extract_function_body(app_source, "_background_startup")
    assert body is not None
    assert "is_paused" in body, "_background_startup 缺 is_paused 检查 — 启动即暂停场景未处理"


def test_background_startup_publishes_error_on_failure(app_source: str) -> None:
    """`_background_startup` 出错必须 publish `ErrorOccurred` 事件 —
    这样 bus subscriber(已有 _on_bus_auth_error 等)能弹窗 + 计数。
    """
    body = _extract_function_body(app_source, "_background_startup")
    assert body is not None
    assert "ErrorOccurred" in body, "_background_startup 缺 ErrorOccurred 事件发布 — 错误路径断了"
    # 必须有 except 块
    assert "except" in body, "_background_startup 缺 except 兜底"


def test_background_startup_updates_activity_label(app_source: str) -> None:
    """`_background_startup` 每步必须调 `win._show_activity(...)` —
    否则状态栏左侧看不到启动进度。
    """
    body = _extract_function_body(app_source, "_background_startup")
    assert body is not None
    activity_calls = re.findall(r"win\._show_activity\(", body)
    # 至少 3 处:加载白名单 / 启动 monitor / bootstrap(或 paused 提示)
    assert len(activity_calls) >= 3, (
        f"_background_startup 应至少调 3 次 win._show_activity(...),实际 {len(activity_calls)} 次"
    )


# ---- helper ----


def _extract_function_body(source: str, name: str) -> str | None:
    """简单 Python 解析 — 找 `def NAME(...)` 到下一个 `def` / `class` / 同级缩进的
    语句块为止。够用,因为 app.py 里都是顶层函数 / 嵌套函数(缩进 4 空格)。

    处理多行签名:用括号平衡追踪,找到 `(` 在 def 行开启后在某行 `)` 关闭、
    且同行有 `-> ...:` 标注的那行作为签名结束。多函数在同一 indent 嵌套也
    不会被误吞。

    Returns: 函数体字符串(不含 def 行);找不到返 None。
    """
    lines = source.splitlines()
    def_line_idx = None
    for i, line in enumerate(lines):
        # 匹配 `    async def NAME(` 或 `    def NAME(`(缩进 4 嵌套)
        m = re.match(rf"^\s+(async\s+)?def\s+{re.escape(name)}\s*\(", line)
        if m:
            def_line_idx = i
            break
    if def_line_idx is None:
        return None

    # 用括号平衡找签名结束 — 从 0 开始,数 def 行起的所有 paren
    # 找到首个 balance=0 且该行有 `:` 的行 = 签名结束
    sig_end_idx = None
    balance = 0
    for i in range(def_line_idx, len(lines)):
        line = lines[i]
        # 计 paren 平衡(忽略字符串字面量,简化:假设签名内无 `"""` 等)
        for ch in line:
            if ch == "(":
                balance += 1
            elif ch == ")":
                balance -= 1
        if balance == 0 and ":" in line:
            sig_end_idx = i
            break
    if sig_end_idx is None:
        return None

    # 函数体从 sig_end_idx + 1 起
    body_start_idx = sig_end_idx + 1

    # 跳过 docstring 块(简化:不严格处理嵌入三引号,够用)
    i = body_start_idx
    if i < len(lines) and lines[i].strip().startswith('"""'):
        # 单行 docstring
        if lines[i].strip().endswith('"""') and len(lines[i].strip()) > 6:
            i += 1
        else:
            # 多行 docstring — 跳过直到下一行出现 """
            i += 1
            while i < len(lines) and '"""' not in lines[i]:
                i += 1
            i += 1  # 跳过收尾 """ 行

    # 函数体真实缩进 = 第一个非空 body 行的缩进
    body_indent = None
    for j in range(i, len(lines)):
        if lines[j].strip():
            body_indent = _line_indent(lines[j])
            break
    if body_indent is None:
        return None

    body_lines: list[str] = []
    for j in range(i, len(lines)):
        line = lines[j]
        if line.strip() == "":
            body_lines.append(line)
            continue
        if _line_indent(line) < body_indent:
            break
        body_lines.append(line)
    return "\n".join(body_lines)


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip())
