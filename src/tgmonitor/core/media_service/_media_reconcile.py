"""Storage reconcile — 2026-10-15 plan-batch6-1.5 抽到子 module。

包含:
- `reconcile_orphans` — 扫描 ObjectStore vs storage 媒体索引,孤儿 = ObjectStore
  里有但 storage 没引用;dry_run=True(默认)只 log 不删,False 走真删

设计(2026-10-15 plan-batch6):
- plain module-level function,首参 `ms: MediaService`
- mutex `ms._reconcile_lock`(2026-09-22 加的并发重入保护)— 锁住时直接返
  空事件不重入
- facade 1 行 thin delegate
- 子 module 不引入新私有 helper(原 reconcile 循环内联 delete,简单清晰;
  未来若 delete 路径复杂化,可拆 `_delete_orphan` helper,本步先不动)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tgmonitor.core.dto import MediaDownloadStatus
from tgmonitor.core.events import MediaReconcileFinished

if TYPE_CHECKING:
    from tgmonitor.core.media_service.media_service import MediaService

log = logging.getLogger(__name__)


async def reconcile_orphans(
    ms: MediaService,
    *,
    dry_run: bool = True,
) -> MediaReconcileFinished:
    """扫描 ObjectStore vs storage 媒体索引,孤儿 = ObjectStore 里有但 storage 没引用。

    dry_run=True(默认)只 log 不删;Media Manager 「Prune Orphans」按钮显式
    触发 dry_run=False 真删。S3 后端:
    - 未连接(iter_keys raise RuntimeError)→ 当作 scanned=0 兜底
    - raise NotImplementedError(理论上不再发生)→ 同上兜底

    2026-09-22 v1.8.x:并发重入保护 — `_reconcile_lock` 已锁时跳过,不抛,
    返 `MediaReconcileFinished(scanned=0, referenced=0, orphans=0, deleted=0,
    dry_run=dry_run)` 空事件,UI footer 不更新。启动期 _startup_reconcile
    (app.py:363,2s 后跑)与用户启动 < 2s 立刻点 "Prune Orphans" 重入时,
    第二次走这条 skip 路径,避免 iter_keys + delete 循环并发。

    2026-10-15 plan-batch6-1.5:thin delegate → `core.media_service._media_reconcile`。
    """
    if ms._reconcile_lock.locked():
        log.info(
            "reconcile_orphans: skipped, already in progress (dry_run=%s)",
            dry_run,
        )
        return MediaReconcileFinished(
            backend=ms._objects.backend_name if ms._objects else "",
            scanned=0,
            referenced=0,
            orphans=0,
            deleted=0,
            dry_run=dry_run,
        )
    async with ms._reconcile_lock:
        backend = ms._objects.backend_name if ms._objects else ""
        scanned_keys: set[str] = set()
        referenced_keys: set[str] = set()
        if ms._objects is not None and hasattr(ms._objects, "iter_keys"):
            try:
                # 2026-09-29:缩略图独立存储 — 同时扫 `media/` + `thumb/`
                # 两个 prefix。原图走 media/,缩略图走 thumb/(顶层平行
                # 关系)。任何孤儿都会出现在 scraped - referenced_keys。
                async for k in ms._objects.iter_keys(prefix="media/"):
                    scanned_keys.add(k)
                async for k in ms._objects.iter_keys(prefix="thumb/"):
                    scanned_keys.add(k)
            except (NotImplementedError, RuntimeError) as e:
                # 2026-08-25 PR #2:加 RuntimeError(S3 未连接会 raise "未连接")
                log.info(
                    "reconcile skipped: %s backend iter_keys unavailable: %s",
                    backend,
                    e,
                )
        chs = await ms._storage.list_channels()
        if chs:
            msgs = await ms._storage.list_messages(
                [c.id for c in chs],
                limit=100_000,
            )
            # 2026-09-29:同时收集原图 + 缩略图引用;thumb 走独立
            # `thumbnails` 表,需逐 message 调 `list_thumbnails_for_message`
            # 拉 thumb DTO(media_idx 索引保证与父 media 一一对应)。
            for m in msgs:
                for med in m.media:
                    if med.object_key and med.download_status == MediaDownloadStatus.DONE:
                        referenced_keys.add(med.object_key)
                thumbs = await ms._storage.list_thumbnails_for_message(
                    m.channel_id, m.telegram_msg_id
                )
                for t in thumbs:
                    if t.object_key and t.download_status == MediaDownloadStatus.DONE:
                        referenced_keys.add(t.object_key)
        orphans = scanned_keys - referenced_keys
        deleted = 0
        if not dry_run and ms._objects is not None and orphans:
            for k in orphans:
                try:
                    await ms._objects.delete(k)
                    deleted += 1
                except Exception:  # noqa: BLE001
                    log.warning("reconcile delete %s failed", k, exc_info=True)
        evt = MediaReconcileFinished(
            backend=backend,
            scanned=len(scanned_keys),
            referenced=len(referenced_keys),
            orphans=len(orphans),
            deleted=deleted,
            dry_run=dry_run,
        )
        log.info(
            "reconcile: backend=%s scanned=%d referenced=%d orphans=%d deleted=%d dry_run=%s",
            backend,
            evt.scanned,
            evt.referenced,
            evt.orphans,
            evt.deleted,
            dry_run,
        )
        await ms._bus.publish(evt)
        return evt


__all__ = ["reconcile_orphans"]
