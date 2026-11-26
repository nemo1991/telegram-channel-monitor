"""Storage reconcile — 2026-10-15 plan-batch6-1.5 抽到子 module。

包含:
- `reconcile_orphans` — 扫描 ObjectStore vs storage 媒体索引,孤儿 = ObjectStore
  里有但 storage 没引用;dry_run=True(默认)只 log 不删,False 走真删

设计(2026-10-15 plan-batch6):
- plain module-level function,首参 `ms: MediaService`
- mutex `ms._reconcile_lock`(2026-09-22 加的并发重入保护)— 锁住时直接返
  空事件不重入
- facade 1 行 thin delegate
- `_collect_referenced_keys`(2026-11-25 P1-5):引用收集分页循环拆出,
  控制 `reconcile_orphans` 认知复杂度
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tgmonitor.core.dto import MediaDownloadStatus, SortDir, SortKey
from tgmonitor.core.events import MediaReconcileFinished

if TYPE_CHECKING:
    from tgmonitor.core.media_service.media_service import MediaService

log = logging.getLogger(__name__)

# 2026-11-25 P1-5:分页页大小(测试可 monkeypatch 压小验证截断场景)。
_RECONCILE_PAGE_SIZE = 500


async def _collect_referenced_keys(
    ms: MediaService,
    channel_ids: list[int],
    referenced_keys: set[str],
) -> None:
    """分页收集全部 DONE 媒体 + thumb 的 object_key 进 `referenced_keys`。

    2026-11-25 P1-5:旧实现 `list_messages([ids], limit=100_000)` 只取
    **最近** 10 万条 — 库内消息超限时,更旧消息的 object_key 进不了
    referenced_keys,Prune Orphans(dry_run=False)会把仍在引用的真实媒体
    误判孤儿真删。改为 `list_media` 按 DATE ASC 分页直到空页,收集**全部**
    DONE 媒体的引用(ASC 排序下新消息追加在尾部,已取窗口稳定,不受实时
    写入影响)。

    thumb 收集按 message 去重 — 一条消息 N 个 media 行会在分页里重复出现。
    注意 thumb 收集不能只跟随 DONE 的 media 行 — 主图 FAILED 的消息 thumb
    仍可能是 DONE(monitor/service.py:626),所以对分页中出现的每条消息都
    查一次 thumb。
    """
    offset = 0
    thumb_seen: set[tuple[int, int]] = set()
    while True:
        rows = await ms._storage.list_media(
            channel_ids=channel_ids,
            limit=_RECONCILE_PAGE_SIZE,
            offset=offset,
            sort=SortKey.DATE,
            sort_dir=SortDir.ASC,
        )
        for m, _idx, med in rows:
            if med.object_key and med.download_status == MediaDownloadStatus.DONE:
                referenced_keys.add(med.object_key)
            # 2026-09-29:同时收集原图 + 缩略图引用;thumb 走独立
            # `thumbnails` 表,逐 message 调 `list_thumbnails_for_message`
            # 拉 thumb DTO(media_idx 索引保证与父 media 一一对应)。
            tk = (m.channel_id, m.telegram_msg_id)
            if tk not in thumb_seen:
                thumb_seen.add(tk)
                for t in await ms._storage.list_thumbnails_for_message(*tk):
                    if t.object_key and t.download_status == MediaDownloadStatus.DONE:
                        referenced_keys.add(t.object_key)
        if len(rows) < _RECONCILE_PAGE_SIZE:
            break
        offset += _RECONCILE_PAGE_SIZE


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
            await _collect_referenced_keys(
                ms,
                [c.id for c in chs],
                referenced_keys,
            )
        orphans = scanned_keys - referenced_keys
        deleted = 0
        if not dry_run and ms._objects is not None and orphans:
            for k in orphans:
                try:
                    await ms._objects.delete(k)
                    deleted += 1
                except Exception:
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
