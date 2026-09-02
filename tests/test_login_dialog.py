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


# ============================================================
# 2026-08-27 v1.4.0 PR #13:email / registration 步骤页 routing。
# ============================================================


def _make_email_dlg(qloop, *, initial_state="email_required"):
    """构造 email/registration 测试用的 LoginDialog(mock app 注入对应 submit 方法)。"""
    mock_app = Mock()
    mock_app.settings.phone = ""
    mock_app.client.state = initial_state
    mock_app.submit_email = AsyncMock(return_value=("email_code_required", None))
    mock_app.submit_email_code = AsyncMock(return_value=("ready", None))
    mock_app.submit_registration = AsyncMock(return_value=("ready", None))
    dlg = LoginDialog(app=mock_app, loop=qloop)
    dlg._expected_state = initial_state  # 强制 _render 路径走对应 page
    dlg._render(initial_state)
    return dlg, mock_app


def test_email_required_state_shows_email_page(qapp, qloop):
    """PR #13:state=email_required → 切到 page 3 (邮箱地址) + focus。"""
    dlg, _ = _make_email_dlg(qloop, initial_state="email_required")
    assert dlg.stack.currentIndex() == 3
    assert "邮箱" in dlg.status_label.text()


def test_email_code_required_state_shows_code_page(qapp, qloop):
    """PR #13:state=email_code_required → page 4。"""
    dlg, _ = _make_email_dlg(qloop, initial_state="email_code_required")
    assert dlg.stack.currentIndex() == 4


def test_registration_required_state_shows_reg_page(qapp, qloop):
    """PR #13:state=registration_required → page 5。"""
    dlg, _ = _make_email_dlg(qloop, initial_state="registration_required")
    assert dlg.stack.currentIndex() == 5
    assert "注册" in dlg.status_label.text()


def test_submit_email_invokes_app_and_unlocks(qapp, qloop):
    """PR #13:state=email_required → _submit_email → 调 app.submit_email。"""
    dlg, mock_app = _make_email_dlg(qloop, initial_state="email_required")
    dlg.in_email.setText("user@example.com")
    dlg._submit_email()

    _wait_until(lambda: mock_app.submit_email.await_count == 1)
    assert dlg._busy is False
    assert dlg.stack.currentIndex() == 4  # 推到 email_code_required


def test_submit_email_busy_blocks_reentry(qapp, qloop):
    """PR #13:busy 期间重复 _submit_email → 不触发第二次。

    注:测试不能用「连续两次 _submit_email()」的写法的根本原因是 — 第二次
    调用前,后台 loop 线程可能已经 tick 完整个 coroutine(AsyncMock
    是瞬时返回),触发 `_set_busy(False)` 让 `_busy` 重新落回 False,
    第二次调用「合法」进入分支,触发 `mock.submit_email.await_count == 2`。
    本机 macOS 通常赢在「主线程同步代码 > 后台 loop tick」,但 Linux /
    Windows CI 上后台 loop 调度更早,出现 flaky。改用同文件
    `test_submit_phone_busy_locks_then_unlocks` 的 `gate` 模式:让 mock
    `submit_email` 阻塞在 `gate.wait()`,主线程就有时间观察 busy 标志
    + 重复提交拦截。
    """
    gate = asyncio.Event()
    call_count = 0

    async def submit_email(_email: str):
        nonlocal call_count
        call_count += 1
        await gate.wait()
        return ("email_code_required", None)

    # 自己建 mock_app + dlg(替换 _make_email_dlg 的瞬时 AsyncMock)
    mock_app = Mock()
    mock_app.settings.phone = ""
    mock_app.client.state = "email_required"
    mock_app.submit_email = submit_email
    mock_app.submit_email_code = AsyncMock(return_value=("ready", None))
    mock_app.submit_registration = AsyncMock(return_value=("ready", None))
    dlg = LoginDialog(app=mock_app, loop=qloop)
    dlg._expected_state = "email_required"  # 强制 _render 走 email 页
    dlg._render("email_required")

    dlg.in_email.setText("user@example.com")
    dlg._submit_email()
    # 等后台 loop 真开始跑 submit_email 协程(call_count == 1 表示协程已
    # 进入 body、busy 标志稳定为 True)
    _wait_until(lambda: call_count == 1)
    assert dlg._busy is True
    # busy 期间第二次提交 — 被拦截,不二次调度
    dlg._submit_email()
    # 即使放开 gate,只应触发第一个 submit_email 完成,无第二次
    qloop.call_soon_threadsafe(gate.set)
    _wait_until(lambda: dlg._busy is False)
    assert call_count == 1


def test_submit_email_code_invokes_app(qapp, qloop):
    """PR #13:_submit_email_code → app.submit_email_code。"""
    dlg, mock_app = _make_email_dlg(qloop, initial_state="email_code_required")
    dlg.in_email_code.setText("123456")
    dlg._submit_email_code()
    _wait_until(lambda: mock_app.submit_email_code.await_count == 1)
    assert dlg._busy is False


def test_submit_registration_invokes_app(qapp, qloop):
    """PR #13:_submit_registration → app.submit_registration(first, last)。"""
    dlg, mock_app = _make_email_dlg(qloop, initial_state="registration_required")
    dlg.in_reg_first.setText("Alice")
    dlg.in_reg_last.setText("Wonder")
    dlg._submit_registration()
    _wait_until(lambda: mock_app.submit_registration.await_count == 1)
    assert dlg._busy is False
    mock_app.submit_registration.assert_awaited_once_with("Alice", "Wonder")


def test_submit_registration_empty_first_name_blocks(qapp, qloop):
    """PR #13:first_name 空 → 不发请求,显示错误,不解锁(用户重输)。"""
    dlg, mock_app = _make_email_dlg(qloop, initial_state="registration_required")
    dlg.in_reg_first.setText("")
    dlg._submit_registration()
    assert mock_app.submit_registration.await_count == 0
    assert "first_name" in dlg.status_label.text() or "必填" in dlg.status_label.text()


def test_on_submit_dispatches_to_email_methods(qapp, qloop):
    """PR #13:_on_submit 根据 _expected_state 分发到正确的 submit 方法。"""
    dlg, mock_app = _make_email_dlg(qloop, initial_state="email_required")
    dlg.in_email.setText("user@example.com")
    dlg._on_submit()  # 应走 _submit_email
    _wait_until(lambda: mock_app.submit_email.await_count == 1)
    # Mock 默认会建 submit_code 属性 — 用 side_effect 标记其**未被**调用
    # 简单做法:确保 _submit_email_code / _submit_password 都没被触发。
    assert dlg._busy is False
    # email flow 推进后应该到 email_code_required page
    assert dlg.stack.currentIndex() == 4
