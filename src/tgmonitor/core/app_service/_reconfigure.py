"""AppService 热重载 — 2026-10-12 plan-batch3 目标 2 step 2.5。

从 `AppService` 抽提 4 个热重载 method + 1 个 module helper:

公共 method(UI 入口):
- `reconfigure(app, new_settings)` — 切换 storage / objects + 同步 monitor /
  channel_sync / channels / messages / media,最后发 SettingsChanged
- `validate_backends(app, new_settings)` — 仅校验后端连通性,不切换运行时

内部 helper(reconfigure 内部按 diff 调):
- `_build_storage(new_settings)` — 纯构建:connect + init_schema 成功才返回
  新实例,失败关闭新连接上抛;不 close 旧库、不赋值(2026-11-25 P1-3)
- `_build_objects(new_settings)` — 同 storage 失败清理语义,纯构建

两段式提交(P1-3):`reconfigure` 先把新 storage / objects 全部构建并校验
成功(不 close 旧、不赋值),然后才在提交段统一完成「赋新 + setter 传播 +
monitor.update_backends + 域服务重建 + 提交 settings」,最后 close 旧后端。
任一构建失败直接丢弃新对象、抛原异常 — MonitorService / ChannelSyncService
持有的旧 storage 保持可用,settings 不提交(修复旧实现"旧库已 close 但
objects 校验失败"的半完成态)。

module-level helper(纯函数,reconfigure 末尾 publish SettingsChanged.what 用):
- `_what_label(diff)` — diff → "storage+objectstore" / "storage" / "objectstore" /
  "client" / "credentials" 标签

设计原则(同 `_backend_sync` / `_lifecycle`):
- plain module-level function(非 class)
- 第一参数 `app: AppService`(走 mutable state 引用)
- 公共方法签名 1:1 保留 — UI 现有调用面零变化
- `_build_*` 私有 helper 也不再 facade 暴露(原本就只 reconfigure 内部调,
  测试从不直接调 — `grep` 确认 `tests/test_reconfigure.py` 仅在
  docstring/comment 提到)

mut state 共享契约:
4 个 method 直接读写 `app.settings` / `app.storage` / `app.objects` /
`app.monitor` / `app.downloader` / `app.channel_sync` / `app.client` /
`app.bus` / `app.channels` / `app.messages` / `app.media` / `app.auth` /
`app._reconfiguring`,不在 module 内持副本。

reconfigure 是唯一会重建 `app.channels` / `app.messages` / `app.media` /
`app.auth` 的入口 — 这些域服务持旧 storage / objects / client 引用,
reconfigure 后必须重新构造,否则 monitor 实时 / 补拉写旧库(2026-08-18 修)。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tgmonitor.core.events import SettingsChanged
from tgmonitor.core.objectstore.base import ObjectStore
from tgmonitor.core.objectstore.factory import build_object_store
from tgmonitor.core.settings_store import SettingsDiff, diff_settings
from tgmonitor.core.storage.factory import build_storage
from tgmonitor.core.storage.repository import StorageRepository

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService
    from tgmonitor.core.auth_service import AuthService
    from tgmonitor.core.channel_service import ChannelService
    from tgmonitor.core.channel_sync import ChannelSyncService
    from tgmonitor.core.config import Settings
    from tgmonitor.core.media_service import MediaService
    from tgmonitor.core.message_service import MessageService

log = logging.getLogger(__name__)


class ReconfigureInProgressError(RuntimeError):
    """另一次 `reconfigure()` 正在执行中,本次请求被拒绝(不排队)。

    2026-11-27 ADR-002 决策 3:两段式提交的提交段必须独占。UI 的
    「应用」按钮无防抖,双击会让两个协程在预备段的 await 点交错进入;
    无守卫时后一次会把前一次刚建好的后端 close 掉,留下静默损坏。
    重入失败发生在任何副作用之前,无需回滚。
    """


# ---------- 公共 method:reconfigure(UI 入口)----------


async def reconfigure(app: AppService, new_settings: Settings) -> None:
    """用新 settings 重建 storage / objects(不重建 TelegramClient)。

    Telegram 凭据(api_id/api_hash)若变化,needs_relogin=True(UI 引导重新扫码登录);
    proxy / session_dir 若变化,needs_restart=True(TelegramClient 构造参数,运行时
    不重建,UI 提示需重启生效 — 2026-08-18,此前纯代理/会话目录变更 diff 判
    无变化直接 return,UI 却弹「已保存并热重载」,属假成功)。

    流程(2026-11-27 ADR-002 PR-B 三段式准原子):
      0. **重入守卫**:`_reconfiguring` 已置位 → 抛 `ReconfigureInProgressError`,
         拒绝而非排队(失败发生在任何副作用之前)
      1. `diff_settings` 算 needs_relogin / storage_changed / objects_changed /
         client_changed;无变化 → return
      2. **预备段**:storage(若变)→ `_build_storage`;objects → `_build_objects`
         (**无条件**跑 — 见下);再用局部新后端构造 channel_sync / 域服务 /
         auth。此段不 close 旧后端、不赋值、不动任何运行时引用;任一构建失败
         丢弃已建成的新对象、原样上抛,旧后端毫发无损
      3. **提交段**:`monitor.update_backends`(几乎不抛)后一次性纯赋值换引用 —
         不做可能抛错的构造;异常时 app 级引用一个都没动
      4. **收尾段**:publish `SettingsChanged` + close 旧后端;失败仅 log,
         不影响已完成的切换(原实现 publish 抛错会让旧后端泄漏且误报保存失败)

    `_build_objects` 无条件执行的背景:之前只在 `objects_changed` 时重建
    校验,若坏的对象存储配置已躺在 .env、本轮保存恰好没改对象存储字段,
    diff 判 objects 未变 → 跳过校验 → 静默通过,直到写 media 才报
    `S3 API Requests must be made to API port`。现在只要有任何设置变化
    (含纯凭据 / 纯代理变化),都真实 connect 校验一次对象存储,失败上抛、
    settings 不提交。
    """
    # ---- 0) 重入守卫:两段式提交要求提交段独占,拒绝而非排队 ----
    if app._reconfiguring:
        raise ReconfigureInProgressError("配置正在应用中,请稍后再试")

    app._reconfiguring = True
    try:
        diff = diff_settings(app.settings, new_settings)
        if not diff.changed:
            return  # 无变化

        new_settings.ensure_dirs()

        # ---- 2) 预备段:构建 + 校验全部新后端(不 close 旧、不赋值)----
        # 2026-11-25 P1-3:旧实现 `_rebuild_storage` 成功(旧库已 close、
        # app.storage 已换新并 setter 传播)后 `_rebuild_objects` 校验失败
        # 上抛 → MonitorService / ChannelSyncService 仍持已 close 的旧
        # storage,实时消息写不进(ErrorOccurred 风暴);且 settings 未提交
        # 导致下次 diff 基线错乱。现在先全部构建成功,再统一提交。
        new_storage: StorageRepository | None = None
        new_objects: ObjectStore | None = None
        new_channel_sync: ChannelSyncService | None = None
        new_channels: ChannelService | None = None
        new_messages: MessageService | None = None
        new_media: MediaService | None = None
        new_auth: AuthService | None = None
        try:
            if diff.storage_changed:
                new_storage = await _build_storage(new_settings)
            # 无条件校验对象存储(配置即使没变也真实 connect 校验;语义保持)
            new_objects = await _build_objects(new_settings)

            # 预备段续:用局部新后端构造全部新服务,不碰 app 运行时引用。
            # downloader 先用 app 当前值占位——提交段 update_backends 重建
            # 后被 setter 覆盖成新实例(见提交段顺序说明)。
            effective_storage = new_storage if new_storage is not None else app.storage
            new_downloader = app.downloader

            from tgmonitor.core.channel_sync import ChannelSyncService

            new_channel_sync = ChannelSyncService(
                app.bus,
                app.client,
                effective_storage,
                downloader=new_downloader,
                objects=new_objects,
                media_policy=new_settings.media_policy,
            )

            # BatchService 不重建:client / monitor 身份不变,is_paused 实时读 app。
            from tgmonitor.core.channel_service import ChannelService
            from tgmonitor.core.media_service import MediaService
            from tgmonitor.core.message_service import MessageService

            new_channels = ChannelService(app.bus, app.client, effective_storage, app.monitor)
            new_messages = MessageService(app.bus, effective_storage, app.monitor)
            new_media = MediaService(
                app.bus,
                effective_storage,
                new_objects,
                downloader=new_downloader,
            )
            # AuthService 持旧 settings 引用的话,_check_credentials 预检用旧凭据
            from tgmonitor.core.auth_service import AuthService

            new_auth = AuthService(app.bus, app.client, new_settings)
        except BaseException:
            # 已建成的前序新对象丢弃关闭(不留泄漏);旧后端不动
            for pending in (new_objects, new_storage):
                if pending is not None:
                    try:
                        await pending.close()
                    except Exception:
                        log.exception("关闭未提交的新后端失败")
            raise

        # ---- 3) 提交段:update_backends + 纯赋值(走到这里 = 全部构造成功)----
        old_storage = app.storage
        old_objects = app.objects
        # update_backends 在任何赋值之前先做:WhitelistManager 先纯赋值再容错
        # 重载白名单,几乎不抛 —— 失败时 app 级引用一个都没动。
        if app.monitor is not None:
            await app.monitor.update_backends(
                effective_storage,
                new_objects,
                new_settings,
            )

        # 一次性换引用。顺序关键:先赋新域服务,再赋 downloader —— 后者经
        # setter 传播给 media / channel_sync,必须打到新实例上。
        if new_storage is not None:
            # setter 传播给 channels / messages / media / channel_sync
            app.storage = new_storage
        app.objects = new_objects  # setter 传播给 media
        app.channel_sync = new_channel_sync
        app.channels = new_channels
        app.messages = new_messages
        app.media = new_media
        app.auth = new_auth
        if app.monitor is not None:
            # 2026-08-24:monitor 重建了 MediaDownloader,同步给 app 侧让 sync
            # 用新实例;setter 传播给上面刚装好的新 media / channel_sync。
            app.downloader = app.monitor.downloader
        app.settings = new_settings

        # ---- 4) 收尾段:publish + close 旧后端,失败仅 log ----
        try:
            await app.bus.publish(
                SettingsChanged(
                    what=_what_label(diff),
                    new_settings=new_settings,
                    needs_relogin=diff.needs_relogin,
                    needs_restart=diff.client_changed,  # proxy / session_dir 需重启
                )
            )
        except Exception:
            log.exception("发布 SettingsChanged 失败(不影响已完成的热重载)")

        # storage 未变时不关(new_storage is None = 本轮没建新库)
        if new_storage is not None:
            try:
                await old_storage.close()
            except Exception as e:  # noqa: BLE001
                log.warning("关闭旧 storage 失败: %s", e)
        try:
            await old_objects.close()
        except Exception as e:  # noqa: BLE001
            log.warning("关闭旧 objectstore 失败: %s", e)
    finally:
        app._reconfiguring = False


# ---------- 公共 method:validate_backends(独立后端连通性校验 API)----------


async def validate_backends(app: AppService, new_settings: Settings) -> None:
    """仅校验后端连通性,**不切换运行时**。

    2026-11-19 v2.0.7 FR-09:原 UI「仅保存到 .env」入口已删除,本 API 作为
    core 公共能力保留(可由 CLI / 外部调用方复用),行为不变。

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
        except Exception:
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
        except Exception:
            log.exception("关闭校验用的 objectstore 失败")
        raise
    try:
        await new_objects.close()
    except Exception as e:  # noqa: BLE001
        log.warning("关闭校验用的 objectstore 失败: %s", e)


