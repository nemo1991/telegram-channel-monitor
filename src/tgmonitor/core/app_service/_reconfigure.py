"""AppService 热重载 — 2026-10-12 plan-batch3 目标 2 step 2.5。

从 `AppService` 抽提 4 个热重载 method + 1 个 module helper:

公共 method(UI 入口):
- `reconfigure(app, new_settings)` — 切换 storage / objects + 同步 monitor /
  channel_sync / sub / media,最后发 SettingsChanged
- `validate_backends(app, new_settings)` — 仅校验后端连通性,不切换运行时

内部 helper(reconfigure 内部按 diff 调):
- `_rebuild_storage(app, new_settings)` — 先建新库,成功后才关旧库并替换
- `_rebuild_objects(app, new_settings)` — 同 storage 失败清理语义

module-level helper(纯函数,reconfigure 末尾 publish SettingsChanged.what 用):
- `_what_label(diff)` — diff → "storage+objectstore" / "storage" / "objectstore" /
  "client" / "credentials" 标签

设计原则(同 `_backend_sync` / `_media_actions` / `_batch_ops` / `_lifecycle`):
- plain module-level function(非 class)
- 第一参数 `app: AppService`(走 mutable state 引用)
- 公共方法签名 1:1 保留 — UI 现有调用面零变化
- `_rebuild_*` 私有 helper 也不再 facade 暴露(原本就只 reconfigure 内部调,
  测试从不直接调 — `grep` 确认 `tests/test_reconfigure.py` 仅在
  docstring/comment 提到,不实际 `app._rebuild_storage(...)`)

mut state 共享契约:
4 个 method 直接读写 `app.settings` / `app.storage` / `app.objects` /
`app.monitor` / `app.downloader` / `app.channel_sync` / `app.client` /
`app.bus` / `app._sub` / `app._media` / `app.auth` / `app._running` /
`app._reconfiguring`,不在 module 内持副本(同 plan-batch2 monitor/service.py
拆 facade 模式)。

reconfigure 是唯一会重建 `app._sub` / `app._media` / `app.auth` 的入口 —
这些子 service 持旧 storage / objects / client 引用,reconfigure 后必须
重新构造,否则 monitor 实时 / 补拉写旧库(2026-08-18 修)。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tgmonitor.core.events import SettingsChanged
from tgmonitor.core.objectstore.factory import build_object_store
from tgmonitor.core.settings_store import SettingsDiff, diff_settings
from tgmonitor.core.storage.factory import build_storage

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.config import Settings

log = logging.getLogger(__name__)


# ---------- 公共 method:reconfigure(UI 入口)----------


async def reconfigure(app: AppService, new_settings: Settings) -> None:
    """用新 settings 重建 storage / objects(不重建 TelegramClient)。

    Telegram 凭据(api_id/api_hash/phone)若变化,needs_relogin=True(UI 引导登出登入);
    proxy / session_dir 若变化,needs_restart=True(TdlibClient 构造参数,运行时
    不重建,UI 提示需重启生效 — 2026-08-18,此前纯代理/会话目录变更 diff 判
    无变化直接 return,UI 却弹「已保存并热重载」,属假成功)。

    流程(2026-08-03 微切,2026-08-18 加后端同步 + needs_restart):
      1. `diff_settings` 算 needs_relogin / storage_changed / objects_changed /
         client_changed
      2. 无变化 → return
      3. storage 优先(在 hot path)→ `_rebuild_storage`
      4. objects → `_rebuild_objects`(2026-08-18 起**无条件**跑 — 见下)
      5. 把新 storage/objects/settings 同步给 monitor(含重建下载器 +
         重载白名单)、重建 channel_sync — 否则热重载切 PG 不生效
      6. 重建 SubscriptionService / MediaService(它们持旧 storage / objects)
      7. publish `SettingsChanged` + 提交新 settings

    `_rebuild_objects` 无条件执行的背景:之前只在 `objects_changed` 时重建
    校验,若坏的对象存储配置已躺在 .env、本轮保存恰好没改对象存储字段,
    diff 判 objects 未变 → 跳过校验 → 静默通过,直到写 media 才报
    `S3 API Requests must be made to API port`。现在只要有任何设置变化
    (含纯凭据 / 纯代理变化),都真实 connect 校验一次对象存储,失败上抛、
    settings 不提交。
    """
    diff = diff_settings(app.settings, new_settings)
    if not diff.changed:
        return  # 无变化

    new_settings.ensure_dirs()

    if diff.storage_changed:
        await _rebuild_storage(app, new_settings)
    # 无条件校验对象存储(配置即使没变也真实 connect 校验)
    await _rebuild_objects(app, new_settings)

    # 5) 把新后端同步给所有持引用者 —— reconfigure 只换 AppService 自己
    # 的引用时,monitor / MediaDownloader 仍持旧 storage,实时 / 补拉消息
    # 继续写旧库,用户看到"切 PG 没生效,重启才生效"(2026-08-18 修)。
    if app.monitor is not None:
        await app.monitor.update_backends(
            app.storage,
            app.objects,
            new_settings,
        )
        # 2026-08-24:reconfigure 后 monitor 重建了 MediaDownloader(新
        # storage / objects / max_bytes),同步给 self.downloader 让 sync
        # 也用新实例。policy 变化同样需要 — 直接拿新 settings 字段。
        app.downloader = app.monitor.downloader
    from tgmonitor.core.channel_sync import ChannelSyncService

    app.channel_sync = ChannelSyncService(
        app.bus,
        app.client,
        app.storage,
        downloader=app.downloader,
        objects=app.objects,
        media_policy=new_settings.media_policy,
    )

    # 6) 重建子 service(它们持旧 storage / objects 引用,必须跟着切)
    from tgmonitor.core.media_service import MediaService
    from tgmonitor.core.subscription_service import SubscriptionService

    app._sub = SubscriptionService(app.bus, app.client, app.storage)
    app._media = MediaService(
        app.bus,
        app.storage,
        app.objects,
        downloader=app.downloader,
    )

    # 7) 提交新 settings + 事件
    app.settings = new_settings
    # AuthService 持旧 settings 引用的话,_check_credentials 预检用旧凭据
    # (2026-08-18:reconfigure 后重建,消除引用不一致)
    from tgmonitor.core.auth_service import AuthService

    app.auth = AuthService(app.bus, app.client, new_settings)
    await app.bus.publish(
        SettingsChanged(
            what=_what_label(diff),
            new_settings=new_settings,
            needs_relogin=diff.needs_relogin,
            needs_restart=diff.client_changed,  # proxy / session_dir 变更需重启生效
        )
    )


# ---------- 公共 method:validate_backends(UI 「仅保存到 .env」按钮用)----------


async def validate_backends(app: AppService, new_settings: Settings) -> None:
    """仅校验后端连通性,**不切换运行时** — 供「仅保存到 .env」按钮用。

    storage:connect → init_schema → close;objectstore:connect → close。
    任一失败上抛,调用方据此放弃落盘 .env;成功时 `app.storage` /
    `app.objects` 完全不受影响。校验中新建的连接失败即关闭,不留泄漏
    (与 `_rebuild_storage` / `_rebuild_objects` 同一套失败清理语义)。
    """
    new_settings.ensure_dirs()

    new_storage = build_storage(new_settings)
    try:
        await new_storage.connect()
        await new_storage.init_schema()
    except BaseException:
        try:
            await new_storage.close()
        except Exception:  # noqa: BLE001
            log.exception("关闭校验用的 storage 失败")
        raise
    try:
        await new_storage.close()
    except Exception as e:  # noqa: BLE001
        log.warning("关闭校验用的 storage 失败: %s", e)

    new_objects = build_object_store(new_settings)
    try:
        await new_objects.connect()
    except BaseException:
        try:
            await new_objects.close()
        except Exception:  # noqa: BLE001
            log.exception("关闭校验用的 objectstore 失败")
        raise
    try:
        await new_objects.close()
    except Exception as e:  # noqa: BLE001
        log.warning("关闭校验用的 objectstore 失败: %s", e)


# ---------- 内部 helper:_rebuild_storage / _rebuild_objects ----------


async def _rebuild_storage(app: AppService, new_settings: Settings) -> None:
    """切换 storage:**先建新库**(connect + init_schema)成功后才关旧库。

    失败时异常上抛(`reconfigure` 中止、settings 不提交),且旧 storage
    保持可用 — 不能"先关后建":新库连不上时旧存储已 close,monitor 会
    写进已关闭的 store,数据静默丢失(2026-08-13 修,PG 连不上时的表现)。
    """
    app._reconfiguring = True
    try:
        new_storage = build_storage(new_settings)
        try:
            await new_storage.connect()
            await new_storage.init_schema()
        except BaseException:
            # 新库未就绪:关掉已建连接再上抛,不留泄漏;旧 storage 不动
            try:
                await new_storage.close()
            except Exception:  # noqa: BLE001
                log.exception("关闭未就绪的新 storage 失败")
            raise
        # 新库就绪后才关旧库并替换;关旧库失败只 log,不影响切换
        try:
            await app.storage.close()
        except Exception as e:  # noqa: BLE001
            log.warning("关闭旧 storage 失败: %s", e)
        app.storage = new_storage
        # 2026-07-31 修 SUBSCRIBED_DRIFT_ANALYSIS #C 收尾:之前
        # 这里 `list_channels()`(全频道)跟 in-memory `_subscribed`
        # cache 做 union,会把 unsubscribed 旧频道错误标记为"已订"。
        # `AppService._subscribed` cache 已整体删除,真理 = storage。
        # VM 通过 `list_subscribed_channels()` 在 init_schema 之后
        # 重新拉一遍,自动同步到 monitor / channel_widget。
        # 触发路径:`reconfigure` 末尾 `await self.bus.publish(
        # SettingsChanged(..., needs_relogin=False))` → VM 的
        # `_on_settings_changed` slot 会重新走 bootstrap。
    finally:
        app._reconfiguring = False


async def _rebuild_objects(app: AppService, new_settings: Settings) -> None:
    """切换 objectstore:先建新、成功后关旧(与 `_rebuild_storage` 同语义)。

    `connect()` 失败(如 S3 端点 / 凭据 / 权限错)异常上抛,reconfigure
    中止、settings 不提交、旧 objectstore 保持可用;新建连接被关闭不留泄漏
    (2026-08-18,与 _rebuild_storage 对齐)。
    """
    new_objects = build_object_store(new_settings)
    try:
        await new_objects.connect()
    except BaseException:
        try:
            await new_objects.close()
        except Exception:  # noqa: BLE001
            log.exception("关闭未就绪的新 objectstore 失败")
        raise
    try:
        await app.objects.close()
    except Exception as e:  # noqa: BLE001
        log.warning("关闭旧 objectstore 失败: %s", e)
    app.objects = new_objects


# ---------- module-level helper:diff → 标签(给 UI 分类显示)----------


def _what_label(diff: SettingsDiff) -> str:
    """`SettingsChanged.what` 字段标签(给 UI 分类显示)。

    优先级:storage+objectstore > storage > objectstore > client > credentials
    (client 单独成立时是纯 proxy / session_dir 变化,storage/objects 都没碰;
    credentials 单独成立时是纯凭据变化)。
    """
    if diff.storage_changed and diff.objects_changed:
        return "storage+objectstore"
    if diff.storage_changed:
        return "storage"
    if diff.objects_changed:
        return "objectstore"
    if diff.client_changed:
        return "client"
    return "credentials"


__all__ = [
    "reconfigure",
    "validate_backends",
]
