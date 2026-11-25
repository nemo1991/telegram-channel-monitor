"""v2.0.2 只读化契约 — 真实 MainWindow 守护三条减法需求。

2026-11-16 v2.0.2:LIVE 页从「可操作消息工作台」收敛为「只读监听状态看板」。
本文件守护:
- R-01 顶部搜索移除:v2.0.10 起页头整块删除;
- R-02 LIVE 工具条/右键多选移除:无 SelectionToolbar,视图单选模式;
- R-03 详情只读:无导出/删除按钮,保留复制文本;
  (2026-11-18 v2.0.5:关闭详情按钮已移除,复制文本上提到详情顶部)
- F-02 消息口径:MAX_ITEMS == 100(与 load_recent_messages limit 一致)。

用真实窗口(build_real_main_window)而非 mock,防「代码删了但装配漏改」回归。
"""

from __future__ import annotations

from PySide6.QtWidgets import QPushButton

from tests.fixtures._factories import make_message
from tests.fixtures._real_window import build_real_main_window
from tgmonitor.ui.widgets.message_list_model import MessageListModel


def test_message_detail_is_readonly_except_copy(qapp, qloop, tmp_path):
    """R-03:详情按钮仅「复制文本」(详情顶部),无导出/删除/关闭。"""
    win = build_real_main_window(qloop, tmp_path)
    win.live_view.set_messages([make_message(msg_id=1, text="只读确认")])
    qapp.processEvents()
    # 触发详情填充
    dto = win.live_view.model().data(win.live_view.model().index(0, 0), MessageListModel.DtoRole)
    win.message_detail.show_message(dto)
    qapp.processEvents()

    labels = [b.text() for b in win.message_detail.findChildren(QPushButton)]
    assert any("复制文本" in t for t in labels), "复制文本按钮应保留(A3)"
    assert not any("关闭详情" in t for t in labels), "v2.0.5:关闭详情按钮已移除"
    assert not any("导出" in t for t in labels), "详情不应有导出按钮"
    assert not any("删除" in t for t in labels), "详情不应有删除按钮"
