"""ui 包 — PySide6 桌面 UI。

边界守则:
- 只 import `core.app_service.AppService` / `core.events.EventBus` / `core.monitor.service.MonitorService` / `core.dto.*`
- 不 import 任何 storage / objectstore / telegram / export 的内部模块
- 跨线程通过 asyncio.run_coroutine_threadsafe 把 UI 事件交给 qasync 循环
"""
