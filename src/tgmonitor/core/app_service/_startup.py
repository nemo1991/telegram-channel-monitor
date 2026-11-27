"""AppService 参数化启动序列 — P1-9 / ADR-002 决策 1。

三处调用方(GUI / CLI monitor / CLI sync)原本各自手写「白名单 → monitor →
bootstrap」,步骤相同但顺序与策略不同。本模块把步骤收敛到 `start(app, options)`,
调用方只保留策略代码(paused 提示 / 退出码 / 状态栏反馈)。

固定顺序:白名单 → monitor → bootstrap — monitor 在会话连接前就绪,避免
白名单未建好就开始收消息。

`_lifecycle.bootstrap` 内部已有 paused guard(直接返 ready 不连 client);
本模块的 `respect_paused` 在其**之前**短路,让调用方能区分「用户主动暂停」
与「会话就绪」两种终态(client 的 ready 在暂停场景并不代表连过 Telegram)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StartOptions:
    """启动策略 — 步骤固定,这里只描述调用方之间的差异。"""

    whitelist: Sequence[int] | None = None
    """None = 从 storage.list_monitored_channels() 加载;显式序列 = CLI 指定。"""

    start_monitor: bool = True
    """False = 只做白名单 + bootstrap,不启实时流(CLI sync)。"""

    respect_paused: bool = True
    """True = is_paused 时跳过 monitor + bootstrap,返回 state='paused'。"""

    on_step: Callable[[str], None] | None = None
    """步骤回调,参数为 whitelist / monitor / bootstrap / paused。"""


@dataclass(frozen=True)
class StartResult:
    """启动结果 — 调用方据此决定 UI 提示 / 退出码。"""

    state: str
    """终态:ready / error / paused(+ client 的其它终态,如 phone_required)。"""

    detail: str | None
    whitelist_count: int
    monitor_started: bool


def _noop_step(_step: str) -> None:
    """默认空步骤回调。"""


async def start(app: AppService, options: StartOptions) -> StartResult:
    """按固定顺序跑完启动序列,不吞异常(由调用方决定呈现方式)。

    `bootstrap()` 自身把 client 失败转成 `(state, detail)` 并发布
    `ErrorOccurred`,所以这里不需要 try/except;白名单 / monitor 步骤若抛错,
    调用方 finally 的 `shutdown()` 负责清理(幂等)。
    """
    on_step = options.on_step or _noop_step

    # Step 1:白名单 — paused 场景也加载(与原 GUI / CLI 行为一致,
    # 恢复监听时白名单已在位,不必再读一次 storage)。
    on_step("whitelist")
    if options.whitelist is None:
        subscribed = await app.channels.list_monitored_channels()
        ids = [c.id for c in subscribed]
    else:
        ids = list(options.whitelist)
    if app.monitor is not None:
        app.monitor.set_whitelist(ids)

    # Step 2:paused gate — 跳过 monitor + bootstrap,client 保持 uninit。
    if options.respect_paused and app.is_paused:
        on_step("paused")
        log.info("start: paused, skip monitor + bootstrap (%d whitelist)", len(ids))
        return StartResult(
            state="paused",
            detail="settings.paused=true",
            whitelist_count=len(ids),
            monitor_started=False,
        )

    # Step 3:monitor — 在会话连接前就绪,不漏消息。
    monitor_started = False
    if options.start_monitor and app.monitor is not None:
        on_step("monitor")
        await app.monitor.start()
        monitor_started = True

    # Step 4:bootstrap Telegram 会话 — 触发 LoginStateChanged。
    on_step("bootstrap")
    state, detail = await app.bootstrap()

    # 插件运行期钩子(AkA 阶段 3):会话真正就绪后 start。paused 分支已在
    # Step 2 短路返回,不会走到这里 —— 冷启动暂停的插件留到 resume 时 start。
    # bootstrap 非 ready(error / phone_required)同样不 start:会话没连上,
    # 插件的「就绪即干活」前提不成立。失败在 host 内部隔离,不拖垮启动。
    if state == "ready":
        try:
            from tgmonitor.plugins.host import PLUGIN_HOST

            PLUGIN_HOST.start_all()
        except Exception:
            log.exception("start: 插件 start_all 失败,不阻断启动")

    return StartResult(
        state=state,
        detail=detail,
        whitelist_count=len(ids),
        monitor_started=monitor_started,
    )


__all__ = ["StartOptions", "StartResult", "start"]
