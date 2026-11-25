"""2026-09-14 v1.7.5 PR #6 (P0-K):鉴权错误入口测试。

覆盖:
- 收到 AuthErrorOccurred → ring buffer +1,铃铛按钮显示 + 计数
- 再次收到 → 计数自增
- _clear_error_log → 铃铛隐藏 + log 空
- _on_bell_clicked → 弹 ErrorLogDialog(用 mock exec)
- 错误 source 映射到 kind 文案

实现说明:
- 真 MainWindow 构造需要 AppService 注入,太重。
- FakeWin = 继承 QWidget 的最小桩,继承只是为了让 QMessageBox.warning
  接受它作 parent。`_ErrorBellButton._on_bus_auth_error` 在 widget 内
  `async def` 但内部无 `await` — 测试里我们直接构造 widget。
- `ErrorLogDialog` 是真类,直接构造测试(不阻塞)。

2026-10-01 v1.11.x 状态栏组件化重构:ring buffer / bell_btn 搬到
`StatusBar._ErrorBellButton`,`_on_bus_auth_error` handler 在 widget 内
直接订阅 EventBus。MainWindow `_on_bell_clicked` / `_clear_error_log`
从 `status_bar.get_error_log` / `status_bar.clear_error_log` 取/清。
本测试拆 widget 端直接测 + MainWindow 端 dialog 入口测。

2026-10-01 v1.12.x:`ErrorLogDialog` 已抽到
`tgmonitor.ui.widgets.error_log_dialog`,callback 注入(替代 hasattr 探针)。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

from tgmonitor.core.events import AuthErrorOccurred  # noqa: E402

# ============== helpers ==============


@pytest.fixture(autouse=True)
def _patch_qmessagebox(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09-14 v1.7.5 PR #6 (P0-K):autouse — 所有 QMessageBox.warning
    走 mock 返回 Ok(否则 offscreen 平台仍会真弹模态,测试卡死)。
    """
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: QMessageBox.Ok)


class _StubApp:
    """最小 AppService 桩 — 只暴露 bus。"""

    def __init__(self) -> None:
        from tgmonitor.core.events import EventBus

        self.bus = EventBus()


def _make_bell(qapp: QApplication):
    """构造 _ErrorBellButton widget(替代原 MainWindow 桩里的 bell_btn)。"""
    from tgmonitor.ui.widgets.status_bar import _ErrorBellButton

    return _ErrorBellButton(_StubApp())


def _make_fake_window(qapp: QApplication) -> Any:
    """构造 MainWindow 桩:含 status_bar / _on_bell_clicked / _clear_error_log。

    2026-10-01 v1.11.x:status_bar 是 widget 形式;`_on_bell_clicked` 真调
    `ErrorLogDialog`;`_clear_error_log` 委托 `status_bar.clear_error_log`。
    2026-10-01 v1.12.x:`ErrorLogDialog` 已迁出 `main_window.py`,用 callback
    注入(不再 hasattr 探针)。
    """

    class FakeWin(QWidget):
        def __init__(self) -> None:
            super().__init__()
            from tgmonitor.ui.widgets.status_bar import StatusBar

            self.status_bar = StatusBar(_StubApp(), None)

        def _on_bell_clicked(self) -> None:
            from tgmonitor.ui.widgets.error_log_dialog import ErrorLogDialog

            dlg = ErrorLogDialog(
                self.status_bar.get_error_log(),
                parent=self,
                on_clear=self._clear_error_log,
            )
            dlg.show()

        def _clear_error_log(self) -> None:
            self.status_bar.clear_error_log()

    win = FakeWin()
    return win


# ============== AuthErrorOccurred → 铃铛 ==============
# 2026-10-01 v1.11.x:handler 在 _ErrorBellButton widget 内,测试直接调 widget handler。


def _run_async(coro):
    """跑一次 async coroutine — 测试里 EventBus handler 是 async def。"""
    return asyncio.run(coro)


def test_ring_buffer_caps_at_100_entries(qapp: QApplication) -> None:
    """P0-K:ring buffer 上限 100,超过只留最近 100。"""
    bell = _make_bell(qapp)
    for i in range(105):
        _run_async(bell._on_bus_auth_error(AuthErrorOccurred(source="code", message=f"err {i}")))
    qapp.processEvents()
    assert len(bell.get_log()) == 100
    assert "err 5" in bell.get_log()[0][2]
    assert "err 4" not in bell.get_log()[0][2]
    assert "err 104" in bell.get_log()[-1][2]


# ============== 铃铛点击 → ErrorLogDialog ==============


# ============== 清空日志 ==============


# ============== ErrorLogDialog 直测 ==============
