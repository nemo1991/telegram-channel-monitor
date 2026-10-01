"""2026-09-14 v1.7.5 PR #6 (P0-K):鉴权错误入口测试。

覆盖:
- 收到 AuthErrorOccurred → ring buffer +1,铃铛按钮显示 + 计数
- 再次收到 → 计数自增
- _clear_error_log → 铃铛隐藏 + log 空
- _on_bell_clicked → 弹 _ErrorLogDialog(用 mock exec)
- 错误 source 映射到 kind 文案

实现说明:
- 真 MainWindow 构造需要 AppService 注入,太重。
- FakeWin = 继承 QWidget 的最小桩,继承只是为了让 QMessageBox.warning
  接受它作 parent。`_ErrorBellButton._on_bus_auth_error` 在 widget 内
  `async def` 但内部无 `await` — 测试里我们直接构造 widget。
- `_ErrorLogDialog` 是真类,直接构造测试(不阻塞)。

2026-10-01 v1.11.x 状态栏组件化重构:ring buffer / bell_btn 搬到
`StatusBar._ErrorBellButton`,`_on_bus_auth_error` handler 在 widget 内
直接订阅 EventBus。MainWindow `_on_bell_clicked` / `_clear_error_log`
从 `status_bar.get_error_log` / `status_bar.clear_error_log` 取/清。
本测试拆 widget 端直接测 + MainWindow 端 dialog 入口测。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

from tgmonitor.core.events import AuthErrorOccurred, ErrorOccurred  # noqa: E402

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
    `_ErrorLogDialog`;`_clear_error_log` 委托 `status_bar.clear_error_log`。
    """

    class FakeWin(QWidget):
        def __init__(self) -> None:
            super().__init__()
            from tgmonitor.ui.widgets.status_bar import StatusBar

            self.status_bar = StatusBar(_StubApp(), None)

        def _on_bell_clicked(self) -> None:
            from tgmonitor.ui.main_window import _ErrorLogDialog

            dlg = _ErrorLogDialog(self.status_bar.get_error_log(), parent=self)
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


def test_auth_error_pushes_bell_btn_visible(qapp: QApplication) -> None:
    """P0-K:首次 AuthErrorOccurred → 铃铛按钮从 hidden → visible,带计数 1。"""
    bell = _make_bell(qapp)
    assert bell.isHidden() is True
    _run_async(bell._on_bus_auth_error(AuthErrorOccurred(source="code", message="bad")))
    qapp.processEvents()
    assert bell.isHidden() is False
    assert "1" in bell.text()


def test_auth_error_increates_count_on_repeat(qapp: QApplication) -> None:
    """P0-K:连续两次 → 铃铛文本「🔔 2」。"""
    bell = _make_bell(qapp)
    _run_async(bell._on_bus_auth_error(AuthErrorOccurred(source="code", message="bad 1")))
    _run_async(bell._on_bus_auth_error(AuthErrorOccurred(source="code", message="bad 2")))
    qapp.processEvents()
    assert "2" in bell.text()
    assert len(bell.get_log()) == 2


def test_auth_error_appends_to_ring_buffer(qapp: QApplication) -> None:
    """P0-K:ring buffer 收到 (datetime, source, message) 三元组。"""
    bell = _make_bell(qapp)
    _run_async(bell._on_bus_auth_error(AuthErrorOccurred(source="password", message="2fa wrong")))
    qapp.processEvents()
    assert len(bell.get_log()) == 1
    when, source, msg = bell.get_log()[0]
    assert source == "password"
    assert msg == "2fa wrong"
    assert when.tzinfo is not None


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


def test_auth_error_qmessagebox_warning_called(qapp: QApplication) -> None:
    """P0-K:弹 QMessageBox.warning(非 status_bar 临时消息)。"""
    bell = _make_bell(qapp)
    with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok) as mock_warn:
        _run_async(bell._on_bus_auth_error(AuthErrorOccurred(source="code", message="验证码错误")))
        mock_warn.assert_called_once()
    args = mock_warn.call_args[0]
    assert "验证码错误" in args[1]


def test_auth_error_qmessagebox_warning_for_password(qapp: QApplication) -> None:
    """P0-K:source=password → kind 翻译为「两步验证密码错误」。"""
    bell = _make_bell(qapp)
    with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok) as mock_warn:
        _run_async(bell._on_bus_auth_error(AuthErrorOccurred(source="password", message="2fa bad")))
        args = mock_warn.call_args[0]
    assert "两步验证密码" in args[1]


def test_auth_error_qmessagebox_warning_unknown_source_falls_back(
    qapp: QApplication,
) -> None:
    """P0-K:未知 source → kind 用「鉴权错误」兜底。"""
    bell = _make_bell(qapp)
    with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok) as mock_warn:
        _run_async(
            bell._on_bus_auth_error(AuthErrorOccurred(source="some_new_kind", message="???"))
        )
        args = mock_warn.call_args[0]
    assert "鉴权错误" in args[1]


