"""`MediaActionsController.on_media_deleted` 删除反馈可见性回归。

2026-09-28 用户反馈:Media Manager 点「删除」无反应 — row 不消失、状态栏
无任何提示,体感「点了没反应」。根因:`vm.media_deleted` 事件无 UI 订阅,
单条删除走完 `MediaDeleted` 事件没人接;批量删除靠末尾手动调
`on_media_refresh()`,但 `vm.delete_media_batch` 是 fire-and-forget,refresh
在 `_go` 跑完前就发了 → 拿到的还是旧 storage。

本测试守住四条边界:
  1. `on_media_deleted` 必须:更新活动指示器 + 触发 widget refresh
  2. 100ms debounce timer 合并:连续 N 条 MediaDeleted → 单次 widget refresh
  3. 非 MediaDeleted 类型(slot 边界)直接 return,不抛、不刷
  4. activity 文案走 throttle,批量 100 条不会刷成流水账

2026-10-04 plan 阶段 3.3:handler 从 MainWindow 抽到 MediaActionsController。
测试改直接构造 controller + mock ctx(沿用 test_search_controller.py /
test_live_selection_controller.py 模板);`_FakeWindow` 仍提供 activity
label + debounce QTimer 桩。
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402

from tgmonitor.core.events import MediaDeleted, MessageDeleted  # noqa: E402
from tgmonitor.ui.controllers._base import MainWindowCtx  # noqa: E402
from tgmonitor.ui.controllers.media_actions_controller import (  # noqa: E402
    MediaActionsController,
)

# 2026-10-22 v1.12.3:本文件曾因 Qt offscreen paint path race 挂了两年标记
# (`xfail` → `skipif`),现在标记全部拆掉,原因是**真 bug 被标记盖住了**,
# 不是 race 修好了。
#
# 时间线:
# - 2026-09-28 v1.9.2:3 个 test 用 `QEventLoop().exec()` 等 debounce QTimer
#   到点,Qt offscreen paint path race 触发 native SIGSEGV → 挂
#   `xfail(strict=False)`
# - 2026-10-22 v1.12.3 发版:xfail 被发现**拦不住** native segfault(进程级
#   崩溃,pytest 拿不到 exception,CI 直接 exit 139)。GH Actions 在 10-10 /
#   10-12 / 10-22 三次把 CI 打红,10-12 那次三平台全红。改成 `skipif` 后 CI 绿了。
# - 同一个 commit 里追下去发现真因:**本文件从未接
#   `media_refresh_debounce.timeout → flush_media_refresh_pending`**
#   (生产在 `ui/main_window.py:424`),所以 `refresh_requested` 永远不会被
#   emit,`assert emit_count == 1` 是一条**永远不成立**的断言。本地它报
#   xfailed(没人看),CI 上先在 `loop.exec()` 崩掉(exit 139,更没人看)——
#   两层标记把同一个真 bug 盖了两年。
#
# 修法(两处):
# 1. `_build_ctrl` 补上生产里那条 `timeout.connect(ctrl.flush_media_refresh_pending)`
# 2. 3 个 test 用 `isActive()` 验 start + **同步 `timeout.emit()`** 验到点,
#    不再挂钟等 100ms、不再进任何事件循环
#
# `QEventLoop().exec()` 在本文件已彻底消失 —— segfault 的触发条件不存在了。
# 同类写法见 `test_status_bar.py`(asyncio.run + processEvents)与
# `test_media_manager_i18n.py`,那些仍在 skip,根因不同,各自单独跟踪。


class _FakeSignal:
    """可连接的伪 Qt Signal — 简单 callable 列表。

    测试只关心 `emit()` 是否被调到,不需要真 QObject metaclass。
    """

    def __init__(self) -> None:
        self._callbacks: list = []
        self.emit_count = 0

    def connect(self, cb) -> None:
        self._callbacks.append(cb)

    def emit(self) -> None:
        self.emit_count += 1
        for cb in list(self._callbacks):
            cb()


class _FakeMediaManager:
    """最小 MediaManager 桩 — 只暴露 `refresh_requested` 触发计数。"""

    def __init__(self) -> None:
        self.refresh_requested = _FakeSignal()
        self.refresh_requested.connect(self._on_refresh)

    def _on_refresh(self) -> None:
        pass  # 计 emit_count 在 _FakeSignal 上


class _FakeActivityLabel:
    """QObject 子集。ActvityLabel — 子组件 `setText` / `text()`。"""

    def __init__(self) -> None:
        self._text = ""

    def setText(self, text: str) -> None:  # noqa: N802 — Qt API 命名
        self._text = text

    def text(self) -> str:
        return self._text


class _FakeWindow:
    """最小 MainWindow 桩 — 覆盖 `_FakeActivityLabel` + `media_manager` +
    `_media_refresh_debounce` QTimer。

    2026-10-04 plan 阶段 3.3:`on_media_deleted` 已移到 `MediaActionsController`,
    测试通过 `MediaActionsController(ctx).on_media_deleted(e)` 触发。
    """

    def __init__(self) -> None:
        self._activity_label = _FakeActivityLabel()
        self._activity_throttle: dict[str, float] = {}
        self.status_bar = _FakeStatusBar(self)
        self.media_manager = _FakeMediaManager()

        # 与 `on_media_deleted` 配的真 QTimer(debounce 行为依赖 Qt 事件循环,
        # offscreen 平台照常工作)
        self._media_refresh_debounce = QTimer()
        self._media_refresh_debounce.setSingleShot(True)
        self._media_refresh_debounce.setInterval(100)


class _FakeStatusBar:
    """StatusBar 子集 — 只暴露 activity throttle / show delegate。"""

    def __init__(self, owner: _FakeWindow) -> None:
        self._owner = owner

    def show_activity(self, text: str, *, timeout_ms=None) -> None:
        self._owner._activity_label.setText(text)

    def throttle_activity(
        self,
        key: str,
        text: str,
        min_interval_ms: int,
        timeout_ms: int | None = None,
    ) -> None:
        # 复用旧 MainWindow 节流逻辑
        import time as _time

        now = _time.monotonic() * 1000.0
        last = self._owner._activity_throttle.get(key, 0.0)
        if now - last < min_interval_ms:
            return
        self._owner._activity_throttle[key] = now
        self.show_activity(text, timeout_ms=timeout_ms)


def _build_ctrl(win: _FakeWindow) -> MediaActionsController:
    """构造 `MediaActionsController` + ctx 引用 `_FakeWindow` 提供的 status_bar
    / media_manager / debounce timer。"""
    ctx = MainWindowCtx(
        app=MagicMock(name="app"),
        monitor=MagicMock(name="monitor"),
        loop=MagicMock(name="loop"),
        vm=MagicMock(name="vm"),
        env_path=MagicMock(name="env_path"),
        main_window=win,
        window_title_setter=MagicMock(name="window_title_setter"),
        quit_app=MagicMock(name="quit_app"),
        live_view=MagicMock(name="live_view"),
        # 2026-10-15 plan-batch7-C-α:LiveActionsController 注入 ctx
        live_actions=MagicMock(name="live_actions"),
        message_detail=MagicMock(name="message_detail"),
        media_manager=win.media_manager,
        selection_toolbar=MagicMock(name="selection_toolbar"),
        dashboard=MagicMock(name="dashboard"),
        channel_panel=MagicMock(name="channel_panel"),
        header=MagicMock(name="header"),
        status_bar=win.status_bar,
        nav=MagicMock(name="nav"),
        stack=MagicMock(name="stack"),
        tray=None,
        search_debounce=MagicMock(name="search_debounce"),
        media_refresh_debounce=win._media_refresh_debounce,
    )
    ctrl = MediaActionsController(ctx)
    # 2026-10-22 v1.12.3:补上生产里 `ui/main_window.py:424` 的那条连接 ——
    # 本文件**从未**接 `media_refresh_debounce.timeout → flush_media_refresh_pending`,
    # 于是 refresh_requested 永远不会被 emit,三条 test 里那句
    # `assert emit_count == 1` 是一条**永远不成立**的断言。它被 xfail 标记吞了:
    # 本地报 xfailed(没人看),CI 上先在 `loop.exec()` 崩掉 exit 139(更没人看),
    # 两边都盖住了真因。GH Actions 在 2026-10-10 / 10-12 / 10-22 三次红都是它。
    win._media_refresh_debounce.timeout.connect(ctrl.flush_media_refresh_pending)
    return ctrl


# ============================================================
# `on_media_deleted` 行为
# ============================================================


def test_on_media_deleted_triggers_widget_refresh(qapp) -> None:
    """`on_media_deleted` 必须经 debounce timer 触发 widget refresh。

    用户报告:点 Delete 后 row 不消失。这是 v1.9.1 修(delete-no-feedback)
    的根因边界。

    2026-10-22 v1.12.3:原本这里嵌套 `QEventLoop().exec()` 等 150ms 让 debounce
    到点 —— Qt offscreen paint path race 在这里崩(native segfault,CI 三平台都
    中招)。改成**同步 emit timeout**:debounce 的语义是「start() 后到点才刷」,
    用 `isActive()` 验 start、用 `timeout.emit()` 验到点后的行为,不需要真的
    挂钟等 100ms,也不需要任何事件循环。
    """
    win = _FakeWindow()
    ctrl = _build_ctrl(win)
    e = MediaDeleted(channel_id=100, telegram_msg_id=5, media_idx=0)
    ctrl.on_media_deleted(e)

    # debounce timer 已启动(interval=100 / singleShot)
    assert win._media_refresh_debounce.isActive()
    # 到点前 refresh 未触发
    assert win.media_manager.refresh_requested.emit_count == 0
    # 到点 → 单次 widget refresh
    win._media_refresh_debounce.timeout.emit()
    assert win.media_manager.refresh_requested.emit_count == 1


def test_on_media_deleted_shows_activity_toast(qapp) -> None:
    """`on_media_deleted` 必须更新活动指示器 — 给用户视觉反馈。"""
    win = _FakeWindow()
    ctrl = _build_ctrl(win)
    ctrl.on_media_deleted(MediaDeleted())
    assert "已删除" in win._activity_label.text()


def test_on_media_deleted_non_mediadeleted_type_is_ignored(qapp) -> None:
    """`on_media_deleted` 收到非 `MediaDeleted` 实例 → 直接 return。

    守住 slot 边界:EventBus 也会推 `MessageDeleted`(整条消息删除,不是
    单条 media),两者不应混淆。本 slot 只关心 per-media 删除。
    """
    win = _FakeWindow()
    ctrl = _build_ctrl(win)
    ctrl.on_media_deleted(MessageDeleted(channel_id=100, telegram_msg_id=5))
    # 不更新 activity、不启 debounce
    assert win._activity_label.text() == ""
    assert win.media_manager.refresh_requested.emit_count == 0


def test_on_media_deleted_batch_debounces_to_single_refresh(qapp) -> None:
    """批量 N 条连续 MediaDeleted → debounce timer 合并为单次 widget refresh。

    验证 100ms debounce 工作:连续 5 条 delete 之间 timer 不应到点(假设
    测试在 50ms 内全发完),最终等 100ms 后只看到 1 次 refresh emit。

    2026-10-22 v1.12.3:同步 emit timeout 版(同上一个 test),不挂钟不事件循环。
    """
    win = _FakeWindow()
    ctrl = _build_ctrl(win)
    # 5 条连续 delete — 每次都 restart debounce timer
    for i in range(5):
        ctrl.on_media_deleted(
            MediaDeleted(channel_id=100, telegram_msg_id=i + 1, media_idx=0),
        )
        # 每次都保持 active(start() 在已运行的 timer 上自动重置 interval)
        assert win._media_refresh_debounce.isActive()
    # timer 重启但还没到点 → refresh 未触发
    assert win.media_manager.refresh_requested.emit_count == 0

    # 5 条 delete → 单次 refresh(不是 5 次)
    win._media_refresh_debounce.timeout.emit()
    assert win.media_manager.refresh_requested.emit_count == 1


def test_on_media_deleted_throttles_activity_messages(qapp) -> None:
    """批量 100 条连续 delete → 活动文案不刷成「已删除 媒体 × 100」。

    `_throttle_activity("media_delete", …)` min_interval_ms=500,
    100 条连续 delete(总耗时 < 500ms)只会更新一次 activity label。
    """
    win = _FakeWindow()
    ctrl = _build_ctrl(win)
    first_label_text = ""
    for i in range(100):
        ctrl.on_media_deleted(
            MediaDeleted(channel_id=100, telegram_msg_id=i + 1, media_idx=0),
        )
        if i == 0:
            first_label_text = win._activity_label.text()
    # 第一条设置后,后续 99 条应被 throttle 拦住 — label 文字不变
    assert win._activity_label.text() == first_label_text
    assert "已删除" in first_label_text

    # timer 到点(无新增 delete,会真刷)→ 同步 emit,不走事件循环
    win._media_refresh_debounce.timeout.emit()
    assert win.media_manager.refresh_requested.emit_count == 1
