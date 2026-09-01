"""ZIP 导出 — 2026-09-01 v1.5.1 PR #B4。

把 messages 里附带 media 的二进制文件打成 .zip:
- 每条 media 一个 entry(只打包 `download_status == DONE` 的)
- 顶部写一个 `_manifest.json`(每条 message 的 dataclass asdict,便于
  解压后用脚本批量入库)
- 可选打包缩略图:`include_thumbnails=True` 时同时写 `thumb_<arcname>`
- arcname 走 `_sanitize_arcname` 防 Zip Slip(`..` 段替换成 `_`)

设计要点:
- 走 `zipfile.ZIP_DEFLATED`(压缩);非 ZIP_STORED 是为了大体积时 IO 减半
- `object_store` 必须非 None — ZIP 没数据等于空包
- `MediaDownloadStatus != DONE` 的 media 直接 skip(没文件可打);FAILED
  / DOWNLOADING / PENDING 的 media **不会进 zip**(在 manifest 还能看到
  元数据,便于用户回查)
- `object_store.get` 当前是全量内存(BytesIO) — 2026-09-01 PR #B4 接受
  限制:建议单文件 < 100MB,否则进程内存峰值会爆。后续 PR 切真流式。

实现约束:沿用 `Exporter.render` ABC 签名;dispatcher(`ExportService.
_run_messages`)把拉好的 messages 一次性传进来 — `ZipExporter` 不用
关心分页 / 进度,框架已保证写盘前的消息全在内存。
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from tgmonitor.core.dto import (
    ChannelDTO,
    ExportFormat,
    MediaDownloadStatus,
    MessageDTO,
)
from tgmonitor.core.export.base import Exporter, exporter

if TYPE_CHECKING:
    from tgmonitor.core.objectstore.base import ObjectStore

log = logging.getLogger(__name__)


# Zip Slip 防御:`..` 段 + 控制字符。`/ ` 与 `\ ` 都视为分隔符(Wind
# -ows 解压工具也认 `\`)。
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_arcname(name: str) -> str:
    """防 Zip Slip + 控制字符:把 `../etc/passwd` 改成 `_/_etc/passwd`。

    空字符串 / 纯 `..` 也兜底;返回永远是非空 POSIX 相对路径(以 `/`
    分隔,Windows 解压也认)。
    """
    # 反斜杠转正斜杠,去前缀斜杠
    s = name.replace("\\", "/").lstrip("/")
    # 切段:任何 `..` 段都换成 `_`(Zip Slip 防御 — 即使 writestr 现代
    # 版本会拒绝,但防嵌套目录穿透更稳)。
    parts = [_CONTROL_CHARS.sub("_", p) if p and p != ".." else "_" for p in s.split("/")]
    # 过滤空段(连续 `//` 合并)
    parts = [p for p in parts if p]
    return "/".join(parts) if parts else "unnamed"


@exporter(ExportFormat.ZIP)
class ZipExporter(Exporter):
    """把 messages + media 二进制打成 .zip — PR #B4 落地。

    arcname 格式:`{telegram_msg_id}_{media_idx}_{file_name}`,过
    `_sanitize_arcname` 防 Zip Slip。同一 message 多条 media 用 idx 区分。

    单条消息导出走 `ExportRequest.single_message_id`(`ExportService
    _run_messages` 检测到非 None 时改走 `storage.get_message`),这里
    不区分两种调用 — 接收到的 `messages` 列表要么是 1 条,要么是
    N 条,行为一致。
    """

    format = ExportFormat.ZIP

    async def render(
        self,
        out_path: Path,
        channels: dict[int, ChannelDTO],
        messages: list[MessageDTO],
        *,
        object_store: ObjectStore | None = None,
        include_thumbnails: bool = False,
    ) -> int:
        """写 zip → 返回字节数。

        `object_store` 为 None 时直接 raise — ZIP 没有二进制就等于空
        包,这种调用是用户配错,不要静默吞。
        """
        if object_store is None:
            raise ValueError("ZipExporter 需要 object_store(没数据可打)")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        written = 0
        # ZIP_DEFLATED 压缩;level 默认 6 平衡速度 / 体积
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # 先写 manifest(顶层 metadata,解压后用户能直接 JSON 解析)
            manifest = json.dumps(
                [asdict(m) for m in messages],
                default=str,
                ensure_ascii=False,
            ).encode("utf-8")
            zf.writestr("_manifest.json", manifest)

            for msg in messages:
                for idx, media in enumerate(msg.media):
                    # 只打 DONE 的 media;FAILED / PENDING / DOWNLOADING 跳过
                    if media.download_status != MediaDownloadStatus.DONE:
                        continue
                    if not media.object_key:
                        continue
                    try:
                        blob = await object_store.get(media.object_key)
                    except KeyError:
                        log.warning(
                            "zip export: media object_key 不存在,跳过: msg_id=%s idx=%d key=%s",
                            msg.telegram_msg_id,
                            idx,
                            media.object_key,
                        )
                        continue
                    arcname = _sanitize_arcname(
                        f"{msg.telegram_msg_id}_{idx}_{media.file_name or media.object_key}"
                    )
                    zf.writestr(arcname, blob)
                    written += len(blob)
                    # 缩略图(可选):命名 `thumb_<arcname>`,失败也跳
                    if include_thumbnails and media.thumb_key:
                        try:
                            thumb_blob = await object_store.get(media.thumb_key)
                        except KeyError:
                            log.warning(
                                "zip export: thumb_key 不存在,跳过: msg_id=%s idx=%d key=%s",
                                msg.telegram_msg_id,
                                idx,
                                media.thumb_key,
                            )
                            continue
                        zf.writestr(f"thumb_{arcname}", thumb_blob)
                        written += len(thumb_blob)
        return out_path.stat().st_size  # noqa: ASYNC240 — 同步 stat 写盘后的路径
