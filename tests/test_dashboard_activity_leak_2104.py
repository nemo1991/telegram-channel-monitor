"""2.1.4 P2 #41 回归 — 大盘活动列表刷新不泄漏 itemWidget。

`QListWidget.clear()` 不会 deleteLater `setItemWidget` 的 widget(实测
widget 仍存活,变 viewport 孤儿);`takeItem` 后 `itemWidget(item)` 恒
返回 None(靠 model index 关联)。守两条路径:
- `_apply_activities` 覆盖刷新:旧 widget 全部 deleteLater;
- `_add_activity` 超 200 条裁剪:被裁 widget deleteLater。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from PySide6.QtWidgets import QApplication

from tgmonitor.core.config import Settings
from tgmonitor.core.events import EventBus
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget


def _make_dashboard() -> tuple[DashboardWidget, MagicMock]:
    """MagicMock app + EventBus 构造 DashboardWidget(无 loop,跳过异步取数)。"""
    app = MagicMock()
    app.bus = EventBus()
    app.client = MagicMock()
    app.client.state = "ready"
    app.client.me = None
    app.settings = Settings.for_test()
    app.storage.count_tg_subscribed = AsyncMock(return_value=0)
    app.storage.aggregate_per_channel = AsyncMock(return_value={})
    monitor = MagicMock()
    monitor.monitored_ids = []
    dash = DashboardWidget(app, monitor, channels_provider=lambda: {})
    return dash, app


def _spy_deletelater(widget) -> list:
    """把 widget.deleteLater 替换成 spy(记录调用),返回调用记录列表。"""
    calls: list = []
    widget.deleteLater = lambda *args, **kwargs: calls.append(widget)  # type: ignore[method-assign]
    return calls


def test_apply_activities_deletelaters_old_item_widgets(qapp: QApplication) -> None:
    """刷新活动列表:旧 itemWidget 全部被 deleteLater(旧代码 clear() 时不触发)。"""
    dash, _app = _make_dashboard()
    dash._add_activity("refresh", "旧 1")
    dash._add_activity("refresh", "旧 2")
    widgets = [
        dash.activity_list.itemWidget(dash.activity_list.item(i))
        for i in range(dash.activity_list.count())
    ]
    assert len(widgets) == 2
    assert all(w is not None for w in widgets)

    spied = [_spy_deletelater(w) for w in widgets]
    dash._apply_activities([])

    assert all(len(calls) == 1 for calls in spied)


def test_add_activity_trim_deletelaters_evicted_widget(qapp: QApplication) -> None:
    """超 200 条裁剪:被挤出的旧 widget 被 deleteLater(而非 viewport 孤儿)。"""
    dash, _app = _make_dashboard()
    for i in range(200):
        dash._add_activity("refresh", f"n{i}")
    assert dash.activity_list.count() == 200
    oldest = dash.activity_list.item(dash.activity_list.count() - 1)
    oldest_widget = dash.activity_list.itemWidget(oldest)
    assert oldest_widget is not None
    calls = _spy_deletelater(oldest_widget)

    dash._add_activity("refresh", "new")  # 201 条 → 裁掉最旧一条

    assert dash.activity_list.count() == 200
    assert len(calls) == 1
