"""底部状态栏:TG 通信状态标签(`_on_conn_state` 映射 + 更新)。

不构造完整 MainWindow(太重,需要 qloop + AppService + viewmodel),
直接对 `_on_conn_state` 做轻量单测:该方法只依赖 `self._conn_label`,
用一个最小桩对象即可覆盖全部状态文案映射。

来源:2026-08-13 需求 — 底部状态栏显示与 TG 的通信状态
(数据源 `updateConnectionState`,经 ConnectionStateChanged 事件桥接过来)。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from tgmonitor.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeWindow:
    """最小桩:只提供 `_on_conn_state` 需要的 `_conn_label`。"""

    def __init__(self) -> None:
        self._conn_label = QLabel("TG 未连接")


def test_conn_state_label_default(qapp) -> None:
    """初始文案是「TG 未连接」(状态未知前的兜底)。"""
    win = _FakeWindow()
    assert win._conn_label.text() == "TG 未连接"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("waiting_for_network", "TG 等待网络"),
        ("connecting", "TG 连接中…"),
        ("updating", "TG 同步中…"),
        ("ready", "TG 已连接"),
        ("unknown", "TG 状态未知"),
        ("some_new_state", "TG some_new_state"),
    ],
)
def test_conn_state_label_mapping(qapp, state: str, expected: str) -> None:
    """已知状态映射固定文案,未知状态兜底显示 `TG <state>`。"""
    win = _FakeWindow()
    MainWindow._on_conn_state(win, state)
    assert win._conn_label.text() == expected
