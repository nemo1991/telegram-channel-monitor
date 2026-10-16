# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""MessageListModel storage mixin — 2026-10-16 plan-batch7 目标 2。

`StorageMixin` 持 DTO 路径查询(只读) — `dto_by_key` / `row_of_key` /
`row_values`。字段(`_items` / `_index_of` / `_format_cache` /
`_channel_titles`)由 `_MessageListModelBase.__init__` 单点 init 收敛,
本 mixin 不写 `__init__`(避免 5 个 super 样板 + 字段归属混乱)。

MRO:`MessageListModel(Format, Filter, Mutation, Storage, _Base)`。
"""

from __future__ import annotations

from tgmonitor.core.dto import MessageDTO


class StorageMixin:
    """DTO 路径查询(只读)— `dto_by_key` / `row_of_key` / `row_values`。

    写入路径(mutation)走 `MutationMixin`;过滤走 `FilterMixin`;渲染走
    `FormatMixin`。本 mixin 只暴露"按 (channel_id, telegram_msg_id) 反查
    DTO / row"3 个只读 method,语义上等同 `_index_of` 字典的 3 个公开视图。
    """

    def dto_by_key(self, channel_id: int, telegram_msg_id: int) -> MessageDTO | None:
        """按 (cid, mid) 拿当前 DTO 引用 — 不存在返 None。

        2026-09-11 v1.7.4:LiveActionsController / message_detail 用这个查
        选中行的最新 DTO(可能被编辑 / reactions 更新)。
        """
        row = self._index_of.get((channel_id, telegram_msg_id))  # type: ignore[attr-defined]
        if row is None:
            return None
        return self._items[row]  # type: ignore[attr-defined]

    def row_of_key(self, channel_id: int, telegram_msg_id: int) -> int | None:
        """按 (cid, mid) 拿当前 row — 不存在返 None。

        2026-09-17 PR 2:测试 / 外部代码用这个查 row,不再穿透 `_index_of`
        私有 dict。
        """
        return self._index_of.get((channel_id, telegram_msg_id))  # type: ignore[attr-defined]

    def row_values(self) -> set[int]:
        """返当前所有 row 的集合 — 测试验「row 是连续 0..N」用。

        `_index_of.values()` 的公开只读别名;不要拿这个 list 来寻址 O(1)
        (仍走 `row_of_key`)。
        """
        return set(self._index_of.values())  # type: ignore[attr-defined]
