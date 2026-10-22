"""Media OS 集成 — 2026-10-15 plan-batch6-1.4 抽到子 module。

包含:
- `open_media` / `open_media_with_result` — 系统默认程序打开 media
- `reveal_in_folder` + `_spawn_reveal`(OS 子进程唤起文件管理器)
- `copy_media_path` — 写剪贴板(Local / Folder / S3 三分支)
- `_stage_s3_to_tmp` — S3 bytes 写到 tmp 文件
- `retry_media` — 重下 FAILED media(走 downloader)

**facade monkeypatch 契约(plan-batch3 + plan-batch6 关键约束)**:
- `AppService.__init__` 末尾用 `app._media = _media_actions` 形式
  monkeypatch 绑定 `retry_media` / `reveal_in_folder`(plan-batch3 拆的
  `_media_actions.py:53-151` 走 `await app._media.<method>(...)`)
- `_spawn_reveal` 是 `@staticmethod` — 由 `MediaService.__init_subclass__`
  末尾的 `MediaService._spawn_reveal = staticmethod(_media_os_actions._spawn_reveal)`
  显式绑定到 facade,保证 `await app._media.reveal_in_folder(...)` 内部
  调 `self._spawn_reveal(...)` 走 facade
- 拆分后,公共 method 签名 1:1 保留,`app._media.X` 调用面零变化

Qt callback 引用走 `_bridge._module_open_url_cb` /
`_bridge._module_tmp_root_cb`(同 1.1 + 1.2 模式),避免
`from ._qt_bridge import _module_open_url_cb` 绑死到子 module obj。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import mimetypes
import os
import secrets
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tgmonitor.core.dto import (
    CopyResult,
    MediaDownloadStatus,
    MediaDTO,
    OpenMediaResult,
    RevealResult,
)
from tgmonitor.core.events import MediaDownloaded, MediaRetried

if TYPE_CHECKING:
    from tgmonitor.core.media_service.media_service import MediaService

# 2026-10-15 plan-batch6-1.1 模式:走 module attr,monkeypatch 兼容
from . import _qt_bridge as _bridge

log = logging.getLogger(__name__)


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
    from tgmonitor.core.objectstore.folder_store import FolderObjectStore
    from tgmonitor.core.objectstore.local_store import LocalObjectStore
    from tgmonitor.core.objectstore.s3_store import S3ObjectStore

    msg = await ms._storage.get_message(channel_id, telegram_msg_id)
    if msg is None or media_idx >= len(msg.media):
        return OpenMediaResult(False, "消息或媒体不存在")
    med = msg.media[media_idx]
    if not med.object_key or med.download_status != MediaDownloadStatus.DONE:
        return OpenMediaResult(False, "媒体未下载完成")

    if _bridge._module_open_url_cb is None:
        return OpenMediaResult(False, "open_url callback 未注册(UI 层未 bootstrap)")

    try:
        if isinstance(ms._objects, (LocalObjectStore, FolderObjectStore)):
            # 用 backend 自带的 _path 而非 ms._objects._root / key —
            # FolderObjectStore 用 `media/<ab>/<cd>/<name>` 分片式相对路径,
            # 直接拼 root 会落到错的子目录。
            abs_path = ms._objects._path(med.object_key)  # noqa: SLF001
            # 2026-09-26 fix(open-media-hang):open_url 在 macOS 上同步走
            # `[NSWorkspace openURL:]` AppleEvent → LaunchServices,会卡
            # 主线程 100ms~数秒;放 worker thread 跑,UI 不冻。
            ok = bool(
                await asyncio.to_thread(
                    _bridge._module_open_url_cb,
                    str(abs_path),
                )
            )
            return (
                OpenMediaResult(True)
                if ok
                else OpenMediaResult(False, "系统调用失败:请检查是否已关联默认应用")
            )
        if isinstance(ms._objects, S3ObjectStore):
            tmp = await _stage_s3_to_tmp(ms, med)
            # 同上 — S3 路径 open_url 也走 worker thread,见上注。
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
    from tgmonitor.core.objectstore.folder_store import FolderObjectStore
    from tgmonitor.core.objectstore.local_store import LocalObjectStore
    from tgmonitor.core.objectstore.s3_store import S3ObjectStore

    msg = await ms._storage.get_message(channel_id, telegram_msg_id)
    if msg is None or media_idx >= len(msg.media):
        return RevealResult(False, "消息或媒体不存在")
    med = msg.media[media_idx]
    if not med.object_key or med.download_status != MediaDownloadStatus.DONE:
        return RevealResult(False, "媒体未下载完成")
    if isinstance(ms._objects, S3ObjectStore):
        return RevealResult(
            False,
            "S3 后端无本地路径:请使用「Copy 路径」拿到 s3:// URI",
        )
    if not isinstance(ms._objects, (LocalObjectStore, FolderObjectStore)):
        return RevealResult(
            False,
            f"不支持的对象存储后端: {type(ms._objects).__name__}",
        )

    try:
        abs_path = ms._objects._path(med.object_key)  # noqa: SLF001
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

    2026-10-15 plan-batch6-1.4:facade 末尾
    `MediaService._spawn_reveal = staticmethod(_media_os_actions._spawn_reveal)`
    显式绑定 — 保持 `app._media.reveal_in_folder(...)` 内部调
    `self._spawn_reveal(...)` 走 facade。
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
    from tgmonitor.core.objectstore.folder_store import FolderObjectStore
    from tgmonitor.core.objectstore.local_store import LocalObjectStore
    from tgmonitor.core.objectstore.s3_store import S3ObjectStore

    msg = await ms._storage.get_message(channel_id, telegram_msg_id)
    if msg is None or media_idx >= len(msg.media):
        return CopyResult(False, error="消息或媒体不存在")
    med = msg.media[media_idx]
    if not med.object_key or med.download_status != MediaDownloadStatus.DONE:
        return CopyResult(False, error="媒体未下载完成")
    if isinstance(ms._objects, (LocalObjectStore, FolderObjectStore)):
        try:
            abs_path = ms._objects._path(med.object_key)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            return CopyResult(
                False,
                error=f"{type(exc).__name__}: {exc}",
            )
        return CopyResult(True, copied_value=str(abs_path))
    if isinstance(ms._objects, S3ObjectStore):
        # S3ObjectStore 通常有 bucket / key 拼接;s3a URI 也可,这里用 s3://
        try:
            bucket = (
                getattr(ms._objects, "bucket_name", None)
                or getattr(ms._objects, "bucket", None)
                or getattr(ms._objects, "_bucket", None)
            )
        except Exception:  # noqa: BLE001
            bucket = None
        if not bucket:
            return CopyResult(
                False,
                error="S3 后端未暴露 bucket 字段",
            )
        uri = f"s3://{bucket}/{med.object_key}"
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


# ---------- retry_media ----------


async def retry_media(
    ms: MediaService,
    channel_id: int,
    telegram_msg_id: int,
    media_idx: int,
) -> None:
    """重下 FAILED media:`objects.delete(old_key)` + download_one(force=True)。

    非 FAILED 状态直接返回(不报错,UI 通常 disable Retry 按钮,这里兜底)。
    成功后回写 storage + 发 MediaDownloaded(LIVE view 据此刷新状态)。

    2026-10-15 plan-batch6-1.4:thin delegate → `core.media_service._media_os_actions`。
    `app._media = _media_actions` monkeypatch 契约保留(plan-batch3 已落
    `_media_actions.py:53-151` 调 `await app._media.retry_media(...)`,
    facade 签名 1:1 不变)。
    """
    msg = await ms._storage.get_message(channel_id, telegram_msg_id)
    if msg is None or media_idx >= len(msg.media):
        return
    med = msg.media[media_idx]
    if med.download_status != MediaDownloadStatus.FAILED:
        return
    old_object_key = med.object_key
    new_med = dataclasses.replace(
        med,
        object_key=None,
        object_backend=None,
        download_status=MediaDownloadStatus.PENDING,
        download_error=None,
    )
    new_media = list(msg.media)
    new_media[media_idx] = new_med
    new_msg = dataclasses.replace(msg, media=list(new_media))
    await ms._storage.update_message(new_msg)
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
    # 先发 MediaRetried,UI 立刻把状态切到 PENDING(避免用户重复点 Retry)
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
            )
        except Exception as e:  # noqa: BLE001
            log.exception("retry download failed: %s", e)
            updated = dataclasses.replace(
                new_med,
                download_status=MediaDownloadStatus.FAILED,
                download_error=f"重试异常: {e}",
            )
        # 回写最终状态
        final_media = list(new_msg.media)
        final_media[media_idx] = updated
        final_msg = dataclasses.replace(new_msg, media=list(final_media))
        await ms._storage.update_message(final_msg)
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
    "retry_media",
]
