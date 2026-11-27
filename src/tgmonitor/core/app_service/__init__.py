"""AppService package — 组合根 + 生命周期协调。

`AppService` 位于 `tgmonitor.core.app_service.app_service`(避免与本
package 同名 import 冲突);外部 `from tgmonitor.core.app_service import
AppService` 走本 __init__ re-export。

2026-11-11 v2.0.0 Phase 3:域逻辑已拆到独立服务(core/channel_service /
message_service / batch_service / media_service / auth_service),本 package
只剩生命周期(`_lifecycle`)、热重载(`_reconfigure`)、后端 swap 传播
(`_backend_sync`)三个辅助模块 — module-level plain function,接收
`app: AppService` 作第一参数走 mutable state 读写。

2026-11-26 ADR-002:新增 `_startup` 承载参数化启动序列 `start(options)`,
同时导出 `StartOptions` / `StartResult` 供 GUI / CLI 调用方构造策略。
"""

from tgmonitor.core.app_service._reconfigure import ReconfigureInProgressError
from tgmonitor.core.app_service._startup import StartOptions, StartResult
from tgmonitor.core.app_service.app_service import AppService

__all__ = ["AppService", "ReconfigureInProgressError", "StartOptions", "StartResult"]
