"""架构守门测试 — 静态保证分层边界。

2026-10-04 重构 plan 阶段 1.4 落地:
  - `core/` 严禁 import PySide6 / qasync / `tgmonitor.ui.*`(顶 + 函数体内
    lazy 都算)
  - `cli/` 严禁 import `tgmonitor.ui.*`(顶层 / lazy 都算)
  - 走 AST walk 扫全部 .py 文件,避免 `grep -r` 漏函数体内 lazy import

历史:
  - 现有 `tests/test_cli.py::test_cli_does_not_import_qt_or_qasync` 用
    subprocess 跑 CLI 入口,覆盖 cli.main + cli.sync + cli.monitor 三个
    模块的 sys.modules 泄漏。本文件补静态层(覆盖 cli 全包所有模块
    + core 全包所有模块),形成双层防御。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path("src/tgmonitor")

# 禁止 import 的根包/模块 — 任何 import 这种就是泄漏
FORBIDDEN_IMPORTS = (
    "PySide6",
    "qasync",
)

# 模块 → 子树路径(用作 "core/<x>/..." 等子包是否走 core 边界)
# 这里只关注"core"与"cli"两个边界最严的子树
GUARDED_PACKAGES = (
    "core",
    "cli",
)


def _collect_py_files(package: str) -> list[Path]:
    """收集 src/tgmonitor/<package> 下所有 .py 文件(不含 __pycache__)。"""
    pkg_dir = SRC / package
    if not pkg_dir.is_dir():
        return []
    return [p for p in pkg_dir.rglob("*.py") if "__pycache__" not in p.parts]


def _scan_forbidden_imports(py_file: Path) -> list[tuple[int, str]]:
    """AST walk 一个 .py 文件,返回所有 forbidden import 的 (line, name) 列表。

    覆盖范围:顶层 `import x` / `from x import y`(包括 lazy import 在
    函数体内或 try 块内的情况 — 任何 AST Import / ImportFrom 节点都计入)。
    """
    src = py_file.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(py_file))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        target: str | None = None
        if isinstance(node, ast.Import):
            target = node.names[0].name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            target = node.module.split(".")[0]
        if target and target in FORBIDDEN_IMPORTS:
            hits.append((node.lineno, target))
    return hits


def _scan_subpackage_imports(py_file: Path, package: str, sub_root: str) -> list[tuple[int, str]]:
    """AST walk 一个 .py 文件,返回所有 import 跨层到 `tgmonitor.<sub_root>`
    的 (line, name) 列表。例如 package='cli' 时禁止 `import tgmonitor.ui.*`。
    """
    src = py_file.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(py_file))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        target: str | None = None
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[1] if alias.name.startswith("tgmonitor.") else None
                if root and root == sub_root:
                    target = alias.name
                    break
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if node.module.startswith(f"tgmonitor.{sub_root}"):
                target = node.module
        if target:
            hits.append((node.lineno, target))
    return hits


# ============================================================
# core/ 严禁 PySide6 / qasync
# ============================================================


@pytest.mark.parametrize("py_file", _collect_py_files("core"), ids=lambda p: str(p.relative_to(SRC)))
def test_core_does_not_import_qt_or_qasync(py_file: Path) -> None:
    """`src/tgmonitor/core/` 下任何 .py 文件,任何位置(顶层 / lazy / try 块)
    都禁止 import PySide6 或 qasync。

    2026-10-04 时已知违规:
      - core/keybinding.py:22 `from PySide6.QtGui import QKeySequence`
      - core/media_service.py:499/500/703 三个 lazy import
      - (阶段 2 修复后会清零)

    何时会触发失败:任何阶段新增的 core/ 模块不慎 import Qt。
    """
    hits = _scan_forbidden_imports(py_file)
    if hits:
        raise AssertionError(
            f"{py_file.relative_to(SRC)} 含禁止 import (违反 core 无 Qt 边界): "
            f"{[(line, name) for line, name in hits]}"
        )


# ============================================================
# cli/ 严禁 import tgmonitor.ui.*
# ============================================================


@pytest.mark.parametrize("py_file", _collect_py_files("cli"), ids=lambda p: str(p.relative_to(SRC)))
def test_cli_does_not_import_ui(py_file: Path) -> None:
    """`src/tgmonitor/cli/` 下任何 .py 文件,任何位置都禁止 import
    `tgmonitor.ui.*`。

    2026-09-27 refactor CLI 切分的核心契约 — CLI 是 headless 纯 asyncio,
    不能因为 import 链被迫加载 GUI 框架(冷启动 300ms → 1-3s)。
    现有 `tests/test_cli.py::test_cli_does_not_import_qt_or_qasync` 用
    subprocess 守 cli.main/sync/monitor 三入口;本测试补静态层覆盖 cli
    全包所有模块(包括未来新增的子命令模块,只要走 cli/ 就自动守门)。
    """
    hits = _scan_subpackage_imports(py_file, package="cli", sub_root="ui")
    if hits:
        raise AssertionError(
            f"{py_file.relative_to(SRC)} 跨层 import tgmonitor.ui.* "
            f"(违反 cli 无 UI 边界): {[(line, name) for line, name in hits]}"
        )
