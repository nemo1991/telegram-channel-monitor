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
        (settings.session_dir / "tdlib").exists(),
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

    # 2026-09-23 v1.8.x:schema introspect + 选改 — 在 init_schema 之后跑,
    # 让 fresh DB 先经 init_schema 建表(introspect 直接 ok);legacy DB 先经
    # init_schema 跑 ALTER 补齐,introspect 也是 ok;只有 init_schema 失败的
    # 边缘场景(introspect 才会看到 drift)。
    t = time.monotonic()
    report = await storage.introspect_schema()
    log.info(
        "[bootstrap] schema introspection took %.2fs | %s",
        time.monotonic() - t,
        report.summary(),
    )
    if not report.ok:
        if settings.schema_auto_repair:
            log.warning(
                "[bootstrap] schema drift (%s); auto-repair ENABLED — applying",
                report.summary(),
            )
            try:
                await storage.repair_schema(report)
                report2 = await storage.introspect_schema()
                if report2.ok:
                    log.info(
                        "[bootstrap] schema repair applied successfully (%s)",
                        report.summary(),
                    )
                else:
                    log.error(
                        "[bootstrap] schema repair incomplete: %s; refusing to start",
                        report2.summary(),
                    )
                    raise RuntimeError(f"schema repair incomplete: {report2.summary()}")
            except Exception:
                log.exception("[bootstrap] schema repair failed; refusing to start")
                raise
        else:
            log.warning(
                "[bootstrap] schema drift (%s); auto-repair DISABLED. "
                "Set TG_SCHEMA_AUTO_REPAIR=true in .env to auto-apply.",
                report.summary(),
            )

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
    # 真 client 构造失败(如 libtdjson 缺失)仍上抛 → 走 setup 失败弹窗;不
    # 静默回退 fake(历史 bug #22:吞异常返 Fake 导致"无 libtdjson 也能 ready")。
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
    `AppService.shutdown` 内部已分阶段 2s 超时(client.close 走 TDLib 内部
    CFRunLoop / eventfd + IOCP,macOS 偶发卡);`monitor.stop()` 内部 2s 超时
    硬 cancel。
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