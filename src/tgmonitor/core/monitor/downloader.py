"""MediaDownloader — 按 telegram_file_id 异步下载原文件入 ObjectStore。

2026-10-10 v1.12.x plan-batch2 目标 2 从 `service.py` 抽出(原本 240 LOC
嵌在 1,278 LOC service.py 末尾)。REVIEW M2.1 真实现见 class docstring。

设计:`MediaDownloader` 自持 `client / storage / objects / max_bytes` — 无
状态共享,可独立单测。`MonitorService` 持 optional `downloader`,FULL
媒体策略时通过 `_download_worker` 串行消费。
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging

from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, ThumbnailDTO
from tgmonitor.core.objectstore.base import ObjectMeta, ObjectStore
from tgmonitor.core.storage.repository import StorageRepository
from tgmonitor.core.telegram.client import TelegramClient

log = logging.getLogger(__name__)


class MediaDownloader:
    """按 telegram_file_id 异步下载原文件入 ObjectStore,然后回写 DB 的 object_key。

    REVIEW M2.1 真实现:之前 `download_one` 返回 None,FULL 模式下用户**下不到任何
    原文件**,只是元数据 + 缩略图 + 一个空 key。现在:
      - 入参:`media: MediaDTO`(含 `telegram_file_id` / `file_size` / `mime_type`)
      - 行为:`client.download_file(file_id)` 拿 bytes → `objects.put` → 返
        **更新过的** `MediaDTO`(`object_key` / `object_backend` / `file_size` 已填,
        `download_status=DONE`)
      - 失败**不抛也不返 None**,而是返回 `download_status=FAILED` +
        `download_error=<原因>` 的 MediaDTO —— 原因可落库、UI 可见(用户不再
        只能从日志猜"有记录无文件"是为什么)。

    边界(全部 → FAILED + 原因):
      - `telegram_file_id` 缺失 → "无 telegram_file_id"
      - `media.file_size > max_bytes` → "超过单文件上限"
      - `download_file` 返 None(超时 / 无数据)→ "下载失败/超时"
      - 实际下载 bytes > max_bytes(大小未知场景 hard cap)→ "实际大小超过上限"
      - `objects.put` 异常 → "对象存储写入失败: ..."
    """

    def __init__(
        self,
        client: TelegramClient,
        storage: StorageRepository,
        objects: ObjectStore,
        *,
        max_bytes: int = 200_000_000,
    ) -> None:
        """`max_bytes` = 单文件硬上限(0 = 无限制,默认 200 MB)。"""
        self.client = client
        self.storage = storage
        self.objects = objects
        self.max_bytes = max_bytes

    @staticmethod
    def make_key(media: MediaDTO, suffix: str = "") -> str:
        """生成稳定的对象 key:`media/<sha256[:16]>.<ext><suffix>`(内容寻址)。"""
        h = hashlib.sha256((media.telegram_file_id or media.file_name or "").encode()).hexdigest()[
            :16
        ]
        ext = (media.file_name or "").split(".")[-1] if media.file_name else "bin"
        return f"media/{h}.{ext}{suffix}"

    @staticmethod
    def make_thumb_key(thumb_fid: str, ext: str = "jpg") -> str:
        """2026-09-29:缩略图内容寻址 key `thumb/<sha256[:16]>.<ext>`(跟原图命名对称)。"""
        h = hashlib.sha256(thumb_fid.encode()).hexdigest()[:16]
        return f"thumb/{h}.{ext}"

    async def download_one(
        self,
        msg_pk: int,
        media: MediaDTO,
        *,
        force: bool = False,
        progress_callback=None,
    ) -> MediaDTO:
        """下载 → 入 ObjectStore → 返回更新后的 MediaDTO。

        skip-if-stored 顺序(2026-08-24):
          1) storage 已有同 `telegram_file_id` 的 DONE media → 拷字段,不发 TDLib 请求
          2) ObjectStore 已有同 `make_key(media)` → 视为已下载,补 object_key / backend
          3) 两个都 miss → 走原下载流程

        `force=True` 时跳过 #1 + #2(2026-08-24 Media Manager retry 路径用):用户
        显式要求「重新尝试」,即使已下载也再发 TDLib 请求覆盖。调用方有责任
        先 `objects.delete(old_key)` 清理旧 bytes,否则写入会覆盖。

        `progress_callback`(2026-09-01 v1.5.1 PR #B3):可选
        `await cb(downloaded: int, total: int | None)`,透传给
        `client.download_file`;skip-if-stored 命中时**不**调(没进度可报)。
        默认 None = 不上报(老调用方零改动)。

        成功:`object_key` / `object_backend` / `file_size` 已填,`download_status=DONE`;
        失败:`download_status=FAILED` + `download_error`(原因可持久化 / UI 展示),
        不抛异常。`msg_pk` 仅用于日志(消息主键,出问题时定位上下文)。
        """

        def failed(reason: str) -> MediaDTO:
            log.warning(
                "skip media msg_pk=%s %s: %s",
                msg_pk,
                media.file_name or media.telegram_file_id or media.type.value,
                reason,
            )
            return dataclasses.replace(
                media,
                download_status=MediaDownloadStatus.FAILED,
                download_error=reason,
            )

        fid = media.telegram_file_id
        if not fid:
            log.debug("skip media msg_pk=%s: no telegram_file_id", msg_pk)
            return failed("无 telegram_file_id")

        # 跳过 #1:storage 已有同 file_id 的成功下载 → 拷字段,不发 TDLib 请求
        # `force=True`(retry)时跳过 — 用户显式要求重试,即使 prior 已存也走原下载。
        if not force:
            prior = await self.storage.find_media_by_file_id(fid)
            if (
                prior is not None
                and prior.download_status == MediaDownloadStatus.DONE
                and prior.object_key
            ):
                log.debug(
                    "skip media msg_pk=%s: storage hit (fid=%s key=%s)",
                    msg_pk,
                    fid,
                    prior.object_key,
                )
                return dataclasses.replace(
                    media,
                    download_status=MediaDownloadStatus.DONE,
                    download_error=None,
                    object_key=prior.object_key,
                    object_backend=prior.object_backend,
                    file_size=prior.file_size,
                )

        # 跳过 #2:ObjectStore 已有该 key → 视为已下载,补字段
        # `force=True` 同样跳过。
        key = self.make_key(media)
        if not force and await self.objects.exists(key):
            log.debug(
                "skip media msg_pk=%s: objectstore hit (key=%s)",
                msg_pk,
                key,
            )
            return dataclasses.replace(
                media,
                download_status=MediaDownloadStatus.DONE,
                download_error=None,
                object_key=key,
                object_backend=self.objects.backend_name,
                file_size=media.file_size,
            )

        if self.max_bytes and media.file_size and media.file_size > self.max_bytes:
            return failed(f"文件 {media.file_size:,} 字节超过单文件上限 {self.max_bytes:,} 字节")
        data = await self.client.download_file(fid, progress_callback=progress_callback)
        if data is None:
            return failed("下载超时或未返回数据")
        # hard cap for unknown-size downloads(sticker / 加密附件 / file_size 不可信场景)
        if self.max_bytes and len(data) > self.max_bytes:
            return failed(f"实际下载 {len(data):,} 字节超过单文件上限 {self.max_bytes:,} 字节")
        meta = ObjectMeta(
            content_type=media.mime_type,
            size=len(data),
        )
        try:
            await self.objects.put(key, data, meta)
        except Exception as e:  # noqa: BLE001 — 契约:下载失败不抛,monitor 循环继续
            return failed(f"对象存储写入失败: {e}")
        # 返新 MediaDTO:保留原字段,只覆盖 object_key / object_backend / file_size
        # 与下载状态。dataclasses.replace 比 `__dict__` 解构更稳(保留 frozen /
        # __post_init__ 等),这里 MediaDTO 是普通 dataclass,replace() 同样适用。
        return dataclasses.replace(
            media,
            download_status=MediaDownloadStatus.DONE,
            download_error=None,
            object_key=key,
            object_backend=self.objects.backend_name,
            file_size=len(data),
        )

    async def download_thumb(
        self,
        msg_pk: tuple[int, int],
        media: MediaDTO,
    ) -> ThumbnailDTO | None:
        """2026-09-29:缩略图独立下载 — 入 ObjectStore `thumb/<sha256>.<ext>`
        prefix + 返 ThumbnailDTO 让 caller 落 `thumbnails` 表。

        - 入参 `msg_pk = (channel_id, telegram_msg_id)` — 关联键三元组前两条;
          `media_idx` 来自 caller(同消息内 media 索引)
        - 行为:`client.download_file(thumb_fid)` → `objects.put(thumb_key)` →
          返回 ThumbnailDTO(DONE);失败 → 返回 FAILED 状态的 ThumbnailDTO
        - 无 `media.thumbnail_telegram_file_id` → 返 None(caller 不落库)

        与 `download_one` 契约一致:不抛。`msg_pk` 仅日志用。
        """
        if not media.thumbnail_telegram_file_id:
            return None

        def failed(reason: str) -> ThumbnailDTO:
            log.warning(
                "skip thumb msg_pk=%s/%s %s: %s",
                msg_pk[0],
                msg_pk[1],
                media.file_name or media.telegram_file_id or media.type.value,
                reason,
            )
            return ThumbnailDTO(
                channel_id=msg_pk[0],
                telegram_msg_id=msg_pk[1],
                media_idx=-1,  # caller 必须在外部覆盖;此处仅兜底
                telegram_thumb_file_id=media.thumbnail_telegram_file_id,
                download_status=MediaDownloadStatus.FAILED,
                download_error=reason,
            )

        try:
            data = await self.client.download_file(media.thumbnail_telegram_file_id)
        except Exception as e:  # noqa: BLE001
            return failed(f"下载缩略图异常: {e}")
        if data is None:
            return failed("下载缩略图超时或未返回数据")
        # 单文件上限继承 main 的 200MB — thumbs 通常 <10KB,几乎不触发;但
        # 防御恶意/异常大文件把桶占满。
        if self.max_bytes and len(data) > self.max_bytes:
            return failed(f"缩略图 {len(data):,} 字节超过单文件上限")
        # 2026-09-29:内容寻址 thumb key(跟 main key 命名对称),`make_thumb_key`
        # 复用 main 的 sha16 ext 规则。
        ext = (media.mime_type or "").split("/")[-1] if media.mime_type else "jpg"
        # mime 不可信(/jpeg/.jpg/没值),与 history 兼容默认 .jpg
        if ext not in ("jpg", "jpeg", "png", "webp"):
            ext = "jpg"
        thumb_key = self.make_thumb_key(media.thumbnail_telegram_file_id, ext)
        meta = ObjectMeta(
            content_type=media.mime_type or "image/jpeg",
            size=len(data),
        )
        try:
            await self.objects.put(thumb_key, data, meta)
        except Exception as e:  # noqa: BLE001
            return failed(f"对象存储写入缩略图失败: {e}")
        return ThumbnailDTO(
            channel_id=msg_pk[0],
            telegram_msg_id=msg_pk[1],
            media_idx=-1,  # caller 覆盖
            object_key=thumb_key,
            object_backend=self.objects.backend_name,
            file_size=len(data),
            mime_type=meta.content_type,
            sha256=hashlib.sha256(data).hexdigest(),
            download_status=MediaDownloadStatus.DONE,
            telegram_thumb_file_id=media.thumbnail_telegram_file_id,
        )
