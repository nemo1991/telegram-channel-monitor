"""PR #A3 — ExportProgressDialog 行为测试(信号连接 / 断开 / 进度显示)。"""

from __future__ import annotations

import os

# offscreen:跑测试不弹真窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication

from tgmonitor.core.events import ExportProgress
from tgmonitor.ui.widgets.export_progress_dialog import ExportProgressDialog

# `qapp` from tests/conftest.py — session-scope,多文件复用单例


@pytest.fixture
def vm() -> MagicMock:
    """构造 mock VM — export_progress signal 用 MagicMock 模拟。

    Mock 的 connect/disconnect 不真正生效,但 dialog 代码只调
    connect/disconnect — 不会触发 callback,所以 mock 足够。
    """
    return MagicMock()


def test_dialog_progress_handler_accepts_total_none(qapp: QApplication, vm: MagicMock) -> None:
    """ExportProgress.total=None → QProgressBar 走 indeterminate(max=0)。"""
    dlg = ExportProgressDialog(vm)
    e = ExportProgress(request_id="r1", written=500, total=None)
    dlg._on_progress(e)

    assert dlg.bar.maximum() == 0  # indeterminate
    assert "500" in dlg.lbl_status.text()
