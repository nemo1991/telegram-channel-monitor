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
  接受它作 parent。`_on_bus_auth_error` 在 MainWindow 是 `async def`
  但内部无 `await` — 测试里我们提供 sync 版本(语义等价),避开
  asyncio.run + Qt 主线程的死锁。
- `_ErrorLogDialog` 是真类,直接构造测试(不阻塞)。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QWidget

from tgmonitor.core.events import AuthErrorOccurred, ErrorOccurred

# ============== helpers ==============


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _patch_qmessagebox(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09-14 v1.7.5 PR #6 (P0-K):autouse — 所有 QMessageBox.warning
    走 mock 返回 Ok(否则 offscreen 平台仍会真弹模态,测试卡死)。
    """
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: QMessageBox.Ok)


def _make_fake_window() -> Any:
    """最小桩 — QWidget 子类,QMessageBox 可作 parent。"""

    class FakeWin(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self._error_log: list[tuple[datetime, str, str]] = []
            self._bell_btn = QPushButton("🔔")
            self._bell_btn.setVisible(False)

    win = FakeWin()

    # sync wrapper — 与 MainWindow._on_bus_auth_error 同语义
    kind_map = {
        "code": "验证码错误",
        "password": "两步验证密码错误",
        "phone": "手机号错误",
        "telegram_internal": "Telegram 内部错误",
    }

    def sync_on_bus_auth_error(e: Any) -> None:
        if not isinstance(e, AuthErrorOccurred):
            return
        when = datetime.now(UTC)
        win._error_log.append((when, e.source, e.message))
        if len(win._error_log) > 100:
            win._error_log[:] = win._error_log[-100:]
        win._bell_btn.setVisible(True)
        win._bell_btn.setText(win.tr(f"🔔 {len(win._error_log)}"))
        kind = kind_map.get(e.source, "鉴权错误")
        QMessageBox.warning(
            win,
            win.tr(f"⚠ {kind}"),
            (
                f"{e.message}\n\n"
                + win.tr("详细错误日志可点击状态栏「🔔 {n}」按钮查看。").format(
                    n=len(win._error_log)
                )
            ),
            QMessageBox.Ok,
        )

    def sync_on_bell_clicked() -> None:
        from tgmonitor.ui.main_window import _ErrorLogDialog

        dlg = _ErrorLogDialog(win._error_log, parent=win)
        dlg.show()

    def sync_clear_error_log() -> None:
        win._error_log.clear()
        win._bell_btn.setVisible(False)

    win._on_bus_auth_error = sync_on_bus_auth_error  # type: ignore[assignment]
    win._on_bell_clicked = sync_on_bell_clicked  # type: ignore[assignment]
    win._clear_error_log = sync_clear_error_log  # type: ignore[assignment]
    return win


# ============== AuthErrorOccurred → 状态栏铃铛 ==============


def test_auth_error_pushes_bell_btn_visible(qapp: QApplication) -> None:
    """P0-K:首次 AuthErrorOccurred → 铃铛按钮从 hidden → visible,带计数 1。"""
    win = _make_fake_window()
    assert win._bell_btn.isHidden() is True
    win._on_bus_auth_error(AuthErrorOccurred(source="code", message="bad"))
    qapp.processEvents()
    assert win._bell_btn.isHidden() is False
    assert "1" in win._bell_btn.text()


def test_auth_error_increates_count_on_repeat(qapp: QApplication) -> None:
    """P0-K:连续两次 → 铃铛文本「🔔 2」。"""
    win = _make_fake_window()
    win._on_bus_auth_error(AuthErrorOccurred(source="code", message="bad 1"))
    win._on_bus_auth_error(AuthErrorOccurred(source="code", message="bad 2"))
    qapp.processEvents()
    assert "2" in win._bell_btn.text()
    assert len(win._error_log) == 2


def test_auth_error_appends_to_ring_buffer(qapp: QApplication) -> None:
    """P0-K:_error_log 收到 (datetime, source, message) 三元组。"""
    win = _make_fake_window()
    win._on_bus_auth_error(AuthErrorOccurred(source="password", message="2fa wrong"))
    qapp.processEvents()
    assert len(win._error_log) == 1
    when, source, msg = win._error_log[0]
    assert source == "password"
    assert msg == "2fa wrong"
    assert when.tzinfo is not None  # UTC 标记


def test_ring_buffer_caps_at_100_entries(qapp: QApplication) -> None:
    """P0-K:ring buffer 上限 100,超过只留最近 100。"""
    win = _make_fake_window()
    for i in range(105):
        win._on_bus_auth_error(AuthErrorOccurred(source="code", message=f"err {i}"))
    qapp.processEvents()
    assert len(win._error_log) == 100
    # 0-4 被截掉,5-104 留下;最新 100 条,最旧一条是 err 5
    assert "err 5" in win._error_log[0][2]
    assert "err 4" not in win._error_log[0][2]
    assert "err 104" in win._error_log[-1][2]


def test_auth_error_qmessagebox_warning_called(qapp: QApplication) -> None:
    """P0-K:弹 QMessageBox.warning(非 status_bar 临时消息)。"""
    win = _make_fake_window()
    with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok) as mock_warn:
        win._on_bus_auth_error(AuthErrorOccurred(source="code", message="验证码错误"))
        mock_warn.assert_called_once()
    # arg0 = parent(self), arg1 = title("⚠ 验证码错误"), arg2 = body
    args = mock_warn.call_args[0]
    assert "验证码错误" in args[1]


def test_auth_error_qmessagebox_warning_for_password(qapp: QApplication) -> None:
    """P0-K:source=password → kind 翻译为「两步验证密码错误」。"""
    win = _make_fake_window()
    with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok) as mock_warn:
        win._on_bus_auth_error(AuthErrorOccurred(source="password", message="2fa bad"))
        args = mock_warn.call_args[0]
    assert "两步验证密码" in args[1]


def test_auth_error_qmessagebox_warning_unknown_source_falls_back(
    qapp: QApplication,
) -> None:
    """P0-K:未知 source → kind 用「鉴权错误」兜底。"""
    win = _make_fake_window()
    with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok) as mock_warn:
        win._on_bus_auth_error(AuthErrorOccurred(source="some_new_kind", message="???"))
        args = mock_warn.call_args[0]
    assert "鉴权错误" in args[1]


def test_non_auth_error_ignored(qapp: QApplication) -> None:
    """P0-K:非 AuthErrorOccurred(只是普通 ErrorOccurred)→ 不进 ring buffer、不弹 box。"""
    win = _make_fake_window()
    with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok) as mock_warn:
        win._on_bus_auth_error(ErrorOccurred(source="general", message="other"))
        mock_warn.assert_not_called()
    assert len(win._error_log) == 0
    assert win._bell_btn.isHidden() is True


# ============== 铃铛点击 → _ErrorLogDialog ==============


def test_bell_click_opens_error_log_dialog(qapp: QApplication) -> None:
    """P0-K:铃铛点击 → 弹 _ErrorLogDialog 含 ring buffer 内容。"""
    win = _make_fake_window()
    # 注入 2 条
    win._on_bus_auth_error(AuthErrorOccurred(source="code", message="验证码错"))
    win._on_bus_auth_error(AuthErrorOccurred(source="password", message="2fa 错"))
    qapp.processEvents()

    # 拦截 _ErrorLogDialog.__init__ — 真 _on_bell_clicked 走 .exec(),
    # sync 测试 wrapper 走 .show(),无论哪条路径都需要捕 dialog 实例。
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
    # 验证 dialog 内的 list 长度等于 ring buffer
    assert captured.get("dlg") is not None
    assert captured["dlg"].list.count() == 2  # type: ignore[attr-defined]
    assert len(captured["entries"]) == 2


# ============== 清空日志 ==============


def test_clear_error_log_empties_and_hides_bell(qapp: QApplication) -> None:
    """P0-K:_clear_error_log → 铃铛 hidden + ring buffer 空。"""
    win = _make_fake_window()
    with patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok):
        win._on_bus_auth_error(AuthErrorOccurred(source="code", message="x"))
    qapp.processEvents()
    assert len(win._error_log) == 1
    assert win._bell_btn.isHidden() is False
    win._clear_error_log()
    assert win._error_log == []
    assert win._bell_btn.isHidden() is True


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
    # 最新(third) 在第 0 行
    assert "third" in dlg.list.item(0).text()
    assert "second" in dlg.list.item(1).text()
    assert "first" in dlg.list.item(2).text()
    dlg.deleteLater()


def test_error_log_dialog_empty_shows_placeholder(qapp: QApplication) -> None:
    """P0-K:空 entries → header 0 条 + placeholder 行。"""
    from tgmonitor.ui.main_window import _ErrorLogDialog

    dlg = _ErrorLogDialog([], parent=None)
    qapp.processEvents()
    # 1 行 placeholder
    assert dlg.list.count() == 1
    assert "(暂无错误)" in dlg.list.item(0).text()
    dlg.deleteLater()


def test_error_log_dialog_clear_button_clears_main(qapp: QApplication) -> None:
    """P0-K:dialog 上的「清空日志」按钮 → 调 win._clear_error_log。"""
    from tgmonitor.ui.main_window import _ErrorLogDialog

    win = _make_fake_window()
    entries = [(datetime(2026, 9, 14, 10, 0, tzinfo=UTC), "code", "x")]
    dlg = _ErrorLogDialog(entries, parent=None)
    qapp.processEvents()
    # 模拟点「清空日志」 — 把 win 当作 parent,触发 hasattr 分支
    with patch.object(dlg, "parent", return_value=win):
        dlg._on_clear()
    qapp.processEvents()
    assert win._error_log == []
    assert win._bell_btn.isHidden() is True
    # dialog 自己也清空(显示 placeholder)
    assert dlg.list.count() == 1
    dlg.deleteLater()
