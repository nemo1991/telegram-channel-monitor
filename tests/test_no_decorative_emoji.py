"""装饰性 emoji 静态守卫（2.0.6 FR-09）。

分层口径（与 PRD-2.0.6 §5.8 对齐）：
- A 层：注释 / docstring —— 零容忍；
- B/C 层：UI 文案 / 导出模板 / 服务消息 —— 装饰 emoji 已全部改为纯文字；
- D 层：Telegram reaction 等用户真实表情数据 —— 唯一例外精确到字面量，
  见 `_D_LAYER_LITERALS`，不允许任何模块级放宽。

`★` / `✓` 是与文字语义绑定的边界字形（收藏 / 完成），按 PRD 口径不做全清。
"""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_BOUNDARY_GLYPHS = {"★", "✓"}

# 最终态：唯一例外是 D 层自定义 reaction 的占位符（`ReactionDTO.emoji` 值），
# 属于 Telegram 领域数据而非 UI 装饰。精确到字面量，禁止任何模块级放宽。
_D_LAYER_LITERALS = {
    ("tgmonitor/core/telegram/telethon_messages.py", "🖼 "),
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


def test_runtime_emoji_only_in_reaction_domain_data() -> None:
    """最终态：运行时字面量的 emoji 仅允许 D 层 reaction 领域数据占位符。"""
    offenders: list[str] = []
    seen: set[tuple[str, str]] = set()
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
            if not _decorative(node.value):
                continue
            key = (rel, node.value)
            if key in _D_LAYER_LITERALS:
                seen.add(key)
            else:
                offenders.append(f"{rel}:{node.lineno}: {_decorative(node.value)}")
    assert not offenders, "非白名单的运行时 emoji:\n" + "\n".join(offenders)
    assert seen == _D_LAYER_LITERALS, "D 层白名单已漂移:" + "\n".join(
        sorted(_D_LAYER_LITERALS - seen) + sorted(seen - _D_LAYER_LITERALS)
    )
