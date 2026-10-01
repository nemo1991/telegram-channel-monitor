"""2026-09-11 v1.7.4:reactions 字符串格式化公共 helper。

之前 `_format_reactions` 在 message_detail.py 私有;v1.7.4 LIVE 行也要
渲染 reactions,提取成公共 helper(避免两处实现分叉)。

设计目标:
- 短串(LIVE 行:`🔥 5  👍 3`,max_show 默认 4 限制宽度)
- 自己投的用 `[]` 包,详情面板需要(「我投过的」高亮),LIVE 行因宽度
  紧可不传 `is_chosen` 包装(默认)
- `count=0` 跳过 — TDLib 偶尔推送空 reaction 占位

**Root cause 限制**:TDLib `updateMessageReactions` 是 **bots-only**,
user client(`TDLib` JSON client,非 bot token)**不会收到**推送。
本项目 UI 实时性靠 `updateMessageInteractionInfo`(`MessageInteractionsChanged
  .reactions`),服务端推送策略决定延迟。
"""

from __future__ import annotations

from tgmonitor.core.dto import ReactionDTO


def format_reactions_short(
    reactions: list[ReactionDTO] | None,
    *,
    max_show: int = 4,
    mark_chosen: bool = True,
) -> str:
    """2026-09-11 v1.7.4:reactions 列表 → 单行展示。

    Args:
        reactions:ReactionDTO 列表(`None` 或空 list 返空串)。
        max_show:最多展示 N 个 emoji;超过显示 `+N`。LIVE 行用 4 控宽度,
            详情面板传 999 显全部。
        mark_chosen:自己投的 emoji 用 `[…]` 包(详情用);LIVE 行传 False
            避免行宽膨胀。

    Returns:
        「🔥 5  👍 3」单行字符串;无 reactions 返空串。
    """
    if not reactions:
        return ""
    parts: list[str] = []
    shown = 0
    extra = 0
    for r in reactions:
        if r.count <= 0:
            continue
        if shown >= max_show:
            extra += 1
            continue
        body = f"{r.emoji} {r.count}"
        if mark_chosen and r.is_chosen:
            parts.append(f"[{body}]")
        else:
            parts.append(body)
        shown += 1
    if extra > 0:
        parts.append(f"+{extra}")
    return "  ".join(parts)
