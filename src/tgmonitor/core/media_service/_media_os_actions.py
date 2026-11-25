"""Media OS 集成 — 2026-10-15 plan-batch6-1.4 抽到子 module。

包含:
- `open_media` / `open_media_with_result` — 系统默认程序打开 media
- `reveal_in_folder` + `_spawn_reveal`(OS 子进程唤起文件管理器)
- `copy_media_path` — 写剪贴板(Local / Folder / S3 三分支)
- `_stage_s3_to_tmp` — S3 bytes 写到 tmp 文件
- `download_media` — 下载未下载 media 原文件(force=False;v2.0.9 FR-06)
- `download_all_pending` — 批量下载筛选视图里的未下载媒体(2026-11-22 用户选型)
- `retry_media` — 重下 FAILED media(force=True,清旧 bytes)

2026-11-11 v2.0.0 Phase 3:本 module 是 retry / reveal 的唯一实现
(AppService 侧重复实现已删)。`_spawn_reveal` 是 module-level plain
function,测试 monkeypatch 本 module 属性即可拦截 OS 子进程唤起。

Qt callback 引用走 `_bridge._module_open_url_cb` /
`_bridge._module_tmp_root_cb`(同 1.1 + 1.2 模式),避免
`from ._qt_bridge import _module_open_url_cb` 绑死到子 module obj。
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
import mimetypes
import os
import secrets
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tgmonitor.core.dto import (
    CopyResult,
    MediaDownloadStatus,
    MediaDTO,
    MediaType,
    OpenMediaResult,
    RevealResult,
)
from tgmonitor.core.events import MediaDownloaded, MediaDownloadProgress, MediaRetried

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from tgmonitor.core.media_service.media_service import MediaService

# 2026-10-15 plan-batch6-1.1 模式:走 module attr,monkeypatch 兼容
from . import _qt_bridge as _bridge

log = logging.getLogger(__name__)


def _guard_inflight(func: Any) -> Any:
    """下载/重下入口的 in-flight 守卫装饰器(2026-11-23)。

    同一 `(channel_id, telegram_msg_id, media_idx)` 已有下载在跑时,二次调用
    直接 no-op —— 防双击 / 批量+行内并发写同一 object_key。用 `ms` 上的 set
    而非模块全局,多 MediaService 实例互不干扰;finally 保证异常也释放。

    兼容旧测试桩:桩没有 `_inflight_downloads` 属性时跳过守卫(行为不变)。
    """

    @functools.wraps(func)
    async def wrapper(
        ms: MediaService,
        channel_id: int,
        telegram_msg_id: int,
        media_idx: int,
    ) -> Any:
        inflight: set[tuple[int, int, int]] | None = getattr(ms, "_inflight_downloads", None)
        if inflight is None:
            return await func(ms, channel_id, telegram_msg_id, media_idx)
        key = (channel_id, telegram_msg_id, media_idx)
        if key in inflight:
            log.info(
                "skip duplicate download in flight: %s/%s/%d",
                channel_id,
                telegram_msg_id,
                media_idx,
            )
            return None
        inflight.add(key)
        try:
            return await func(ms, channel_id, telegram_msg_id, media_idx)
        finally:
            inflight.discard(key)

    return wrapper


def _make_progress_callback(
    ms: MediaService,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> Callable[[int, int | None], Awaitable[None]]:
    """构造进度回调 → 发 `MediaDownloadProgress` 事件给 UI 刷新状态列。

    2026-11-23 v2.0.9:手动下载/重试原先不传 progress_callback —— 整段下载
    期间 UI 无任何事件,用户点完像「没反应」。这里复用 monitor 下载队列
    (`service.py:_download_worker`)同一事件契约;节流由 Telegram 客户端的
    `_make_progress_wrapper` 负责(0.5s/次),此处直接透传。
    """

    async def _on_progress(downloaded: int, total: int | None) -> None:
        await ms._bus.publish(
            MediaDownloadProgress(
                channel_id=channel_id,
                telegram_msg_id=telegram_msg_id,
                media_idx=media_idx,
                downloaded=downloaded,
                total=total,
            )
        )

    return _on_progress


async def _write_media_update(
    ms: MediaService,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
    med: MediaDTO,
) -> None:
    """P2 #36:定向回写 media[media_idx] = med(只动 media 行,不碰消息其它列)。

    旧路径 `update_message(整条 msg)` 会把下载期间(可达 30 分钟)并发
    编辑推送的新 text / views 用下载开始时的旧 DTO 覆盖回去。改为:重读
    当前消息 → 只替换目标位 media → `storage.update_message_media`
    (SQL 后端单事务 DELETE+重插 media 行)。消息不存在(下载期间被删)
    时 no-op。
    """
    current = await ms._storage.get_message(channel_id, telegram_msg_id)
    if current is None:
        log.warning(
            "skip media writeback: message gone: %s/%s",
            channel_id,
            telegram_msg_id,
        )
        return
    media_list = list(current.media)
    if media_idx < len(media_list):
        media_list[media_idx] = med
    else:
        media_list.append(med)
    await ms._storage.update_message_media(channel_id, telegram_msg_id, media_list)


# ---------- open_media / open_media_with_result ----------


async def open_media(
    ms: MediaService,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> bool:
    """系统默认程序打开 media 文件。True = 成功发起,False = 不可打开。

    2026-08-25 v1.3.0 PR #5:扩展到 S3 后端(走 `_stage_s3_to_tmp`)并暴露
    失败原因。返回 bool 是向后兼容 wrapper — 真实实现走 `open_media_with_result`,
    UI 失败时显示 reason。

    2026-10-15 plan-batch6-1.4:thin delegate → `core.media_service._media_os_actions`。
    """
    return (await open_media_with_result(ms, channel_id, telegram_msg_id, media_idx)).success


async def open_media_with_result(
    ms: MediaService,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> OpenMediaResult:
    """打开 media + 返回结构化结果(2026-08-25 v1.3.0 PR #5)。

    Local / Folder:直接调 UI 注入的 open_url callback(等价于
    `QDesktopServices.openUrl(QUrl.fromLocalFile(path))`)。
    S3:把 ObjectStore bytes 写到 UI 注入的 tmp_root callback 返的目录
    下的 tmp 文件,再 open_url;tmp 成功路径不主动 unlink(交给 OS 在
    app exit / 重启时回收;Windows 上 OS 持有 handle 时 unlink 会失败,
    故意不冒此风险),失败路径显式 unlink 兜底。

    所有异常(对象存储连接断 / get 失败 / tmp 写失败 / open_url 返 False)都
    收口到 OpenMediaResult,不让 UI 看到原始堆栈。

    2026-10-04 plan 阶段 2.2:Qt 调用抽到 `tgmonitor.ui._qt_callbacks`,
    core 完全不 import PySide6。callbacks 未注册(CLI 路径 / 没调
    `register_qt_callbacks`)时返失败 DTO 而不是抛 — 与现有
    `objects=None` 失败语义一致。
    """
    msg = await ms._storage.get_message(channel_id, telegram_msg_id)
    if msg is None or media_idx >= len(msg.media):
        return OpenMediaResult(False, "消息或媒体不存在")
    med = msg.media[media_idx]
    if not med.object_key or med.download_status != MediaDownloadStatus.DONE:
        return OpenMediaResult(False, "媒体未下载完成")

    if _bridge._module_open_url_cb is None:
        return OpenMediaResult(False, "open_url callback 未注册(UI 层未 bootstrap)")

    try:
        objects = ms._objects
        if objects is None:
            return OpenMediaResult(False, "不支持的对象存储后端: NoneType")
        # P1-C3:能力探测替代 isinstance(Local/Folder/S3)。有 local_path →
        # 直接开本地文件;无本地路径但有 uri(远端后端)→ stage 到 tmp 再开。
        local = objects.local_path(med.object_key)
        if local is not None:
            # 2026-09-26 fix(open-media-hang):open_url 在 macOS 上同步走
            # `[NSWorkspace openURL:]` AppleEvent → LaunchServices,会卡
            # 主线程 100ms~数秒;放 worker thread 跑,UI 不冻。
            ok = bool(
                await asyncio.to_thread(
                    _bridge._module_open_url_cb,
                    str(local),
                )
            )
            return (
                OpenMediaResult(True)
                if ok
                else OpenMediaResult(False, "系统调用失败:请检查是否已关联默认应用")
            )
        if objects.uri(med.object_key) is not None:
            tmp = await _stage_s3_to_tmp(ms, med)
            # 同上 — 远端路径 open_url 也走 worker thread,见上注。
            ok = bool(
                await asyncio.to_thread(
                    _bridge._module_open_url_cb,
                    str(tmp),
                )
            )
            if not ok:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                return OpenMediaResult(
                    False,
                    f"系统调用失败:无法打开临时文件 {tmp.name}",
                )
            return OpenMediaResult(True)
        return OpenMediaResult(
            False,
            f"不支持的对象存储后端: {type(ms._objects).__name__}",
        )
    except Exception as exc:  # noqa: BLE001 — 收口,UI 不应见堆栈
        return OpenMediaResult(False, f"{type(exc).__name__}: {exc}")


# ---------- reveal_in_folder / _spawn_reveal ----------


async def reveal_in_folder(
    ms: MediaService,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> RevealResult:
    """2026-08-27 v1.4.0 PR #16:在文件管理器中高亮 media 文件(macOS
    Finder / Windows Explorer / Linux xdg-open 父目录)。

    仅 Local / Folder 后端有效:S3 无本地文件,S3 路径应走 `copy_media_path`
    拿 URI。失败原因以 `RevealResult.error` 返回,UI 据此弹 QMessageBox。
    """
    msg = await ms._storage.get_message(channel_id, telegram_msg_id)
    if msg is None or media_idx >= len(msg.media):
        return RevealResult(False, "消息或媒体不存在")
    med = msg.media[media_idx]
    if not med.object_key or med.download_status != MediaDownloadStatus.DONE:
        return RevealResult(False, "媒体未下载完成")
    # P1-C3:能力探测替代 isinstance。S3 等远端后端有 uri 无 local_path →
    # 保留原"S3 后端无本地路径"提示;两者皆无 → 不支持。
    objects = ms._objects
    if objects is None:
        return RevealResult(False, "不支持的对象存储后端: NoneType")
    local = objects.local_path(med.object_key)
    if local is None:
        if objects.uri(med.object_key) is not None:
            return RevealResult(
                False,
                "S3 后端无本地路径:请使用「Copy 路径」拿到 s3:// URI",
            )
        return RevealResult(
            False,
            f"不支持的对象存储后端: {type(ms._objects).__name__}",
        )

    try:
        abs_path = local
        if not abs_path.exists():
            return RevealResult(False, f"文件不存在: {abs_path}")
        # OS-specific 唤起文件管理器(macOS `open -R` 高亮 / Windows
        # `explorer /select,` / Linux `xdg-open <parent_dir>`)
        await asyncio.to_thread(
            _spawn_reveal,
            abs_path,
            sys.platform,
        )
        return RevealResult(True)
    except Exception as exc:  # noqa: BLE001 — 收口,UI 不应见堆栈
        return RevealResult(False, f"{type(exc).__name__}: {exc}")


def _spawn_reveal(abs_path, platform: str) -> None:
    """2026-08-27 v1.4.0 PR #16:同步 spawn 子进程唤起 OS 文件管理器。

    - macOS:`open -R <abs_path>`(在 Finder 高亮该文件)
    - Windows:`explorer /select,<abs_path>`(在 Explorer 高亮)
    - Linux / 其它:`xdg-open <abs_path.parent>`(打开父目录,Linux 无
      标准「高亮」API,降级开父目录)

    测试 monkeypatch 本 module 的 `_spawn_reveal` 属性即可拦截。
    """
    import subprocess

    if platform == "darwin":
        subprocess.Popen(["open", "-R", str(abs_path)])
    elif platform == "win32":
        subprocess.Popen(["explorer", f"/select,{abs_path}"])
    else:
        subprocess.Popen(["xdg-open", str(abs_path.parent)])


# ---------- copy_media_path ----------


async def copy_media_path(
    ms: MediaService,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> CopyResult:
    """2026-08-27 v1.4.0 PR #16:把 media 路径 / URI 写入剪贴板。

    - Local / Folder:绝对路径(`<root>/<object_key>`)
    - S3:`s3://<bucket>/<object_key>`(URI 字符串,不是本地路径)
    - 其它后端:失败 + 不支持提示
    """
    msg = await ms._storage.get_message(channel_id, telegram_msg_id)
    if msg is None or media_idx >= len(msg.media):
        return CopyResult(False, error="消息或媒体不存在")
    med = msg.media[media_idx]
    if not med.object_key or med.download_status != MediaDownloadStatus.DONE:
        return CopyResult(False, error="媒体未下载完成")
    # P1-C3:能力探测替代 isinstance。local_path 优先(本地 FS 绝对路径),
    # 否则 uri(S3 s3:// 或未来远端后端);两者皆无 → 不支持。
    objects = ms._objects
    if objects is None:
        return CopyResult(False, error="不支持的对象存储后端: NoneType")
    local = objects.local_path(med.object_key)
    if local is not None:
        try:
            return CopyResult(True, copied_value=str(local))
        except Exception as exc:  # noqa: BLE001
            return CopyResult(False, error=f"{type(exc).__name__}: {exc}")
    uri = objects.uri(med.object_key)
    if uri is not None:
        return CopyResult(True, copied_value=uri)
    return CopyResult(
        False,
        error=f"不支持的对象存储后端: {type(ms._objects).__name__}",
    )


# ---------- _stage_s3_to_tmp ----------


async def _stage_s3_to_tmp(ms: MediaService, med: MediaDTO) -> Path:
    """2026-08-25 v1.3.0 PR #5:把 S3 media bytes 写到本地 tmp 文件用于
    `open_url callback`(等价 `QDesktopServices.openUrl(QUrl.fromLocalFile(...))`)。

    - 扩展名推断优先级:`med.file_name` 后缀 > `med.mime_type` 查
      `mimetypes.guess_extension` > `.bin` fallback
    - tmp 目录:`tmp_root callback` 返的路径(macOS = `QStandardPaths.
      writableLocation(QStandardPaths.StandardLocation.TempLocation)`),
      callback 未注册时回退 `~/.cache/tgmonitor`
    - 文件名:`tgmonitor-<secrets.token_hex(8)><suffix>` — `tgmonitor-`
      前缀留给未来 sweep 工具批量清理
    - 写文件用 `asyncio.to_thread(tmp_path.write_bytes, data)` 防卡 loop

    2026-10-04 plan 阶段 2.2:`QStandardPaths` 调用抽到 UI 层 callback,
    core 完全不 import PySide6。
    """
    # 1. suffix
    suffix = ""
    if med.file_name:
        suffix = os.path.splitext(med.file_name)[1]
    if not suffix and med.mime_type:
        guessed = mimetypes.guess_extension(med.mime_type, strict=False)
        if guessed:
            suffix = guessed
    if not suffix:
        suffix = ".bin"

    # 2. tmp 目录 — callback 未注册时用 `~/.cache/tgmonitor` fallback
    # (CLI 路径 / 测试不调 open_url → 不会触发,fallback 不影响)
    tmp_dir: Path
    if _bridge._module_tmp_root_cb is None:
        tmp_dir = Path.home() / ".cache" / "tgmonitor"
    else:
        tmp_dir = _bridge._module_tmp_root_cb()
    await asyncio.to_thread(tmp_dir.mkdir, parents=True, exist_ok=True)

    # 3. 写文件
    tmp_path = tmp_dir / f"tgmonitor-{secrets.token_hex(8)}{suffix}"
    assert ms._objects is not None  # ensure_objects 已检查
    data = await ms._objects.get(med.object_key or "")
    await asyncio.to_thread(tmp_path.write_bytes, data)
    return tmp_path


# ---------- download_media(未下载媒体首次下载) ----------


@_guard_inflight
async def download_media(
    ms: MediaService,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> None:
    """下载「未下载」media 的原文件(2026-11-22 v2.0.9 FR-06 / D5=β)。

    2026-11-22:「未下载」由 `download_status is None` 表达(PENDING 枚举已删)。

    与 `retry_media` 的区别:
    - 状态守卫:未下载(None)或 DOWNLOADING 续传才下;重试只接 FAILED
    - 不清旧 bytes:首次下载无旧字节;即使有,`download_one(force=False)` 的
      skip-if-stored 快路径会直接返回 DONE,不产生重复下载
    - force=False:已有字节直接复用;重试 force=True 强制重下损坏文件
    - 不发 MediaRetried(那是「重下」语义);成功/失败仍发 MediaDownloaded,
      VM 据此刷新列表
    """
    msg = await ms._storage.get_message(channel_id, telegram_msg_id)
    if msg is None or media_idx >= len(msg.media):
        return
    med = msg.media[media_idx]
    if med.download_status not in (None, MediaDownloadStatus.DOWNLOADING):
        return

    # 2026-11-22 修:无 downloader 时保持「未下载」(None),写原因供 UI tooltip 显示。
    # 原实现先标 DOWNLOADING 落库、再因 `if ms._downloader is not None`
    # 静默返回 —— 状态永久卡在 DOWNLOADING,重启也不会 backfill(只有 FULL
    # 策略会入队),用户点「下载」再点「重试」都救不回来。
    if ms._downloader is None:
        stuck = dataclasses.replace(
            med,
            download_error="未配置下载器,无法下载原文件",
        )
        await _write_media_update(ms, channel_id, telegram_msg_id, media_idx, stuck)
        log.warning(
            "download_media: downloader 未配置,保持未下载: %s/%s/%d",
            channel_id,
            telegram_msg_id,
            media_idx,
        )
        # 发事件让 UI 重拉一次,以便 tooltip 显示原因(未下载不发通知)。
        await ms._bus.publish(
            MediaDownloaded(
                channel_id=channel_id,
                telegram_msg_id=telegram_msg_id,
                media=stuck,
            )
        )
        return

    # 标 DOWNLOADING 先落库(用户立刻可见「下载中」,崩溃后重启可续)。
    # P2 #36:走定向 media 回写,不整条 update_message。
    new_med = dataclasses.replace(
        med,
        download_status=MediaDownloadStatus.DOWNLOADING,
        download_error=None,
    )
    await _write_media_update(ms, channel_id, telegram_msg_id, media_idx, new_med)

    if ms._downloader is not None:
        try:
            updated = await ms._downloader.download_one(
                msg_pk=msg.id,
                media=new_med,
                force=False,
                progress_callback=_make_progress_callback(
                    ms, channel_id, telegram_msg_id, media_idx
                ),
            )
        except Exception as e:  # noqa: BLE001
            log.exception("download media failed: %s", e)
            updated = dataclasses.replace(
                new_med,
                download_status=MediaDownloadStatus.FAILED,
                download_error=f"下载异常: {e}",
            )
        # P2 #36:最终状态同样定向回写 media 行(text 不被 stale DTO 覆盖)。
        await _write_media_update(ms, channel_id, telegram_msg_id, media_idx, updated)
        await ms._bus.publish(
            MediaDownloaded(
                channel_id=channel_id,
                telegram_msg_id=telegram_msg_id,
                media=updated,
            )
        )


# ---------- download_all_pending(批量未下载首下) ----------


async def download_all_pending(
    ms: MediaService,
    *,
    channel_id: int | None = None,
    search: str = "",
    media_type: MediaType | None = None,
) -> int:
    """批量下载当前筛选视图的全部「未下载」媒体 — 返回发起下载数。

    2026-11-22 用户选型:历史积压的未下载媒体一次清掉。先分页把所有 key 收齐
    再逐个下 —— 不能边遍历边下,状态从 None 变 DONE 会让后续 offset 跳过行。
    每条仍走 `download_media` 的守卫与事件,失败转 FAILED 不会中断批次。
    """
    page_size = 500
    keys: list[tuple[int, int, int]] = []
    offset = 0
    while True:
        rows = await ms._storage.list_media(
            channel_ids=[channel_id] if channel_id is not None else None,
            undownloaded=True,
            media_type=media_type,
            search=search,
            limit=page_size,
            offset=offset,
        )
        keys.extend((msg.channel_id, msg.telegram_msg_id, idx) for msg, idx, _med in rows)
        if len(rows) < page_size:
            break
        offset += page_size

    for cid, mid, idx in keys:
        await download_media(ms, cid, mid, idx)
    return len(keys)


# ---------- retry_media ----------


@_guard_inflight
async def retry_media(
    ms: MediaService,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> None:
    """重下 FAILED media:`objects.delete(old_key)` + download_one(force=True)。

    非 FAILED 状态直接返回(不报错,UI 通常 disable Retry 按钮,这里兜底)。
    成功后回写 storage + 发 MediaDownloaded(LIVE view 据此刷新状态)。
    """
    msg = await ms._storage.get_message(channel_id, telegram_msg_id)
    if msg is None or media_idx >= len(msg.media):
        return
    med = msg.media[media_idx]
    if med.download_status != MediaDownloadStatus.FAILED:
        return
    old_object_key = med.object_key
    # 2026-11-22:原把 FAILED 重置为 PENDING 作中间态,枚举删 PENDING 后
    # 直接转 DOWNLOADING(重试已经在下,语义比「未下载」更准)。
    new_med = dataclasses.replace(
        med,
        object_key=None,
        object_backend=None,
        download_status=MediaDownloadStatus.DOWNLOADING,
        download_error=None,
    )
    # P2 #36:定向 media 回写(不整条 update_message)。
    await _write_media_update(ms, channel_id, telegram_msg_id, media_idx, new_med)
    # 清旧 bytes — 让 download_one(force=True) 一定走真下载路径
    if old_object_key and ms._objects is not None:
        try:
            await ms._objects.delete(old_object_key)
        except Exception:  # noqa: BLE001
            log.warning(
                "retry pre-clean bytes %s failed",
                old_object_key,
                exc_info=True,
            )
    # 先发 MediaRetried,UI 立刻把状态切到 DOWNLOADING(避免用户重复点 Retry)
    await ms._bus.publish(
        MediaRetried(
            channel_id=channel_id,
            telegram_msg_id=telegram_msg_id,
            media_idx=media_idx,
        )
    )
    # 然后走同步下载路径(retry 走 MediaService 直调,不走 worker queue —
    # 2026-08-24 D4:不增加 force flag 进 queue,避免协议变更)
    if ms._downloader is not None:
        try:
            updated = await ms._downloader.download_one(
                msg_pk=msg.id,
                media=new_med,
                force=True,
                progress_callback=_make_progress_callback(
                    ms, channel_id, telegram_msg_id, media_idx
                ),
            )
        except Exception as e:  # noqa: BLE001
            log.exception("retry download failed: %s", e)
            updated = dataclasses.replace(
                new_med,
                download_status=MediaDownloadStatus.FAILED,
                download_error=f"重试异常: {e}",
            )
        # 回写最终状态 — P2 #36:定向 media 回写(text 不被 stale DTO 覆盖)。
        await _write_media_update(ms, channel_id, telegram_msg_id, media_idx, updated)
        await ms._bus.publish(
            MediaDownloaded(
                channel_id=channel_id,
                telegram_msg_id=telegram_msg_id,
                media=updated,
            )
        )


__all__ = [
    "open_media",
    "open_media_with_result",
    "reveal_in_folder",
    "_spawn_reveal",
    "copy_media_path",
    "_stage_s3_to_tmp",
    "download_media",
    "download_all_pending",
    "retry_media",
]
