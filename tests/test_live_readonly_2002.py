"""v2.0.2 只读化契约 — 真实 MainWindow 守护三条减法需求。

2026-11-16 v2.0.2:LIVE 页从「可操作消息工作台」收敛为「只读监听状态看板」。
本文件守护:
- R-01 顶部搜索移除:HeaderBar 无 SearchBar 实例;
- R-02 LIVE 工具条/右键多选移除:无 SelectionToolbar,视图单选模式;
- R-03 详情只读:无导出/删除按钮,保留复制文本 + 关闭;
- F-02 消息口径:MAX_ITEMS == 100(与 load_recent_messages limit 一致)。

用真实窗口(build_real_main_window)而非 mock,防「代码删了但装配漏改」回归。
"""

from __future__ import annotations

from PySide6.QtWidgets import QAbstractItemView, QMenu, QPushButton

from tests.fixtures._factories import make_message
from tests.fixtures._real_window import build_real_main_window
from tgmonitor.ui.widgets.message_list_model import MessageListModel


def test_header_has_no_search_bar(qapp, qloop, tmp_path):
    """R-01:头栏不再有 SearchBar 控件,但保留状态与登录/登出按钮。"""
    win = build_real_main_window(qloop, tmp_path)
    assert not hasattr(win.header, "search_bar"), "HeaderBar 不应再持有 search_bar"
    # A1:状态徽章 + 操作按钮保留
    assert win.header.state_dot is not None
    assert win.header.state_label is not None
    assert win.header.btn_action is not None
    assert win.header.btn_logout is not None


def test_live_view_is_single_selection_and_no_selection_toolbar(qapp, qloop, tmp_path):
    """R-02:LIVE 视图单选模式,窗口内无 SelectionToolbar widget。"""
    win = build_real_main_window(qloop, tmp_path)
    assert win.live_view.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection
    assert not hasattr(win, "_selection_toolbar"), "MainWindow 不应持有 selection_toolbar"
    # 对象名级别的兜底断言(防 widget 仍在对象树里)
    assert win.findChild(QAbstractItemView, "selectionToolbar") is None


def test_message_detail_is_readonly_except_copy(qapp, qloop, tmp_path):
    """R-03:详情按钮仅「复制文本」「关闭详情」,无导出/删除。"""
    win = build_real_main_window(qloop, tmp_path)
    win.live_view.set_messages([make_message(msg_id=1, text="只读确认")])
    qapp.processEvents()
    # 触发详情填充
    dto = win.live_view.model().data(win.live_view.model().index(0, 0), MessageListModel.DtoRole)
    win.message_detail.show_message(dto)
    qapp.processEvents()

    labels = [b.text() for b in win.message_detail.findChildren(QPushButton)]
    assert any("复制文本" in t for t in labels), "复制文本按钮应保留(A3)"
    assert any("关闭详情" in t for t in labels)
    assert not any("导出" in t for t in labels), "详情不应有导出按钮"
    assert not any("删除" in t for t in labels), "详情不应有删除按钮"


def test_max_items_is_100(qapp):
    """F-02:视图容量上限 100,与启动加载 limit 一致。"""
    from tgmonitor.ui.widgets.message_view import MessageView

    assert MessageView.MAX_ITEMS == 100


def test_no_custom_context_menu_on_live_view(qapp, qloop, tmp_path, monkeypatch):
    """A2:LIVE 视图右键不再弹 tgmonitor 自定义菜单(导出/删除/标记等)。"""
    win = build_real_main_window(qloop, tmp_path)
    menus: list[QMenu] = []

    def _capture_exec(self, *args, **kwargs):
        menus.append(self)
        return None

    monkeypatch.setattr(QMenu, "exec", _capture_exec, raising=False)
    # 右键事件走 Qt 默认(无 controller 注入)→ 不应构造任何自定义菜单
    win.live_view.contextMenuEvent = lambda event: None
    win.live_view.set_messages([make_message(msg_id=1, text="x")])
    qapp.processEvents()
    # 直接断言没有 live_actions 注入点
    assert not hasattr(win.live_view, "_live_actions")
    assert not hasattr(win, "live_actions")
    assert menus == []
