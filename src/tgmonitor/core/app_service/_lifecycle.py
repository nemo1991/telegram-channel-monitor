"""AppService 生命周期 — 2026-10-12 plan-batch3 目标 2 step 2.4。

从 `AppService` 抽提 6 个生命周期 method + 1 个启动 guard:

- `bootstrap` — 应用启动调一次:client.start() + 401 rotate 重建
- `start_monitor` / `stop_monitor` — 实时流生命周期 + update stream 关
- `pause_monitor` / `resume_monitor` — 暂停 / 恢复监听(.env 持久化)
- `shutdown` — app exit:停 monitor → 关 client → storage → objects

设计原则(同 `_backend_sync` / `_media_actions` / `_batch_ops`):
- plain module-level function(非 class)
- 第一参数 `app: AppService`(走 mutable state 引用)
- 公共方法签名 1:1 保留 — UI 现有调用面零变化

mut state 共享契约:
6 个 method 直接读写 `app._is_paused` / `app._running` /
`app._update_streams` / `app.monitor` / `app.client` / `app.bus` /
`app.env_path` / `app.settings` / `app.storage` / `app.objects`,
不在 module 内持副本(同 plan-batch2 monitor/service.py 拆 facade 模式)。

`bootstrap` 是唯一会 swap `app.client` / 重建 `app._sub` / `app.auth`
的方法 — 401 encryption key 错误时需 rotate + rebuild client。
子 service 持旧 client 引用,rebuild 后必须重新构造。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from tgmonitor.core.auth_service import AuthService
from tgmonitor.core.events import ErrorOccurred, MonitoringPaused, MonitoringResumed
from tgmonitor.core.settings_store import update_env_paused
from tgmonitor.core.subscription_service import SubscriptionService

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService

log = logging.getLogger(__name__)


async def bootstrap(app: AppService) -> tuple[str, str | None]:
    """应用启动时调用一次:自动检测本地 session,有效就直接 ready,无效走 login。

    返回 (state, detail)。

    401 加密 key 错误 → rotate + rebuild client + 再 start;client swap
    留在此方法(影响 `AppService.client`,AuthService 不该碰)。

    注意:历史上有 in-memory `self._subscribed` cache(2026-07-31 删),
    现在所有「订阅真理」都走 `storage.list_subscribed_channels()` —
    详见 `docs/SUBSCRIBED_DRIFT_ANALYSIS.md` #A/#B/#C。

    2026-09-04 v1.6.6:paused-startup 时 (`_is_paused=True`) 直接返
    `("ready", None)` 不连 TDLib — 正常路径由 `app.py:_setup_then_show`
    gate 跳过 `monitor.start()` + `bootstrap()` 整个不进 paused-startup。
    本 guard 是 belt-and-suspenders:兜底直接调 `bootstrap()` 的边缘场景(
    测试 / 未来扩展),避免在 paused 状态走 TDLib 连接(client.state 仍
    "uninit" 是 explicit,UI 渲染依赖 `is_paused` 不是 client.state)。
    """
    if app._is_paused:
        log.info("bootstrap: already paused, returning ready without client.start()")
        return "ready", None

    try:
        state, detail = await app.client.start()
    except Exception as e:  # noqa: BLE001
        await app.bus.publish(ErrorOccurred(source="bootstrap", message=str(e), exception=e))
        return "error", str(e)
    # 如果 start 失败 + 检测到 401 → 让底层的 nuke_and_rebuild 接管,
    # rotate 加密 key + 重建 client + 再 start 一次
    if state == "error" and detail and "encryption key" in detail:
        log.warning("bootstrap: 401 detected — rotating key and rebuilding client")
        await app.client.nuke_and_rebuild(rotate_key=True)
        from tgmonitor.core.telegram.factory import build_telegram_client

        await app.client.close()
        app.client = build_telegram_client(
            app.settings,
            use_fake=False,
            event_bus=app.bus,
        )
        # client swap 后必须重建 SubscriptionService(它持有旧 client)
        app._sub = SubscriptionService(app.bus, app.client, app.storage)
        # AuthService 持旧 client 引用,重建
        app.auth = AuthService(app.bus, app.client, app.settings)
        state, detail = await app.client.start()
    # client 端已经 publish 过 LoginStateChanged,这里只 fail-safe 再发一次终态
    if state == "error":
        await app.bus.publish(
            ErrorOccurred(
                source="bootstrap",
                message=detail or "start failed",
            )
        )
    return state, detail


async def start_monitor(app: AppService) -> None:
    """订阅 client 的实时更新并消费(MonitorService 的细节后续接入)。"""
    if app._running:
        return
    app._running = True


async def stop_monitor(app: AppService) -> None:
    """停 monitor + 关所有 update stream;幂等。"""
    app._running = False
    for s in app._update_streams:
        try:
            await s.aclose()
        except Exception:  # noqa: BLE001
            pass
    app._update_streams.clear()


async def pause_monitor(app: AppService, source: str = "tray") -> None:
    """2026-09-03 v1.6.1:暂停监听 — 断 TDLib + 取消 in-flight 下载。

    流程:
    1. `monitor.stop()` — 取消 download_task + 关 update stream
       (download_task 取消会把队列里 PENDING + 进行中 IN_PROGRESS 都 cancel)
    2. `client.stop()` — TDLib 完整断开,释放 fd + 内存
    3. `_is_paused = True` + 发 `MonitoringPaused` 事件

    幂等:已 paused 时再调 no-op。`_running` 仍保持 True(暂停 ≠ 退出),
    resume 走 `resume_monitor()` 走同一份 _running 状态。
    """
    if app._is_paused:
        log.debug("pause_monitor: already paused, no-op")
        return
    log.info("pause_monitor: source=%s", source)
    if app.monitor is not None:
        try:
            await app.monitor.stop()
        except Exception:  # noqa: BLE001
            log.exception("pause_monitor: monitor.stop failed")
    try:
        await app.client.stop()
    except Exception:  # noqa: BLE001
        log.exception("pause_monitor: client.stop failed")
    app._is_paused = True
    # 2026-09-04 v1.6.6:写 .env(TG_PAUSED=true)—失败 log.warning 不抛,
    # 内存态已正确,UI 正常;.env 写失败下次重启回退到旧值,可接受退化。
    if app.env_path is not None:
        try:
            update_env_paused(app.env_path, True)
        except Exception:  # noqa: BLE001
            log.exception("pause_monitor: failed to persist paused state to .env")
    await app.bus.publish(MonitoringPaused(source=source))


async def resume_monitor(app: AppService, source: str = "tray") -> None:
    """2026-09-03 v1.6.1:恢复监听 — 重启 TDLib + 重建 monitor。

    流程:
    1. `client.start()` — 重新走 authorization 状态机(已 login 的 session
       会秒过,冷启动走 phone/code/password)
    2. `monitor.start()` — 重订阅 TDLib update stream + 启动 download worker
    3. `_is_paused = False` + 发 `MonitoringResumed` 事件

    幂等:已 resumed 时再调 no-op。

    2026-09-04 v1.6.6:client.start 失败时**不**改 .env,保留 paused 状态
    让用户重试(resume 失败而 .env 写 resumed 会导致下次启动误以为已恢复,
    实际上 client 没连 — 不一致更糟)。写顺序:in-memory flip 先于 .env 写
    (失败仅 log,内存态正确;下次重启回退 .env 旧值,可接受退化)。
    """
    if not app._is_paused:
        log.debug("resume_monitor: not paused, no-op")
        return
    log.info("resume_monitor: source=%s", source)
    try:
        await app.client.start()
    except Exception:  # noqa: BLE001
        log.exception("resume_monitor: client.start failed")
        # 启动失败仍尝试重建 monitor + 发 Resumed 事件让 UI 复位?
        # — 失败时**不**发 Resumed,保持 paused 状态让用户重试
        return
    if app.monitor is not None:
        try:
            await app.monitor.start()
        except Exception:  # noqa: BLE001
            log.exception("resume_monitor: monitor.start failed")
    app._is_paused = False
    # 2026-09-04 v1.6.6:写 .env(TG_PAUSED=false)—失败保留 paused=True 内存态,
    # 避免 .env 说 resumed 但实际 client.start 失败的不一致(只在内存 flip 成功
    # 后写 — 上面 client.start 已成功才走到这里)。
    if app.env_path is not None:
        try:
            update_env_paused(app.env_path, False)
        except Exception:  # noqa: BLE001
            log.exception("resume_monitor: failed to persist paused state to .env")
    await app.bus.publish(MonitoringResumed(source=source))


async def shutdown(app: AppService) -> None:
    """app exit — 停 monitor + 关 client / storage / objects(顺序敏感)。

    2026-08-30 PR #A2 hotfix:不再清空 self._sub / self._media 引用 —
    它们是构造时必建,与 facade 同生命周期,置 None 反而让 mypy 在后续
    方法(若被误调)报 union-attr。shutdown 后整个 facade 随 app 销毁,
    GC 时一起回收。子 service 持旧 storage/objects 引用也无害
    (close() 后该引用已不可用,但 facade 已不再有调用入口)。

    2026-09-22 v1.8.x:**分阶段超时** — 之前整条 `_shutdown_async` 共用
    `app.py:416` 外层 5s wait_for,任何一段 hang(尤其 `client.close()` 走
    tdlib_json 内部 CFRunLoop,macOS 偶发卡)就会让整链被强 cancel,
    TDLib 子进程没 join、storage 连接没 close → 下次启动触发 401 →
    `nuke_and_rebuild` 重建 session → 用户被踢回登录。改为每阶段独立
    短超时,任一超时只 log 不上抛,继续往下走(原则:尽力清,不卡死)。
    `monitor.stop()` 内部已有 2s 自带超时(参见 service.py:200-228),
    这里不再包。
    """
    await stop_monitor(app)
    # 关 TelegramClient — tdlib_json 走 CFRunLoop (macOS) / eventfd+IOCP
    # (Linux/Windows),偶发 hang 给 2s hard timeout,失败仅 log。
    try:
        await asyncio.wait_for(app.client.close(), timeout=2.0)
    except TimeoutError:
        log.error(
            "client.close() 超时(2s)— TDLib thread 可能残留,下次启动若 401 "
            "会走 nuke_and_rebuild 重建 session"
        )
    except Exception:  # noqa: BLE001
        log.exception("client.close() failed")
    await app.storage.close()
    await app.objects.close()


__all__ = [
    "bootstrap",
    "start_monitor",
    "stop_monitor",
    "pause_monitor",
    "resume_monitor",
    "shutdown",
]
