"""搜索 controller — `_on_search_*` 8 handler + `_run_search_query`。

2026-10-04 plan 阶段 3.3:从 `MainWindow` 抽出,模板 controller — 不持有
MainWindow 引用,只通过 ctx 访问 `header.search_bar` / `live_view` / `vm`。
后续 `live_selection_controller` / `media_actions_controller` /
`message_stream_controller` 沿用此样板。

信号契约(均来自 `SearchBar`):
- `text_changed(str)` → `on_search_changed`:双过滤(即时快过滤 + 300ms debounce)
- `date_changed(dt_from, dt_to)` → `on_search_date_changed`:日期变立即拉(无 debounce)
- `scope_changed(bool)` → `on_search_scope_changed`:重启动 debounce
- `favorite_toggled(bool)` / `tag_toggled(bool)` / `pinned_toggled(bool)` → 3 filter 切换
- `_search_debounce.timeout` → `on_debounce_fire`:300ms 到点调 `vm.search_messages`

`_run_search_query` 是核心 — 读 SearchBar 全部状态 + 3 toggle,空 query
清视图,非空透传给 VM。
"""

from __future__ import annotations

from tgmonitor.ui.controllers._base import MainWindowCtx


class SearchController:
    """搜索框信号 → LIVE 流双过滤 + 服务端权威搜索结果。"""

    def __init__(self, ctx: MainWindowCtx) -> None:
        self._ctx = ctx

    def on_search_changed(self, txt: str) -> None:
        """2026-09-02 v1.5.2 PR #B5:搜索框变化 → 双过滤。

        1) 即时:`live_view.set_filter(txt)` 内存快过滤已加载 LIVE 流(无 IO,
        UI 立即响应 — 当前 200 条 LIVE 的 hide/show)。
        2) 300ms debounce:启动 `_search_debounce` QTimer;到点触发
           `vm.search_messages(...)` → 异步拉 storage 服务端命中 →
           emit `message_search_results` → `live_view.set_messages(...)`
           批量替换视图。

        双过滤不冲突:快过滤是「视觉欺骗」(本地 hide/show),set_messages 是
        「权威」(server-side 命中替换)。
        2026-09-14 v1.7.5 PR #5 (P0-L):搜索激活 → searching overlay(无匹配结果);
        清空搜索词 → live_empty / no_subscribed(由 channels_changed 决定)。
        """
        # 1) 即时快过滤(无 IO)— 2026-09-14 v1.7.5 PR #8:同时传 3 filter toggle
        # 状态,UI 即时 narrow 已加载的 LIVE 流。
        sb = self._ctx.header.search_bar
        self._ctx.live_view.set_filter(
            txt,
            favorite_only=sb.is_favorite_active(),
            tag_only=sb.is_tag_active(),
            pinned_only=sb.is_pinned_active(),
        )
        # 2026-09-14 v1.7.5 PR #5 (P0-L):搜索词非空 → 切 searching overlay
        # (覆盖 live_empty / no_subscribed);清空搜索词 → 由 _refresh_state
        # 触发回到正确 state。
        if txt.strip():
            self._ctx.live_view.set_empty_state("searching")
        # 2) debounce 启动 timer;每次输入重置,只有 300ms 内没新输入才触发
        self._ctx.search_debounce.start()

    def on_search_date_changed(self, dt_from, dt_to) -> None:
        """2026-09-02 v1.5.2 PR #B5:日期范围变化 → 走同一 debounce 拉结果。

        日期变化立即触发(不需要 300ms 等待 — 用户主动调整日历 = 期望立刻响应)。
        """
        self.run_search_query()

    def on_search_scope_changed(self, is_all: bool) -> None:
        """2026-09-03 v1.5.3 PR #D2:scope toggle(已订阅/全部)→ 走同一
        debounce 重拉(不 clear view,让用户对比结果差异)。

        重启 debounce timer:用户在 300ms 内连切 toggle(罕见)只触发 1 次
        `vm.search_messages`(最新 scope 值生效)。
        """
        self._ctx.search_debounce.start()

    def on_search_favorite_toggled(self, _checked: bool) -> None:
        """2026-09-14 v1.7.5 PR #8:★ favorite toggle → 同步 LIVE 流 narrow +
        debounce 异步重拉(走 storage 服务端过滤)。

        `run_search_query` 会读 SearchBar 全部 toggle 状态,这里只触发
        debounce 即可。
        """
        self._ctx.search_debounce.start()

    def on_search_tag_toggled(self, _checked: bool) -> None:
        """2026-09-14 v1.7.5 PR #8:🏷 tag toggle → debounce 重拉。"""
        self._ctx.search_debounce.start()

    def on_search_pinned_toggled(self, _checked: bool) -> None:
        """2026-09-14 v1.7.5 PR #8:📌 pinned toggle → debounce 重拉。"""
        self._ctx.search_debounce.start()

    def on_debounce_fire(self) -> None:
        """300ms debounce 到点 → 调 vm.search_messages。"""
        self.run_search_query()

    def run_search_query(self) -> None:
        """拉一次搜索结果:空 query → emit `[]` 清空视图(回 LIVE 流占位)。

        `live_view.set_messages([])` 行为是 clear_view — 但 LIVE 流真正
        恢复需要重新 `vm.load_recent_messages()` 重新拉 200 条。这里简化为
        「空 query = 清空列表」(用户清空搜索时视觉上是「无结果」,后续如有
        新 LIVE 消息到达,append 会加进空表 — 等于 LIVE 流以增量形式
        重建)。如果未来需要"清空 = 重拉 LIVE 200 条",改成 emit 触发
        `vm.load_recent_messages()` 即可。

        2026-09-03 v1.5.3 PR #D2:`scope_btn` toggle 控制搜索范围:
        - 已订阅(默认)→ 走 VM `search_messages(scope="subscribed")`
        - 全部(含已退订频道历史)→ 走 `scope="all"`(VM 内部转
          `include_unsubscribed=True`)
        2026-09-14 v1.7.5 PR #8:同时读 ★/🏷/📌 3 toggle → 透传
        `favorite_only` / `tag_only` / `pinned_only` 给 VM。
        """
        sb = self._ctx.header.search_bar
        txt = sb.text()
        df, dt = sb.date_range()
        scope = "all" if sb.scope() else "subscribed"
        # 2026-09-14 v1.7.5 PR #8:3 filter toggle 状态(默认 False 不窄化)。
        fav_only = sb.is_favorite_active()
        tag_only = sb.is_tag_active()
        pin_only = sb.is_pinned_active()
        if not txt and df is None and dt is None and not fav_only and not tag_only and not pin_only:
            # 空 query → 清空视图(后续 live 消息 append 进去会自然重建 LIVE 流)
            self._ctx.live_view.set_messages([])
            return
        # 走 VM search_messages;scope 由 sb.scope() 控制
        # (channel_ids 不显式传,VM 根据 scope 自动拉「已订」或「全部」)
        # PR #8:同时传 3 filter toggle 状态。
        self._ctx.vm.search_messages(
            text=txt,
            date_from=df,
            date_to=dt,
            limit=200,
            scope=scope,
            favorite_only=fav_only,
            tag_only=tag_only,
            pinned_only=pin_only,
        )
