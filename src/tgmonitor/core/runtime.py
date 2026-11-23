"""Composition root — GUI / CLI 共用装配入口。

2026-09-27 refactor:把 `app.py:_bootstrap` 与 `_shutdown_async` 抽到本模块
public,供 CLI(`tgmonitor.cli.sync` / `tgmonitor.cli.monitor`)与 GUI
(`tgmonitor.app.run`)共用同一份装配,只换事件循环(qasync vs asyncio.run)。

**边界守则**:本模块顶层**不**import PySide6 / qasync / `tgmonitor.ui.*`。
CLI 入口调用本模块时不触发任何 UI 框架加载。`bootstrap()` 内部 lazy import
factory / MediaDownloader / ChannelSyncService 等,保留现有行为。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from tgmonitor.core.app_service import AppService
from tgmonitor.core.config import Settings, _user_data_dir
from tgmonitor.core.events import EventBus
from tgmonitor.core.monitor.service import MediaDownloader, MonitorService
from tgmonitor.core.objectstore.factory import build_object_store
from tgmonitor.core.storage.factory import build_storage
from tgmonitor.core.telegram.factory import build_telegram_client

log = logging.getLogger(__name__)


async def bootstrap(
    settings: Settings | None = None,
    env_path: Path | None = None,
) -> tuple[AppService, MonitorService, Settings, str | None]:
    """CLI / GUI 共用的 composition root。

    装配顺序:
        Settings → EventBus → Storage(connect + init_schema + introspect + repair)
                → ObjectStore(connect) → TelegramClient → MonitorService → AppService

    schema 迁移对用户无感:init_schema 幂等建表 + 补缺失列,repair 兜底
    introspect 仍报出的 drift(幂等;缺表拒绝、类型漂移仅 log)。无用户开关。

    返回 4 元组 `(app, monitor, settings, objects_error_or_None)`:
    - `app`:AppService facade(UI 唯一入口,CLI 也用同一份)
    - `monitor`:MonitorService 实时监听服务
    - `settings`:Settings 单例(供 CLI 读 media_policy 等)
    - `objects_error_or_None`:对象存储 connect 失败原因(GUI 主窗口红字提示;
      CLI 忽略 — 媒体下载失败会落到 download_error,后续可观察)

    `settings=None` / `env_path=None` 时回到默认构造(测试 fixture 没 env_path)。
    """
    t0 = time.monotonic()
    if settings is None:
        settings = Settings()
    # v1.0.1:Settings 的 Path defaults 已经是 platform-native 绝对路径
    # (~/Library/Application Support/tgmonitor/...),不再需要 .resolve()
    # 把相对路径强制绝对 — 之前这步是 cwd-relative 的根因。
    settings.ensure_dirs()
    log.info(
        "[bootstrap] settings loaded in %.2fs | data_dir=%s session=%s exists=%s | db_backend=%s",
        time.monotonic() - t0,
        settings.data_root,
        settings.session_dir,
        (settings.session_dir / "tgmonitor.session").exists(),
        settings.db_backend.value,
    )

    bus = EventBus()

    t = time.monotonic()
    storage = build_storage(settings)
    await storage.connect()
    log.info("[bootstrap] storage.connect() took %.2fs", time.monotonic() - t)
    t = time.monotonic()
    await storage.init_schema()
    log.info("[bootstrap] storage.init_schema() took %.2fs", time.monotonic() - t)

    # 2026-11-17 v2.0.3:schema 迁移对用户无感 —— init_schema 已幂等建表 + 补列,
    # 这里 introspect 保留诊断日志,并对仍报出的 drift 无条件 repair 兜底。
    # repair 幂等:缺表拒绝(避免 DROP+CREATE 丢数据)、类型漂移仅 log、缺列补上。
    # 正常路径 report 恒 ok → repair 是 no-op,不产生任何用户可见提示或配置负担。
    t = time.monotonic()
    report = await storage.introspect_schema()
    log.info(
        "[bootstrap] schema introspection took %.2fs | %s",
        time.monotonic() - t,
        report.summary(),
    )
    if not report.ok:
        log.warning("[bootstrap] schema drift (%s); applying idempotent repair", report.summary())
        try:
            await storage.repair_schema(report)
            report2 = await storage.introspect_schema()
            if not report2.ok:
                log.error(
                    "[bootstrap] schema repair incomplete: %s; refusing to start",
                    report2.summary(),
                )
                raise RuntimeError(f"schema repair incomplete: {report2.summary()}")
            log.info("[bootstrap] schema repair applied successfully (%s)", report.summary())
        except Exception:
            log.exception("[bootstrap] schema repair failed; refusing to start")
            raise

    # 2026-11-23 v2.0.9 hotfix:启动自愈卡住的 DOWNLOADING —— 下载任务只活在
    # 内存队列,进程关闭后没人推进,DB 永久残留 downloading + 无 object_key,
    # UI 画沙漏且下载/重试按钮都禁用。复位为未下载,用户可重新点下载。
    # 失败不阻断启动(降级为日志),与 objectstore.connect 的容错策略一致。
    t = time.monotonic()
    try:
        stuck = await storage.reset_stuck_downloads()
        if stuck:
            log.warning(
                "[bootstrap] reset %d stuck DOWNLOADING media (no object_key) "
                "to undownloaded in %.2fs",
                stuck,
                time.monotonic() - t,
            )
    except Exception:  # noqa: BLE001
        log.exception("[bootstrap] reset_stuck_downloads failed; continuing startup")

    # 2026-11-17 v2.0.4 FR-05:活动持久化订阅者 —— bus + storage 均已就绪,
    # 通配订阅把覆盖的运维事件归一化后落库。订阅者内部吞异常,落库失败不
    # 阻断主流程;formatter 不含 Qt 依赖(core 架构守门)。
    from tgmonitor.core.activity import make_persistence_subscriber  # noqa: PLC0415

    bus.subscribe_all(make_persistence_subscriber(storage))

    t = time.monotonic()
    objects = build_object_store(settings)
    objects_error: str | None = None
    try:
        await objects.connect()
    except Exception as e:  # noqa: BLE001
        # v1.0.21:对象存储不可用(端点错/凭据错/桶无权限)不阻止应用启动 —
        # 媒体落盘是可降级能力,失败在保存设置时已严格校验;启动这里只降级
        # 记日志,下载任务会标 download_error,用户可感知。
        # v1.0.22:错误信息带回给 UI,主窗口状态栏红字常驻提示(不只写日志)。
        objects_error = str(e)
        log.error(
            "[bootstrap] objectstore.connect() failed, 媒体下载将失败: %s "
            "(backend=%s bucket=%s endpoint=%s)",
            e,
            settings.objectstore_backend.value,
            settings.objectstore_bucket,
            settings.objectstore_endpoint,
        )
    else:
        log.info(
            "[bootstrap] objectstore.connect() took %.2fs backend=%s",
            time.monotonic() - t,
            settings.objectstore_backend.value,
        )

    t = time.monotonic()
    # 凭据未配置时,factory 返回占位 client(UnconfiguredTelegramClient)→ UI
    # 正常启动,显示"未登录"引导,用户在 设置 → 账户 填好凭据重启即可。
    # 真 client 构造失败仍上抛 → 走 setup 失败弹窗;不静默回退 fake
    # (历史 bug #22:吞异常返 Fake 导致"后端不可用也能 ready")。
    client = build_telegram_client(settings, use_fake=False, event_bus=bus)
    log.info(
        "[bootstrap] telegram client built in %.2fs kind=%s",
        time.monotonic() - t,
        type(client).__name__,
    )

    # FULL 媒体策略才真正下载原文件:组合根负责接线 MediaDownloader,
    # MonitorService 侧 `downloader=None`(如未接线)时 FULL 策略静默退化为
    # 不下载 — 避免历史 bug:策略选了 FULL 但没有任何下载器在工作。
    monitor = MonitorService(
        bus,
        client,
        storage,
        objects,
        settings,
        downloader=MediaDownloader(
            client,
            storage,
            objects,
            max_bytes=settings.media_max_bytes,
        ),
    )
    # 2026-09-22 v1.8.x:env_path 单源化 — `run()` 阶段 0 算一次后传入,这里
    # 不再独立计算,避免与 MainWindow / AppService 三处可能漂移。
    if env_path is None:
        env_path = _user_data_dir() / ".env"
    app = AppService(
        bus,
        client,
        storage,
        objects,
        settings,
        monitor=monitor,
        # 2026-09-04 v1.6.6:pause 持久化要写 .env,透传 env_path。
        env_path=env_path,
    )
    log.info(
        "[bootstrap] full bootstrap done in %.2fs",
        time.monotonic() - t0,
    )
    return app, monitor, settings, objects_error


async def shutdown(app: AppService, monitor: MonitorService) -> None:
    """`bootstrap()` 的配套清理 — CLI / GUI 共用。

    顺序敏感:`monitor.stop()` 先(断实时流 + 关下载队列)→
    `app.shutdown()`(client.close + storage.close + objects.close)。

    每步自带兜底:失败仅 log 不抛,保证 CLI 进程总能干净退出。
    `AppService.shutdown` 内部已分阶段 2s 超时(client.close 断网偶发卡);
    `monitor.stop()` 内部 2s 超时硬 cancel。
    """
    if isinstance(monitor, MonitorService):
        try:
            await monitor.stop()
        except Exception:  # noqa: BLE001
            log.exception("shutdown: monitor.stop() failed")
    if isinstance(app, AppService):
        try:
            await app.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("shutdown: app.shutdown() failed")
