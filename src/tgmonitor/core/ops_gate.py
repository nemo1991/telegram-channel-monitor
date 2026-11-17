"""操作就绪门控 —— v2.0.3 D7:core 层全局操作门控的单一真相源。

设计(用户 2026-11-17 五次评审拍板的层次归属):
- **core 为底**:门控规则只在 core 定义一次,所有依赖 TG 的操作入口在这里裁决;
- **UI 适配**:UI 订阅 `LoginStateChanged` 只做按钮禁用/恢复,不重复实现判定规则;
- 真相源复用 `client.state`(7 态状态机,唯一写入口 `_set_state` 发
  `LoginStateChanged`),**不新造状态字段**。

为什么在 core 而不是 UI(关键论证):`cli/monitor.py` / 编程式调用 / 异步时序
都会绕过 UI。门控若只在 UI,这些路径未登录照样执行 TG 操作。放 core 域服务
入口才能对所有调用方一致生效。

两种模式:
- `require_ready(client, operation)` —— 事务操作(订阅/退订/全量同步)用:
  未 ready 抛 `OperationNotReadyError`,调用方 run_coro 统一显示错误。
- `is_ready(client)` —— best-effort 操作(刷新列表)用:未 ready 返 False,
  由调用方决定降级行为(如 refresh_joined_channels 返 [] + ErrorOccurred)。

与 `backfill.py:96` 的 `state != "ready"` 跳过、`_check_alive` 的 closing guard
同属 core 层既有模式,本模块把它们收敛成一个可复用的判定点。
"""

from __future__ import annotations

from tgmonitor.core.telegram.client import TelegramClient

# 唯一允许执行依赖 TG 操作的状态。与 backfill.py:96 保持一致。
READY_STATE = "ready"


class OperationNotReadyError(RuntimeError):
    """依赖 TG 的操作在 client 未就绪(未登录/关闭中)时被拒绝。

    由 `require_ready` 抛出,携带当前 state 便于 UI 显示精确提示。
    """

    def __init__(self, operation: str, state: str) -> None:
        self.operation = operation
        self.state = state
        super().__init__(f"未登录或连接未就绪(state={state}),无法执行: {operation}")


def is_ready(client: TelegramClient) -> bool:
    """client 是否处于可执行 TG 操作的状态(唯一判定:state == ready)。"""
    return client.state == READY_STATE


def require_ready(client: TelegramClient, operation: str) -> None:
    """事务操作入口 guard:未 ready 抛 `OperationNotReadyError`。

    `operation` 是操作名(如 "订阅频道" / "全量同步"),用于错误消息。
    """
    state = client.state
    if state != READY_STATE:
        raise OperationNotReadyError(operation, state)
