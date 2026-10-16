# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""MessageListModel filter mixin — 2026-10-16 plan-batch7 目标 2。

`FilterMixin` 持 4 个 filter state 字段(`_filter_text` /
`_favorite_only` / `_tag_only` / `_pinned_only`)+ AND 语义 `_matches`
+ `set_filter` / `set_channel_titles` 公开 setter。

字段由 `_MessageListModelBase.__init__` 单点 init 收敛;本 mixin 不写
`__init__`。MRO:`MessageListModel(Format, Filter, Mutation, Storage, _Base)`。
"""

from __future__ import annotations

from tgmonitor.core.dto import MessageDTO


class FilterMixin:
    """4 个 filter state 字段(text + favorite + tag + pinned)+ AND 语义。

    AND 语义:`_matches` 任一条件不满足返 False(过滤掉);text 空 = 不参与
    AND(只过滤元数据);元数据字段 False = 不参与 AND。
    """

    def set_filter(
        self,
        text: str = "",
        *,
        favorite_only: bool = False,
        tag_only: bool = False,
        pinned_only: bool = False,
    ) -> None:
        """设过滤条件 — 所有 row 的 HiddenRole 变化 → emit dataChanged。

        向后兼容:旧调用 `set_filter(text)` 仍能用(默认 favorite/tag/pinned=False)。

        2026-09-14 v1.7.5 PR #8:加 favorite/tag/pinned 3 个 bool kwarg(AND
        语义)。SearchBar ★/🏷/📌 toggle 状态走这条路径 — 即时 narrow LIVE
        流已加载消息,无 IO。`search_messages` 异步路径另走 storage 服务端过滤。
        """
        self._filter_text = text.strip().lower()  # type: ignore[attr-defined]
        self._favorite_only = favorite_only  # type: ignore[attr-defined]
        self._tag_only = tag_only  # type: ignore[attr-defined]
        self._pinned_only = pinned_only  # type: ignore[attr-defined]
        if self.rowCount() == 0:  # type: ignore[attr-defined]
            return
        top = self.index(0, 0)
        bottom = self.index(self.rowCount() - 1, 0)  # type: ignore[attr-defined]
        self.dataChanged.emit(top, bottom, [self.HiddenRole])  # type: ignore[attr-defined]

    def set_channel_titles(self, titles: dict[int, str]) -> None:
        """设 channel_titles + FormattedRole 全部失效(影响 head 频道名)。

        PR #9:channel titles 影响每行的 head 频道名 → 全表 FormattedRole 都失效,
        缓存全清。
        """
        self._channel_titles = dict(titles)  # type: ignore[attr-defined]
        self._format_cache.clear()  # type: ignore[attr-defined]  # PR #9:全表失效
        if self.rowCount() == 0:  # type: ignore[attr-defined]
            return
        top = self.index(0, 0)
        bottom = self.index(self.rowCount() - 1, 0)  # type: ignore[attr-defined]
        self.dataChanged.emit(top, bottom, [self.FormattedRole])  # type: ignore[attr-defined]

    def _matches(self, m: MessageDTO) -> bool:
        """匹配规则:text + favorite + tag + pinned(AND 语义 — 任一不满足就 False)。

        2026-09-14 v1.7.5 PR #8:filter state 走 self 上的 4 个字段:
        `_filter_text` / `_favorite_only` / `_tag_only` / `_pinned_only`。
        `_filter_text` 空 = 不参与 AND(只过滤元数据);元数据字段 False
        = 不参与 AND。
        """
        if self._favorite_only and not m.is_favorite:  # type: ignore[attr-defined]
            return False
        if self._tag_only and not m.tags:  # type: ignore[attr-defined]
            return False
        if self._pinned_only and not m.is_pinned:  # type: ignore[attr-defined]
            return False
        text = self._filter_text  # type: ignore[attr-defined]
        if not text:
            return True
        if m.text and text in m.text.lower():
            return True
        if m.author and text in m.author.lower():
            return True
        title = self._channel_titles.get(m.channel_id, "")  # type: ignore[attr-defined]
        if title and text in title.lower():
            return True
        return str(m.telegram_msg_id) == text or text.lstrip("#") == str(m.telegram_msg_id)
