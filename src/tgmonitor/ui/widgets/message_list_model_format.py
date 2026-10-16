# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""MessageListModel format mixin — 2026-10-16 plan-batch7 目标 2。

`FormatMixin` 持 `_format` method + `_format_meta_icons` module-level
helper(meta icons 格式化是 model 渲染层职责,语义上属 Format 子模块)。

字段由 `_MessageListModelBase.__init__` 单点 init 收敛;本 mixin 不写
`__init__`。MRO:`MessageListModel(Format, Filter, Mutation, Storage, _Base)`。
"""

from __future__ import annotations

from datetime import UTC

from tgmonitor.core.dto import MediaDownloadStatus, MessageDTO


def _format_meta_icons(m: MessageDTO) -> str:
    """2026-09-10 v1.7.3:把 ★ / 🏷 / 📝 / 📌 拼成一行字符串;无 metadata 返空串。

    顺序固定:★ → 🏷 / 📝 / 📌(★ 最重要,放最前;📌 server-side state 放最后)。
    tags 用 `,` 分隔(与 LIVE 行 hover / MessageDetail 一致);notes 超过
    30 字符截断加 `…`(行高可控,避免 100 字备注撑爆单行)。

    2026-09-11 v1.7.4:reactions 拼到末尾(`🔥 5  👍 3`)— 由
    MessageInteractionsChanged event 触发本地刷新,实时性受限于 TDLib
    `updateMessageInteractionInfo` 推送(bots-only `updateMessageReactions`
    user client 不收)。

    2026-10-16 plan-batch7 目标 2:从 `message_view.py` module-level 整体搬
    到本文件,语义上属 Format 子模块;破循环 import(format mixin 不再
    `from message_view import _format_meta_icons`,而 `message_view.py` 也
    不再持有此 helper)。
    """
    from tgmonitor.ui.widgets.reaction_format import format_reactions_short

    parts: list[str] = []
    if m.is_favorite:
        parts.append("★")
    if m.tags:
        parts.append(f"🏷{','.join(m.tags)}")
    if m.notes:
        snippet = m.notes if len(m.notes) <= 30 else m.notes[:30] + "…"
        parts.append(f"📝{snippet}")
    if m.is_pinned:
        parts.append("📌")
    if m.reactions:
        rx = format_reactions_short(m.reactions, max_show=3, mark_chosen=False)
        if rx:
            parts.append(rx)
    return " ".join(parts)


class FormatMixin:
    """`_format` method — 渲染 head / body / media 行。

    paint 热路径:`data(idx, FormattedRole)` → 走 `_format_cache` 命中即返
    (PR #9 perf),未命中才调 `_format(m)`,结果回写 cache。Mutation 路径
    (append / replace / update_media_status / refresh_reactions / remove)
    负责失效对应 key 的 cache(走 `_format_cache.pop(key, None)`)。
    """

    def _format(self, m: MessageDTO) -> str:
        # 本地时区显示;m.date 是 **aware UTC**(来自 dto.py 默认工厂
        # `datetime.now(UTC)`,或 _map_message 的 `datetime.fromtimestamp(ts, UTC)`)。
        # 如果 m.date 没 tzinfo(从旧版本反序列化),fallback attach UTC tzinfo,
        # 然后 astimezone() 转本地;否则 astimezone() 把 naive 当成本地时间,
        # 不做时区转换。
        if m.date:
            dt_utc = m.date if m.date.tzinfo else m.date.replace(tzinfo=UTC)
            dt_local = dt_utc.astimezone()
            dt = dt_local.strftime("%H:%M:%S")
        else:
            dt = "[?]"
        # 频道名:有 title 用 title,没有用 #id 回退
        title = self._channel_titles.get(m.channel_id)  # type: ignore[attr-defined]
        ch_label = f"[{title}]" if title else f"[#{m.channel_id}]"
        # msg_id:在频道内的消息 id(Telegram 原始),不是 DB 自增
        msg_id = m.telegram_msg_id
        head = f"⏱ {dt}  {ch_label}  #{msg_id}"
        if m.author:
            head += f"  👤 {m.author}"
        # 2026-09-10 v1.7.3:用户元数据 / pin 图标行 — ★ 收藏 / 🏷tags / 📝notes 截断
        # / 📌 pin。与 v1.7.2 metadata 字段配套:v1.7.2 加了字段但没渲染,本版本补
        # 上。用户右键 ★ 后行立即显示 ★(无需 reload)。
        meta_icons = _format_meta_icons(m)
        if meta_icons:
            head += "  " + meta_icons
        body = m.text or ""
        if m.has_media:
            parts = []
            for med in m.media:
                label = med.type.value
                # 下载状态标记:⏳ 下载中 / ❌ 失败 / ✓ 已下载(PENDING 不加后缀)
                if med.download_status == MediaDownloadStatus.DOWNLOADING:
                    label += "⏳"
                elif med.download_status == MediaDownloadStatus.FAILED:
                    label += "❌"
                elif med.download_status == MediaDownloadStatus.DONE:
                    label += "✓"
                parts.append(label)
            body += f"  📎 {','.join(parts)}"
        return f"{head}\n  {body}"
