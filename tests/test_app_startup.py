"""`app.py` 启动拆分的回归测试 — 2026-09-25 v1.8.x。

`_setup_then_show` 把 `monitor.start()` + `app.bootstrap()` 拆到 `_background_startup`
后台 task,`win.show()` 必须先于它们执行,这样双击 app 时主窗口立即可见,不
等 libtdjson 启动(冷启动 / 断网可达 5-10s)。

测试策略:静态分析 `app.py` 源码(与 `test_app_run.py` 同模式)— 验:
1. `_background_startup` 函数存在
2. `_setup_then_show` 里 `win.show()` 在 `_background_startup` 调用之前
3. `_setup_then_show` 不直接 `await monitor.start()` 或 `await app.bootstrap()`
4. `_background_startup` 把三步收敛到 `app_svc.start(StartOptions(...))`,paused
   终态由调用方提示(步骤顺序由 `test_app_start.py` 的 start() 单测守护)
5. `_background_startup` 出错时 publish ErrorOccurred 事件

2026-11-26 ADR-002:原「源码正则断步骤顺序」改为「断委托给 start()」。

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


def test_background_startup_delegates_to_app_start(app_source: str) -> None:
    """`_background_startup` 必须把三步收敛到 `app_svc.start(StartOptions(...))` —
    ADR-002 后步骤顺序由 `start()` 单测守护,调用方不再手写(否则顺序又分裂)。
    """
    body = _extract_function_body(app_source, "_background_startup")
    assert body is not None, "_background_startup 函数体未找到"

    assert re.search(r"app_svc\.start\(\s*StartOptions\(", body) is not None, (
        "_background_startup 未调用 app_svc.start(StartOptions(...))"
    )
    # 三步骤不再由 GUI 手写 — 手写会让顺序重新分裂
    for pattern in (
        r"list_monitored_channels\(\)",
        r"monitor\.set_whitelist\(",
        r"await\s+monitor\.start\(\)",
        r"app_svc\.bootstrap\(\)",
    ):
        assert re.search(pattern, body) is None, (
            f"_background_startup 仍有手写步骤 {pattern} — 收敛被还原了?"
        )


def test_background_startup_handles_paused(app_source: str) -> None:
    """paused 是 `start()` 返回的终态,调用方只做提示 — client 保持 uninit,
    用户点 tray「继续监听」走 resume_monitor。
    """
    body = _extract_function_body(app_source, "_background_startup")
    assert body is not None
    assert 'result.state == "paused"' in body, (
        "_background_startup 缺 paused 终态处理 — 启动即暂停场景未处理"
    )
    assert "继续监听" in body, "paused 分支缺 tray 恢复提示"


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
    """步骤名经 `on_step` 回调映射到 status_bar 活动文案 — 四个步骤都要有
    文案(含 paused),否则状态栏左侧看不到启动进度。

    2026-11-26 ADR-002:回调替代原先三处手写 `show_activity(...)`。
    """
    body = _extract_function_body(app_source, "_background_startup")
    assert body is not None
    assert "on_step" in body, "_background_startup 缺 on_step 回调"
    assert re.search(r"win\.status_bar\.show_activity\(", body) is not None, (
        "on_step 未转调 status_bar.show_activity"
    )
    for label in ("加载已订阅频道", "启动监听服务", "连接 Telegram", "监听已暂停"):
        assert label in body, f"步骤 {label} 缺状态栏文案"


# ---- helper ----


def _extract_function_body(source: str, name: str) -> str | None:  # noqa: C901 — 测试专用源码解析,分支多为括号/缩进边界,够用即可
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
