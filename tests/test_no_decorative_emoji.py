"""装饰性 emoji 静态守卫（2.0.6 FR-09）。

分层口径（与 PRD-2.0.6 §5.8 对齐）：
- A 层：注释 / docstring —— 零容忍，本守卫第一批即锁死；
- B/C 层：运行时字面量（UI 文案 / 导出模板 / 服务消息）—— 按模块宽白名单起步，
  每批清理后从白名单移除；白名单出现"已无 emoji 的模块"同样判失败，避免只增不减；
- D 层：Telegram reaction 等用户真实表情数据 —— 永久允许的边界见
  `_BOUNDARY_GLYPHS` 与后续批次的细粒度例外，不在此处放宽。

`★` / `✓` 是与文字语义绑定的边界字形（收藏 / 完成），按 PRD 口径不做全清。
"""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_BOUNDARY_GLYPHS = {"★", "✓"}

# B 层已清零；仅剩 C 层导出/服务消息与 D 层 reaction 占位符待处理。
_RUNTIME_EMOJI_MODULES = {
    "tgmonitor/core/export/html_exporter.py",
    "tgmonitor/core/export/markdown_exporter.py",
    "tgmonitor/core/telegram/telethon_messages.py",
}


def _decorative(text: str) -> list[str]:
    return [
        ch
        for ch in text
        if ch not in _BOUNDARY_GLYPHS
        and (0x1F300 <= ord(ch) <= 0x1FAFF or 0x2600 <= ord(ch) <= 0x27BF)
    ]


def _docstring_nodes(tree: ast.AST) -> list[ast.Constant]:
    nodes: list[ast.Constant] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                nodes.append(body[0].value)
    return nodes


def test_no_decorative_emoji_in_comments_and_docstrings() -> None:
    """A 层：注释 token 与 docstring 节点内容中不得出现装饰性 emoji。"""
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        source = path.read_text(encoding="utf-8")
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT and (hits := _decorative(token.string)):
                offenders.append(f"{rel}:{token.start[0]}: {hits}")
        for node in _docstring_nodes(ast.parse(source)):
            if hits := _decorative(node.value):
                offenders.append(f"{rel}:{node.lineno}: docstring {hits}")
    assert not offenders, "注释/docstring 中的装饰 emoji 未清:\n" + "\n".join(offenders)


def test_runtime_emoji_limited_to_whitelisted_modules() -> None:
    """B/C/D 层：运行时字面量的 emoji 仅允许出现在待清理白名单模块。"""
    found: set[str] = set()
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        doc_ids = {id(node) for node in _docstring_nodes(tree)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in doc_ids:
                continue
            if hits := _decorative(node.value):
                if rel not in _RUNTIME_EMOJI_MODULES:
                    offenders.append(f"{rel}:{node.lineno}: {hits}")
                else:
                    found.add(rel)
    assert not offenders, "白名单外的运行时 emoji:\n" + "\n".join(offenders)
    stale = _RUNTIME_EMOJI_MODULES - found
    assert not stale, "白名单已过期,请随清理进度移除:\n" + "\n".join(sorted(stale))
