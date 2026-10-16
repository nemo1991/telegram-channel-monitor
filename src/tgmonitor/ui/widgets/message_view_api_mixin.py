# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""MessageView 公开 API 委托层 — 2026-10-16 plan-batch7 目标 3。

`MessageViewAPIMixin` 持 14 个 `self._model.X()` thin delegate + `set_messages`
(含 `_reapply_filter_if_active` module-level helper)。Mixin 是 view 端的"模型
委托层下沉",主类 MessageView 改多继承 `MessageView(MessageViewAPIMixin, QListView)`。

MRO:`MessageView → MessageViewAPIMixin → QListView → QAbstractItemView → ...`
Shiboken metaclass 兼容(plan-batch7-goal2 MessageListModel 5 mixin +
QAbstractListModel 同模式先例已落地,pytest 全套绿)。

私有字段访问:
- `set_messages` filter re-apply 块读 `self._model._filter_text` /
  `_favorite_only` / `_tag_only` / `_pinned_only` 4 私有字段 — **没有公开 API
  可替代**(跨 `message_list_model_*` 4 mixin 边界,违反"目标 3 只动 view"约束)。
- 抽 module-level `_reapply_filter_if_active(model)`,mixin 不直接触碰 model
  内部状态,语义上属 view 端"批量替换后同步 filter"的实现细节。
