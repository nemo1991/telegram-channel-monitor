# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""ExportMixin — 导出 / 批量进度事件转发 + UI 入口方法 — 2026-10-20 plan-batch9-ζ。

`MonitorViewModel` 拆 5 mixin + BusMixin 第六步(2026-10-20
plan-batch9-ζ):export 域 = 4 handler + 5 method + 6 signal。

- handler(4):`_on_export_done` / `_on_export_progress` /
  `_on_batch_progress` / `_on_batch_done`
- method(5):`start_export` / `cancel_current_export` /
  `cancel_current_batch` / `export_media_list` / `export_zip`
- signal(6):`export_done` / `export_progress` / `export_cancelled` /
  `batch_progress` / `batch_done`(5 个事件对应 4 handler;`export_cancelled`
  是 UI 主动 trigger 的回调 signal,本 mixin 不 emit,给
  MainWindow 在 ExportProgressDialog 取消按钮订阅后接 cancel 后通知释放
  dialog 资源用)
- 字段:`_export_task: asyncio.Future[None] | None` 持当前导出异步任务
  引用 — UI 取消时 cancel 此 future,`ExportDone`/`CancelledError` 路径
  由 `_on_export_done` 清空。ExportMixin 唯一持此字段(plan §8 风险 #2
  「字段同 mixin 持有」原则)。

跨 mixin access:
- `_on_export_done` 写 `self._export_task = None` — 本 mixin 字段
- `cancel_current_export` 读 `self._export_task` — 本 mixin 字段
- `start_export` 写 `self._export_task = ` — 本 mixin 字段;返回 future
  给 caller(本例 main_window)也取句柄
- 其它 method 都 `run_coro` 转发到 `self.app.export_*` — facade 实例字段

`asdict()` 在 `_on_export_done` 用:`e.result` 是 `ExportResult`(dataclass)
需转 dict 跨 Qt 跨线程。dataclasses.asdict() 不递归展开嵌套 MediaDTO(此处
result 无 MediaDTO,是 ExportResult 字段),安全。

单点 init 收敛:`_export_task` 字段本 mixin 持有,但 facade `__init__` 仍
显式 `self._export_task = None` 兜底 — 沿用 plan §8 风险 #2 惯例直到 C-η
BusMixin.__init__ 接管。

MRO:`MonitorViewModel(ChannelsMixin, SystemMixin, MessagesMixin,
MediaMixin, ExportMixin, BusMixin, QObject)` — ExportMixin 在 MediaMixin
之后,BusMixin 末(QObject 兜底 super().__init__())。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal

from tgmonitor.core.dto import ExportRequest, MediaExportRequest
from tgmonitor.core.events import (
    BatchDone,
    BatchProgress,
    ExportDone,
    ExportProgress,
)
from tgmonitor.ui.async_bridge import run_coro

if TYPE_CHECKING:
    from PySide6.QtCore import QObject

    _Base = QObject
else:
    _Base = object

log = logging.getLogger(__name__)


