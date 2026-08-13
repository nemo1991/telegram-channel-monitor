"""LoginDialog 提交 loading 锁定 / 解锁 的 UI 行为测试。

需求(2026-08-13):
  用户在登录 / 提交手机号 / 提交验证码后,等待响应期间应看到 loading 状态,
  且不允许重复提交。

覆盖:
  - `_submit_*` 提交期间 `_busy=True`:按钮 + 输入框禁用,status_label 显示 loading 文案
  - busy 期间重复调用 `_submit_*` 直接返回,不触发第二次提交
  - 响应返回后自动解锁(成功切页 / 失败显示错误文案,均不隐藏对话框)
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import AsyncMock, Mock  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.ui.widgets.login_dialog import LoginDialog  # noqa: E402


class _LoopThread:
    """后台线程跑一个持续运行的 asyncio loop — 模拟 qasync 的 QEventLoop。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def qloop() -> asyncio.AbstractEventLoop:
    """后台线程 + run_forever loop — 模拟 qasync 主线程 loop。"""
    lt = _LoopThread()
    yield lt.loop
    lt.loop.call_soon_threadsafe(lt.loop.stop)
    lt._thread.join(timeout=2.0)
    try:
        lt.loop.close()
    except Exception:  # noqa: BLE001
        pass


def _make_dlg(qloop, *, submit_phone=None):
    """构造一个可交互的 LoginDialog(mock app + 后台 loop)。"""
    mock_app = Mock()
    mock_app.settings.phone = ""
    mock_app.client.state = "phone_required"
    if submit_phone is None:
        mock_app.submit_phone = AsyncMock(return_value=("code_required", None))
    else:
        mock_app.submit_phone = AsyncMock(side_effect=submit_phone)
    dlg = LoginDialog(app=mock_app, loop=qloop)
    return dlg, mock_app


def _wait_until(pred, *, timeout: float = 3.0, step: float = 0.02) -> None:
    """主线程轮询等待 pred() 为真(配合后台 loop 的 done callback)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        QApplication.processEvents()
        time.sleep(step)
    raise AssertionError(f"pred() 未在 {timeout:.1f}s 内满足")


def test_submit_phone_busy_locks_then_unlocks(qapp, qloop):
    """提交手机号期间锁定输入 + 显示 loading;响应返回后解锁并切到验证码页。"""
    gate = asyncio.Event()

    async def submit_phone(phone: str):
        await gate.wait()
        return ("code_required", None)

    dlg, mock_app = _make_dlg(qloop, submit_phone=submit_phone)
    dlg.in_phone.setText("+8613800000000")
    dlg._submit_phone()
    # 等后台 loop 真正开始执行 coroutine(AsyncMock 计数发生在 coroutine 启动时)
    _wait_until(lambda: mock_app.submit_phone.await_count == 1)

    # 提交期间:busy + 控件禁用 + loading 文案
    assert dlg._busy is True
    assert dlg.btn_submit.isEnabled() is False
    assert dlg.in_phone.isEnabled() is False
    assert "正在请求验证码" in dlg.status_label.text()

    # busy 期间重复提交被拦截,不二次调用
    dlg._submit_phone()
    assert mock_app.submit_phone.await_count == 1

    # 放行响应 → done callback 解锁 + 切到验证码页
    # 注:asyncio.Event 是 loop-bound,主线程直接 set() 唤醒不了后台 loop 的
    # wait();必须 call_soon_threadsafe 让 set 在后台 loop 线程执行。
    qloop.call_soon_threadsafe(gate.set)
    _wait_until(lambda: dlg._busy is False)
    assert dlg.btn_submit.isEnabled() is True
    assert dlg.stack.currentIndex() == 1  # 验证码页
    assert dlg.status_label.text() == "Telegram 验证码"


def test_submit_code_busy_blocks_reentry(qapp, qloop):
    """提交验证码期间同样锁定,重复点击不触发第二次提交。"""
    gate = asyncio.Event()

    async def submit_code(code: str):
        await gate.wait()
        return ("ready", None)

    mock_app = Mock()
    mock_app.settings.phone = ""
    mock_app.client.state = "code_required"
    mock_app.submit_code = AsyncMock(side_effect=submit_code)
    dlg = LoginDialog(app=mock_app, loop=qloop)
    dlg.in_code.setText("12345")
    dlg._submit_code()
    _wait_until(lambda: mock_app.submit_code.await_count == 1)

    assert dlg._busy is True
    assert dlg.btn_submit.isEnabled() is False
    assert "正在验证" in dlg.status_label.text()

    dlg._submit_code()
    dlg._on_submit()
    assert mock_app.submit_code.await_count == 1

    qloop.call_soon_threadsafe(gate.set)
    _wait_until(lambda: dlg._busy is False)
    assert dlg.btn_submit.isEnabled() is True


def test_submit_error_shows_message_keeps_page(qapp, qloop):
    """提交返回 ('error', msg) 时显示错误文案并保持当前页,不隐藏对话框。"""
    msg = "TG_API_ID 未配置:请打开 设置… 填写"

    def submit_phone(phone: str):
        return ("error", msg)

    dlg, _ = _make_dlg(qloop, submit_phone=submit_phone)
    dlg.in_phone.setText("+8613800000000")
    dlg._submit_phone()

    _wait_until(lambda: dlg._busy is False)
    assert "登录失败" in dlg.status_label.text()
    assert msg in dlg.status_label.text()
    assert dlg.stack.currentIndex() == 0  # 保持手机号页,可重试
    assert dlg.btn_submit.isEnabled() is True


def test_submit_exception_shows_error(qapp, qloop):
    """run_coro 异常路径:对话框内显示失败原因,不隐藏。"""
    async def boom(phone: str):
        raise RuntimeError("boom")

    dlg, _ = _make_dlg(qloop, submit_phone=boom)
    dlg.in_phone.setText("+8613800000000")
    dlg._submit_phone()

    _wait_until(lambda: dlg._busy is False)
    assert "提交手机号失败" in dlg.status_label.text()
    assert "boom" in dlg.status_label.text()
    assert dlg.btn_submit.isEnabled() is True