"""

from __future__ import annotations

from tgmonitor.core.dto import MediaDTO, MessageDTO, ReactionDTO


def _reapply_filter_if_active(model: object) -> None:
    """`MessageListModel` 4 filter 字段任一 active → 重新 apply 一次。

    读 `model._filter_text` / `_favorite_only` / `_tag_only` / `_pinned_only`
    私有字段 — 跨 mixin 边界无公开 API,语义上属 view 端"set_messages 整批
    替换后同步 filter state"的实现细节。

    Args:
        model: `MessageListModel` 实例(由 `MessageViewAPIMixin.set_messages` 传入)
    """
    if model._filter_text or model._favorite_only or model._tag_only or model._pinned_only:
        model.set_filter(
            model._filter_text,
            favorite_only=model._favorite_only,
            tag_only=model._tag_only,
            pinned_only=model._pinned_only,
        )


class MessageViewAPIMixin:
    """14 thin delegate — 全部 `self._model.X(...)` 委托。

    字段由 `MessageView.__init__` 注入(`self._model`);mixin 类体不放 `__init__`
    (沿用 plan-batch7-goal2 单点 init 收敛惯例)。

    14 method:
    - 实时消息流:append / replace_message / update_media_status / refresh_reactions
    - 批量操作:set_messages / clear_view
    - 过滤:set_filter / set_channel_titles
    - 查询:dto_by_key / row_of_key / count / _format
    - 删行:remove_row
    """

    def append(self, m: MessageDTO) -> None:
        """实时追加一条 — 委托 model。"""
        self._model.append(m)

    def set_messages(self, messages: list[MessageDTO]) -> None:
        """VM 搜索结果批量替换 — 与 `clear_view` + 多次 `append` 等价。

        `messages` 通常按 date ASC 从 storage 拉回 — 正常顺序逐条 `append()`,
        最新一条最后 append → `append` 走 `model.append` 的 `beginInsertRows(0,0)`
        自然落到 row 0(newest-first),与 LIVE 流约定一致。**不要 reversed** ——
        反向迭代会让最旧消息最后 append → 顶到 row 0,顺序颠倒。

        race 处理:live `MessageReceived` 在 `set_messages` 期间到达 → 落到
        `_index_of` 表已存在的 key 上,`append()` 的「已存在替换」分支
        正确处理(替换 text 不增 row)。这是预期行为。

        空列表 = 清空视图 + `_index_of` 表(同 `clear_view()`)。

        2026-09-15 v1.7.5 PR #9:整批替换只 emit 1 次 `modelReset`,避免
        `clear_view() + reset(...)` 双 reset 带来的 2 次 signal + 2 次
        delegate cache 清。

        2026-10-16 plan-batch7 目标 3 commit B:filter re-apply 14 LOC 块抽到
        module-level `_reapply_filter_if_active(model)`,mixin 不触碰 model
        私有字段,保留原状。
        """
        # 2026-09-14 v1.7.5 PR #5 (P0-I):set_messages 是批量替换,清空浮条计数
        self.clear_new_msg_counter()
        # 2026-09-15 v1.7.5 PR #9:单次 reset(new=[] 也能正确清空;model.reset
        # 已分支处理空列表)。`model.reset` 接受按 list 顺序存储,而 view 是
        # newest-first(top=row 0 是最新),所以 caller 给 messages 是 date ASC
        # (newest-last)→ caller 调 reversed 传入。详细见 _model.reset。
        self._model.reset(list(reversed(messages)) if messages else [])
        # 截断后 apply 现有 filter(set_messages 整批替换,apply 一次省心)
        # 2026-09-14 v1.7.5 PR #8:apply 当前完整 filter state(text + 3 元数据)。
        _reapply_filter_if_active(self._model)

    def set_channel_titles(self, titles: dict[int, str]) -> None:
        """外部注入频道 id → title 映射 — 委托 model。"""
        self._model.set_channel_titles(titles)

    def set_filter(
        self,
        text: str = "",
        *,
        favorite_only: bool = False,
        tag_only: bool = False,
        pinned_only: bool = False,
    ) -> None:
        """过滤 LIVE 流 — 文本 + 3 元数据维度(2026-09-14 v1.7.5 PR #8)。

        向后兼容:旧调用 `set_filter(text)` 仍能工作(元数据维度默认 False)。
        """
        self._model.set_filter(
            text,
            favorite_only=favorite_only,
            tag_only=tag_only,
            pinned_only=pinned_only,
        )

    def remove_row(self, channel_id: int, telegram_msg_id: int) -> None:
        """删一行 — 委托 model。"""
        self._model.remove_by_key(channel_id, telegram_msg_id)

    def clear_view(self) -> None:
        """清空列表 — model reset + empty overlay 显示。"""
        self._model.reset([])

    def replace_message(self, msg: MessageDTO) -> None:
        """编辑事件触发 — 委托 model。"""
        self._model.replace_message(msg)

    def update_media_status(self, channel_id: int, telegram_msg_id: int, media: MediaDTO) -> None:
        """异步下载结束回调 — 委托 model。"""
        self._model.update_media_status(channel_id, telegram_msg_id, media)

    def refresh_reactions(
        self,
        channel_id: int,
        telegram_msg_id: int,
        reactions: list[ReactionDTO] | None,
    ) -> None:
        """2026-09-11 v1.7.4:TDLib push reactions 变化 → 局部刷新一行。

        详见 `MessageListModel.refresh_reactions`(调用同方法)。
        """
        self._model.refresh_reactions(channel_id, telegram_msg_id, reactions)

    def dto_by_key(self, channel_id: int, telegram_msg_id: int) -> MessageDTO | None:
        """2026-09-11 v1.7.4:按 (cid, mid) 拿当前 DTO 引用 — 给详情面板同步 reactions 用。

        返回 LIVE 模型里那个对象(可变引用) — 详情面板拿到后 view 自己
        `show_message(dto)` 重建;若不存在返回 None(已被截断 / 不在 LIVE 视图)。
        """
        return self._model.dto_by_key(channel_id, telegram_msg_id)

    def row_of_key(self, channel_id: int, telegram_msg_id: int) -> int | None:
        """2026-09-17 PR 2:按 (cid, mid) 拿当前 row,不存在返 None。"""
        return self._model.row_of_key(channel_id, telegram_msg_id)

    def count(self) -> int:
        """行数 — 兼容 QListWidget.count()。"""
        return self._model.rowCount()

    def _format(self, m: MessageDTO) -> str:
        """2026-09-02 v1.5.3 PR #D1:test shim,暴露 `MessageListModel._format`
        给 golden / 视觉测试(head 行 split("\n")[0] 断言用)。
        """
        return self._model._format(m)
