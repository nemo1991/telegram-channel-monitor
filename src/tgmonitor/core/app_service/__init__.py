"""AppService package — 2026-10-12 plan-batch3 目标 2 拆分。

facade `AppService` 位于 `tgmonitor.core.app_service.app_service`(避免
与本 package 同名 import 冲突);外部 `from tgmonitor.core.app_service
import AppService` 走本 __init__ re-export 仍可用。

子模块(`_backend_sync` / `_media_actions` / `_batch_ops` / `_lifecycle` /
`_reconfigure`)各管一块,module-level plain function,接收 `app: AppService`
作第一参数走 `app._xxx` mutable state 读写。

设计原则:
- 各子 module 持有 `app: AppService` 作第一参数,直接读 / 写 `app._xxx`
  mutable state(跟 plan-batch2 monitor/service.py 拆 facade 同构)
- 公共方法签名 1:1 保留 — UI 现有调用面零变化
"""

from tgmonitor.core.app_service.app_service import AppService

__all__ = ["AppService"]