class ExportMixin(_Base):
    """导出 / 批量进度事件转发 + UI 入口 — 2026-10-20 plan-batch9-ζ。

    公开 API 1:1 保留原 `MonitorViewModel` export 域:
    - 5 method:`start_export` / `cancel_current_export` /
      `cancel_current_batch` / `export_media_list` / `export_zip`
    - 6 signal:`export_done` / `export_progress` / `export_cancelled` /
      `batch_progress` / `batch_done`
    - 4 EventBus handler:`ExportDone` / `ExportProgress` /
      `BatchProgress` / `BatchDone`
    - 1 字段:`_export_task` 持当前导出 asyncio.Future(facade `__init__`
      显式 `self._export_task = None` 兜底,本 mixin 不写 init)
    """

    # 2026-08-30 v1.5.0 PR #A3:导出完成事件转发 — payload
    # (result_dict | None, error | None)。MainWindow 接到后:
    # - result 非 None → 弹成功消息框 + reveal in Finder
    # - error 非 None → 弹错误消息框
    export_done = Signal(object, object)
    # 导出进度(2026-08-30 v1.5.0 PR #A3)— payload ExportProgress
    # (request_id, written, total)。`total=None` 表示流式分页
    # (`service.py:_run_messages` 只在最后一批写完时才能确定总数)。
    export_progress = Signal(object)
    # 导出任务取消回调(2026-08-30 v1.5.0 PR #A3)— ExportProgressDialog
    # 取消按钮 → vm.cancel_current_export() → 取消当前 asyncio.Task,
    # ExportDone 仍发(error)。`export_cancelled` signal 留给 dialog 资源
    # 释放 hook(Mixin 不内部 emit;MainWindow 可主动 emit 或监
    # cancel_current_export 调用后 emit)。
    # payload (request_id,)
    export_cancelled = Signal(object)
    # 2026-09-09 v1.7.2:批量操作进度/完成 — payload BatchProgress / BatchDone。
    # MainWindow 接到 → BatchProgressDialog 进度更新 / accept。
    batch_progress = Signal(object)
    batch_done = Signal(object)

    # 2026-08-30 v1.5.0 PR #A3:`_export_task` 持当前导出 future —
    # ExportMixin 唯一持有此字段(facade `__init__` 兜底 = None,
    # `start_export` 写入,`_on_export_done` 清空)。mypy 需要类级
    # 字段声明以让 ExportMixin 内方法读 / 写 self._export_task 时
    # 都有 Future[None] | None 类型。
    _export_task: asyncio.Future[None] | None = None

    _BUS_HANDLERS = {
        ExportDone: "_on_export_done",
        ExportProgress: "_on_export_progress",
        BatchProgress: "_on_batch_progress",
        BatchDone: "_on_batch_done",
    }

    # ---- 4 EventBus handler ----

    async def _on_export_done(self, e: object) -> None:
        if not isinstance(e, ExportDone):
            return
        if e.error:
            self.export_done.emit(None, e.error)
        elif e.result is not None:
            self.export_done.emit(asdict(e.result), None)
        # 2026-08-30 v1.5.0 PR #A3:导出结束清空 task 引用 — 否则下一次
        # cancel_current_export 会误取消一个已完成 / 已抛错的 task
        # (asyncio 取消已完成 task 是 no-op,但日志噪音 + 引用悬挂)。
        self._export_task = None

    async def _on_export_progress(self, e: object) -> None:
        """2026-08-30 v1.5.0 PR #A3:转发 ExportService 进度 → Qt signal。

        `total=None` 表示分页中(总数未知,QProgressBar 用 indeterminate);
        `total=int` 表示流式写盘结束,UI 收尾。
        """
        if not isinstance(e, ExportProgress):
            return
        self.export_progress.emit(e)

    async def _on_batch_progress(self, e: object) -> None:
        """2026-09-09 v1.7.2:转发 AppService batch 进度 → Qt signal。

        MainWindow 接到 → BatchProgressDialog 进度条 + 状态标签更新。
        """
        if not isinstance(e, BatchProgress):
            return
        self.batch_progress.emit(e)

    async def _on_batch_done(self, e: object) -> None:
        """2026-09-09 v1.7.2:转发 AppService batch 完成 → Qt signal。

        MainWindow 接到 → BatchProgressDialog 收尾(accept / 标完成)。
        """
        if not isinstance(e, BatchDone):
            return
        self.batch_done.emit(e)

    # ---- 5 public method ----

    def start_export(self, req: ExportRequest) -> asyncio.Future[None]:
        """后台 fire `app.export(req)` 异步生成器,UI 不阻塞。

        `app.export` 内部 yield 心跳,这里只消耗掉(进度走 ExportProgress
        事件)。2026-08-30 v1.5.0 PR #A3:返回 future 引用,存到
        `self._export_task` 供 `cancel_current_export` 用。返回 future
        而非 None 让 caller(本例 main_window)也能取句柄。
        """

        async def _go() -> None:
            async for _ in self.app.export(req):
                pass

        self._export_task = run_coro(self.loop, _go(), error_label="start_export")
        return self._export_task

    def cancel_current_export(self) -> None:
        """2026-08-30 v1.5.0 PR #A3:取消当前导出 — `asyncio.Task.cancel()`。

        走 CancelledError 路径,ExportService._run_messages 的 `async for`
        让出点会抛 CancelledError,ExportDone 仍发(带 error)。多次取消安全:
        第二次以后 task 已结束,no-op。
        """
        task = self._export_task
        if task is not None and not task.done():
            task.cancel()
            # 不立即清 _export_task — 让 _on_export_done 在 export 真正
            # 结束(CancelledError 被 run_coro 捕获)后清。避免下一次
            # start_export 进来时被 UI 误标「上一轮已取消」。

    def cancel_current_batch(self) -> None:
        """2026-09-10 v1.7.3:取消当前批量操作 — `AppService._cancel_event.set()`。

        走协作式 cancel(不调 task.cancel):批量 facade 已在主 task loop
        await,设 Event 即可让循环跳出;task.cancel 会触发 CancelledError
        被 run_coro 吞,无法发 BatchDone(error='cancelled')。

        多次调用安全(只是 set 已 set 的 Event);无批量进行时也无副作用
        (下一次 facade 开头 `_cancel_event.clear()`)。已发 TDLib RPC 可能
        server-side 仍完成 — best-effort。
        """
        if self.app is not None:
            self.app.cancel_current_batch()

    def export_media_list(self, req: MediaExportRequest) -> None:
        """Media Manager 当前视图 → per-media CSV 导出 — 2026-08-25 v1.3.0 PR #7。

        复用 `ExportDone` 事件(`vm.export_done` 已绑),UI 无须新增信号;
        完成 / 失败通过 `_on_export_done` 走老路径弹消息框。
        """

        async def _go() -> None:
            async for _ in self.app.export_media_list(req):
                pass

        run_coro(self.loop, _go(), error_label="export_media_list")

    def export_zip(self, req: ExportRequest) -> None:
        """2026-09-01 v1.5.1 PR #B4:Media Manager 当前视图 → ZIP 打包。

        复用 `app.export(ExportRequest)` — `_run_messages` 检测到
        `format == ZIP` 时把 `object_store` 一律传进去(不依赖
        `include_thumbnails`),自动调 `ZipExporter.render` 完成打包。

        `req.format` 必须是 `ExportFormat.ZIP`;`req.channel_ids` 通常是
        `[<single_channel>]` 或 filter 出来的多条;`req.single_message_id`
        非 None 时走单条消息出口(见 `_run_messages`)。
        """

        async def _go() -> None:
            async for _ in self.app.export(req):
                pass

        run_coro(self.loop, _go(), error_label="export_zip")