def test_non_auth_error_ignored(qapp: QApplication) -> None:
    """P0-K:非 AuthErrorOccurred(只是普通 ErrorOccurred)→ 不进 ring buffer、不弹 box。"""
    bell = _make_bell(qapp)
    with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok) as mock_warn:
        _run_async(bell._on_bus_auth_error(ErrorOccurred(source="general", message="other")))
        mock_warn.assert_not_called()
    assert bell.get_log() == []
    assert bell.isHidden() is True


# ============== 铃铛点击 → _ErrorLogDialog ==============


def test_bell_click_opens_error_log_dialog(qapp: QApplication) -> None:
    """P0-K:铃铛点击 → 弹 _ErrorLogDialog 含 ring buffer 内容。"""
    win = _make_fake_window(qapp)
    bell = win.status_bar._bell
    _run_async(bell._on_bus_auth_error(AuthErrorOccurred(source="code", message="验证码错")))
    _run_async(bell._on_bus_auth_error(AuthErrorOccurred(source="password", message="2fa 错")))
    qapp.processEvents()

    from tgmonitor.ui.main_window import _ErrorLogDialog

    captured: dict[str, Any] = {}
    orig_init = _ErrorLogDialog.__init__

    def spy_init(self: Any, entries: Any, parent: Any = None) -> None:
        captured["dlg"] = self
        captured["entries"] = entries
        orig_init(self, entries, parent)

    with (
        patch.object(_ErrorLogDialog, "__init__", spy_init),
        patch.object(_ErrorLogDialog, "show", return_value=None),
        patch.object(_ErrorLogDialog, "exec", return_value=0),
    ):
        win._on_bell_clicked()
    assert captured.get("dlg") is not None
    assert captured["dlg"].list.count() == 2  # type: ignore[attr-defined]
    assert len(captured["entries"]) == 2


# ============== 清空日志 ==============


def test_clear_error_log_empties_and_hides_bell(qapp: QApplication) -> None:
    """P0-K:_clear_error_log → 铃铛 hidden + ring buffer 空。"""
    win = _make_fake_window(qapp)
    bell = win.status_bar._bell
    with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok):
        _run_async(bell._on_bus_auth_error(AuthErrorOccurred(source="code", message="x")))
    qapp.processEvents()
    assert len(bell.get_log()) == 1
    assert bell.isHidden() is False
    win._clear_error_log()
    assert bell.get_log() == []
    assert bell.isHidden() is True


# ============== _ErrorLogDialog 直测 ==============


def test_error_log_dialog_lists_entries_in_reverse(qapp: QApplication) -> None:
    """P0-K:_ErrorLogDialog 倒序显示 — 最新在最上面。"""
    from tgmonitor.ui.main_window import _ErrorLogDialog

    entries = [
        (datetime(2026, 9, 14, 10, 0, 0, tzinfo=UTC), "code", "first"),
        (datetime(2026, 9, 14, 10, 5, 0, tzinfo=UTC), "code", "second"),
        (datetime(2026, 9, 14, 10, 10, 0, tzinfo=UTC), "password", "third"),
    ]
    dlg = _ErrorLogDialog(entries, parent=None)
    qapp.processEvents()
    assert dlg.list.count() == 3
    assert "third" in dlg.list.item(0).text()
    assert "second" in dlg.list.item(1).text()
    assert "first" in dlg.list.item(2).text()
    dlg.deleteLater()


def test_error_log_dialog_empty_shows_placeholder(qapp: QApplication) -> None:
    """P0-K:空 entries → header 0 条 + placeholder 行。"""
    from tgmonitor.ui.main_window import _ErrorLogDialog

    dlg = _ErrorLogDialog([], parent=None)
    qapp.processEvents()
    assert dlg.list.count() == 1
    assert "(暂无错误)" in dlg.list.item(0).text()
    dlg.deleteLater()


def test_error_log_dialog_clear_button_clears_main(qapp: QApplication) -> None:
    """P0-K:dialog 上的「清空日志」按钮 → 调 win._clear_error_log。"""
    from tgmonitor.ui.main_window import _ErrorLogDialog

    win = _make_fake_window(qapp)
    entries = [(datetime(2026, 9, 14, 10, 0, tzinfo=UTC), "code", "x")]
    dlg = _ErrorLogDialog(entries, parent=None)
    qapp.processEvents()
    with patch.object(dlg, "parent", return_value=win):
        dlg._on_clear()
    qapp.processEvents()
    bell = win.status_bar._bell
    assert bell.get_log() == []
    assert bell.isHidden() is True
    assert dlg.list.count() == 1
    dlg.deleteLater()
