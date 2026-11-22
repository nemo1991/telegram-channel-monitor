"""底部状态栏:TG 通信状态标签(`_on_conn_state` 映射 + 更新)。

不构造完整 MainWindow(太重,需要 qloop + AppService + viewmodel),
直接对 `_on_conn_state` 做轻量单测:该方法只委托 `status_bar.set_connection_state`,
用一个最小桩对象即可覆盖全部状态文案映射。

来源:2026-08-13 需求 — 底部状态栏显示与 TG 的通信状态
(数据源 `updateConnectionState`,经 ConnectionStateChanged 事件桥接过来)。

2026-10-01 v1.11.x 状态栏组件化重构:`_conn_label` 已迁移到 StatusBar,
MainWindow `_on_conn_state` 只委托 setter;本测试断言 setter 透传。
2026-11-17 v2.0.3 D2:连接态与登录态合并为 `_UnifiedStateLabel`;本桩只用
`_conn_state_label` 翻译表验证文案映射,不受组件改名影响。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QLabel  # noqa: E402

from tgmonitor.ui.main_window import MainWindow  # noqa: E402


class _FakeWindow:
    """最小桩:只提供 `_on_conn_state` 需要的 `status_bar` + activity delegate。

    2026-10-01 v1.11.x:`_on_conn_state` 只委托 `status_bar.set_connection_state`,
    且同时调 `status_bar.show_activity`(原 `_show_activity`)— 桩要带这两字段。
    2026-10-09 v1.12.x:`_on_conn_state` 改成 thin delegate `AppStateController`
    桩要持 `_app_state_ctrl` 委托给 status_bar(本测试只关心
    `set_connection_state` 的 label 映射,activity 走 controller 流程即可)。
    """

    def __init__(self) -> None:
        self.status_bar = _FakeStatusBar()
        # 2026-10-09:thin delegate 转给一个最小 controller 桩,直接调 status_bar。
        from types import SimpleNamespace

        self._app_state_ctrl = SimpleNamespace(
            on_conn_state=lambda state: self.status_bar.set_connection_state(state),
        )

    _on_conn_state = MainWindow._on_conn_state


class _FakeStatusBar:
    """StatusBar 子集 — 只暴露 conn setter + show_activity delegate。"""

    def __init__(self) -> None:
        self._conn_label = QLabel("TG 未连接")

    def set_connection_state(self, state: str) -> None:
        from tgmonitor.ui.widgets.status_bar import _conn_state_label

        self._conn_label.setText(_conn_state_label(state))

    def show_activity(self, text: str, *, timeout_ms=None) -> None:
        return None


def test_conn_state_label_default(qapp) -> None:
    """初始文案是「TG 未连接」(状态未知前的兜底)。"""
    win = _FakeWindow()
    assert win.status_bar._conn_label.text() == "TG 未连接"


def test_on_conn_state_delegates_to_status_bar(qapp) -> None:
    """thin delegate:`MainWindow._on_conn_state` 经 `_app_state_ctrl` 透传到
    `status_bar.set_connection_state`(本文件独有的守护;状态文案映射表由
    `test_status_bar.py::test_conn_state_label_mapping` 覆盖,不在此重复)。
    """
    win = _FakeWindow()
    MainWindow._on_conn_state(win, "ready")
    assert win.status_bar._conn_label.text() == "TG 已连接"
