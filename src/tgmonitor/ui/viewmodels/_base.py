# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""BusMixin — EventBus handler 自动挂载协议 + facade `__init__` 接管 — 2026-10-20 plan-batch9-η。

5 mixin + 1 入口壳拆 `viewmodels/monitor_vm.py` 839 LOC,BusMixin 持协议
骨架 + 单点 init 收口(plan §8 #2 「单点 init 收敛」):
- `_BUS_HANDLERS: ClassVar[dict[type[Event], str]]` — 子 mixin 在类体
  填 `event_type → handler_method_name` 映射(集中声明,优于原
  `b.subscribe(X, self._on_X)` × 21 行)
- `wire_bus()` — 走 `type(self).__mro__` walk 累加挂载;handler 名错
  立即 AttributeError 在 `wire_bus()` 调时即失败(显式优于静默漏挂)
- `__init__()` — 2026-10-20 plan-batch9-η 起接管 facade 初始化:
  `super().__init__()` 收口 QObject → 赋 3 facade 实例引用 →
  `self.wire_bus()` 触发自动 wire 21 events。3 字段(known_channels /
  _export_task / _message_count)由各自 mixin 类级声明提供 default 初值,
  `__init__` 不重复赋值以避免「facade init 跨 mixin 写字段」违反
  字段同 mixin 持有原则。

具体业务 mixin(`_channels` / `_system` / `_messages` / `_media` /
`_export`)从 C-β 起各自填 `_BUS_HANDLERS`,C-η 起各自类级声明字段。

沿用 plan-batch7-goal2 `MessageListModel 5 mixin + QAbstractListModel`
多继承惯例:5 mixin 在前,BusMixin 在 QObject 之前(`super().__init__()`
一处收口)。

不引(2026-10-19 plan-batch9 §6 风险 #6):5 个 mixin 文件之间互不 import,
共享类型只走 `tgmonitor.core.events` 顶层,避免循环。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.dto import ChannelDTO
    from tgmonitor.core.events import Event, EventBus
    from tgmonitor.core.monitor.service import MonitorService


class BusMixin:
    """EventBus handler 自动挂载协议 + facade 初始化收口 — 2026-10-20 plan-batch9-η。

    子 mixin 在类体填 `_BUS_HANDLERS = {EventType: "handler_name"}`,
    `wire_bus()` 走 `type(self).__mro__` walk 累加(防止子类 dict 覆盖
    父类 dict 丢注册),用 `getattr(self, name)` 拿 bound method 挂到
    `EventBus.subscribe`。handler 名错 → `AttributeError` 在
    `wire_bus()` 调时即失败(显式优于静默漏挂)。

    MRO 倒序 walk 是从基类往子类走(基类 stub dict 空 → 各 mixin dict
    合入 → QObject 不在 dict 链路,自动跳过)— 顺序无关紧要,因为
    每个 mixin 的 `_BUS_HANDLERS` 互斥,walk 累加 = 全集。

    `__init__` 2026-10-20 plan-batch9-η 起接管 facade 初始化 — `super().__init__()`
    走 QObject → 赋 3 facade 实例引用(`app` / `monitor` / `loop`) →
    `self.wire_bus()` 触发自动 wire 21 events。3 字段(known_channels /
    _export_task / _message_count)由各自 mixin 类级声明提供 default
    初值,本 `__init__` 不重复赋值以避免 facade init 跨 mixin 写字段。
    """

    _BUS_HANDLERS: ClassVar[dict[type[Event], str]] = {}

    def __init__(
        self,
        app: AppService,
        monitor: MonitorService,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """2026-10-20 plan-batch9-η:`MonitorViewModel.__init__` 接管。

        5 mixin + BusMixin 收口时,BusMixin.__init__ 显式 `super().__init__()`
        一处收口(plan §8 #2 「单点 init 收敛」惯例,沿用 plan-batch7-goal2
        `MessageListModel` 7 字段单点 init 模式):赋值 3 facade 实例引用 +
        初始化 3 mixin 字段 + 调 `self.wire_bus()` 触发 BusMixin 自动挂
        21 events(channels 2 + system 7 + messages 3 + media 5 + export 4)。

        3 字段(语义上归各 mixin 持有,init 集中在本方法避免 mutable
        default 共享陷阱 — `known_channels: dict = {}` 类体 default
        会被多 VM 实例共享,违反 facade per-instance 语义):
        - `self.known_channels: dict[int, ChannelDTO] = {}`(ChannelsMixin 域)
        - `self._message_count: int = 0`(MessagesMixin 域)
        - `self._export_task: asyncio.Future[None] | None = None`
          (ExportMixin 域)

        `super().__init__()` 走 MRO 链 QObject(`MonitorViewModel` 继承链
        末 `QObject`),让 Shiboken metaclass 完成 Qt 子类初始化 —
        Signal 描述符聚合、event filter 安装等。

        Args:
            app: AppService facade 引用 — VM 事件转发的目标服务入口
            monitor: MonitorService 引用 — channel sync / whitelist 操作
            loop: Qt+qasync 主线程 asyncio 事件循环 — `run_coro` 异步转发用
        """
        super().__init__()
        self.app = app
        self.monitor = monitor
        self.loop = loop
        # 2026-10-20 plan-batch9-η:3 mixin 字段单点 init 收口(类体
        # default 不能用 `= {}` mutable shared,改在本方法显式实例属性赋值)
        self.known_channels: dict[int, ChannelDTO] = {}
        self._message_count = 0
        self._export_task: asyncio.Future[None] | None = None
        self.wire_bus()

    def wire_bus(self) -> None:
        """挂载所有 mixin 注册的 EventBus handler — 走 MRO walk 累加。

        2026-10-19 plan-batch9-α:`type(self).__mro__` 倒序 walk 累加
        各 mixin 类体 `_BUS_HANDLERS`(子类不覆盖父类 dict,直接合)。
        `getattr(self, name)` 拿 bound method 给 `bus.subscribe`。

        Raises:
            AttributeError: handler_name 拼错时,`getattr` 立即抛
                — 测试构造 VM 时即发现,优于静默漏挂事件(原
                monitor_vm.py 21 个 `b.subscribe` 行手写,漏一个不易查)。
        """
        bus: EventBus = self.app.bus
        for klass in reversed(type(self).__mro__):
            for evt_cls, handler_name in klass.__dict__.get("_BUS_HANDLERS", {}).items():
                bus.subscribe(evt_cls, getattr(self, handler_name))
