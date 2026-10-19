# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""BusMixin — EventBus handler 自动挂载协议 — 2026-10-19 plan-batch9-α。

5 mixin + 1 入口壳拆 `viewmodels/monitor_vm.py` 839 LOC,BusMixin 持协议
骨架:
- `_BUS_HANDLERS: ClassVar[dict[type[Event], str]]` — 子 mixin 在类体
  填 `event_type → handler_method_name` 映射(集中声明,优于原
  `b.subscribe(X, self._on_X)` × 21 行)
- `wire_bus()` — 走 `type(self).__mro__` walk 累加挂载;handler 名错
  立即 AttributeError 在 `wire_bus()` 调时即失败(显式优于静默漏挂)

具体业务 mixin(`_channels` / `_system` / `_messages` / `_media` /
`_export`)从 C-β 起各自填 `_BUS_HANDLERS`。BusMixin 类体不放
`__init__`(避免与 monitor_vm 现有 `__init__` 冲突,init 接管留
C-η 收口时统一迁移;本批只写协议骨架)。

沿用 plan-batch7-goal2 `MessageListModel 5 mixin + QAbstractListModel`
多继承惯例:5 mixin 在前,BusMixin 在 QObject 之前(`super().__init__()`
一处收口)。

不引(2026-10-19 plan-batch9 §6 风险 #6):5 个 mixin 文件之间互不 import,
共享类型只走 `tgmonitor.core.events` 顶层,避免循环。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from tgmonitor.core.events import Event, EventBus


class BusMixin:
    """EventBus handler 自动挂载协议 — 2026-10-19 plan-batch9-α。

    子 mixin 在类体填 `_BUS_HANDLERS = {EventType: "handler_name"}`,
    `wire_bus()` 走 `type(self).__mro__` walk 累加(防止子类 dict 覆盖
    父类 dict 丢注册),用 `getattr(self, name)` 拿 bound method 挂到
    `EventBus.subscribe`。handler 名错 → `AttributeError` 在
    `wire_bus()` 调时即失败(显式优于静默漏挂)。

    MRO 倒序 walk 是从基类往子类走(基类 stub dict 空 → 各 mixin dict
    合入 → QObject 不在 dict 链路,自动跳过)— 顺序无关紧要,因为
    每个 mixin 的 `_BUS_HANDLERS` 互斥,walk 累加 = 全集。

    BusMixin 类体不放 `__init__`(2026-10-19 plan-batch9 §8 风险 #2
    缓解):仅 BusMixin 的 `__init__` 显式 `super().__init__()` 收口,
    其它 mixin 不写 `__init__`,沿用 plan-batch7-goal2 "单点 init 收敛"
    惯例。init 接管留 C-η 收口。
    """

    _BUS_HANDLERS: ClassVar[dict[type[Event], str]] = {}

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