# ---------- 内部 helper:_build_storage / _build_objects(纯构建)----------


async def _build_storage(new_settings: Settings) -> StorageRepository:
    """构建并校验新 storage(connect + init_schema),**不碰任何运行时引用**。

    2026-11-25 P1-3:从 `_rebuild_storage` 改为纯构建 — 不 close 旧库、不赋值
    `app.storage`,只返回就绪的新实例。失败时关闭已建连接再上抛(不留泄漏)。
    提交(close 旧 + 赋新)由 `reconfigure` 阶段 2 在全部构建成功后统一完成。

    为什么不能"先关后建":新库连不上时若旧存储已 close,monitor 会写进
    已关闭的 store,数据静默丢失(2026-08-13 修,PG 连不上时的表现)。
    """
    new_storage = build_storage(new_settings)
    try:
        await new_storage.connect()
        await new_storage.init_schema()
    except BaseException:
        # 新库未就绪:关掉已建连接再上抛,不留泄漏;旧 storage 不动
        try:
            await new_storage.close()
        except Exception:
            log.exception("关闭未就绪的新 storage 失败")
        raise
    return new_storage


async def _build_objects(new_settings: Settings) -> ObjectStore:
    """构建并校验新 objectstore(connect),**不碰任何运行时引用**。

    2026-11-25 P1-3:从 `_rebuild_objects` 改为纯构建 — 不 close 旧 store、
    不赋值 `app.objects`,只返回已连接的新实例。`connect()` 失败(如 S3
    端点 / 凭据 / 权限错)关闭新连接后原样上抛,旧 objectstore 保持可用
    (2026-08-18 语义,与 `_build_storage` 对齐)。
    """
    new_objects = build_object_store(new_settings)
    try:
        await new_objects.connect()
    except BaseException:
        try:
            await new_objects.close()
        except Exception:
            log.exception("关闭未就绪的新 objectstore 失败")
        raise
    return new_objects


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
