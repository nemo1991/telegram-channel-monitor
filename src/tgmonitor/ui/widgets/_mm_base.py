# mypy: disable-error-code="attr-defined,has-type,unused-ignore"
"""Media Manager 共享常量与类型 — 2026-10-20 plan-batch10-α。

`media_manager_widget.py` 1149 LOC 拆 3 mixin + facade 的**第一步**:把
被 facade + 3 个 mixin 同时引用的类型 / emoji 映射抽到独立 module。

抽出原因:`_RowKey` / `_format_size` / `_MEDIA_ICONS` / `_STATUS_TEXT` /
`LIGHTBOX_PREVIEWABLE_TYPES` 5 项跨越 facade、`_mm_filter.RenderMixin`、
`_mm_actions.ActionsMixin` 三方共用。若留在 facade,mixin 就得
`from ...media_manager_widget import _RowKey` → **循环 import**
(facade 要 import mixin,mixin 又要 import facade)。独立 module 是唯一
干净解,依赖单向(facade / mixin → `_mm_base`,反向 0 依赖)。

**不含 UI 文案**:本 module 全部为纯常量与纯函数。
"""

from __future__ import annotations

from dataclasses import dataclass

from tgmonitor.core.dto import ChannelDTO, MediaDownloadStatus, MediaType

# ---------- 类型 / emoji 映射 ----------

_MEDIA_ICONS: dict[MediaType, str] = {
    MediaType.PHOTO: "图片",
    MediaType.VIDEO: "视频",
    MediaType.AUDIO: "音频",
    MediaType.VOICE: "语音",
    MediaType.DOCUMENT: "文档",
    MediaType.STICKER: "贴纸",
    MediaType.ANIMATION: "动图",
    MediaType.VIDEO_NOTE: "视频留言",
}

# 2026-08-31 v1.5.0 PR #A8:Lightbox 预览白名单 — 仅图片类(DONE 状态下)
# 可点缩略图弹大图;视频/音频/文档走 Open/Reveal,不动 lightbox。
# STICKER 算图片(WebP),ANIMATION 是 GIF。
# 2026-09-04 v1.6.7:扩展到 VIDEO / VIDEO_NOTE — MP4 bytes 进 Lightbox
# 内联播(QMediaPlayer);AUDIO / VOICE / DOCUMENT 仍走系统查看器。
# 公共常量(无下划线前缀)— message_detail 也 import 同一份,避免双拷贝。
# 2026-10-20 plan-batch10-α:定义搬到本 module;`media_manager_widget`
# 顶部 `from ..._mm_base import LIGHTBOX_PREVIEWABLE_TYPES` re-export,
# 保 `message_detail.py:34` 的 import 路径 1:1 不变。
LIGHTBOX_PREVIEWABLE_TYPES: frozenset[MediaType] = frozenset(
    {
        MediaType.PHOTO,
        MediaType.STICKER,
        MediaType.ANIMATION,
        MediaType.VIDEO,
        MediaType.VIDEO_NOTE,
    }
)

_STATUS_TEXT: dict[MediaDownloadStatus, str] = {
    MediaDownloadStatus.PENDING: "⏳",
    MediaDownloadStatus.DOWNLOADING: "⏳",
    MediaDownloadStatus.DONE: "✓",
    MediaDownloadStatus.FAILED: "×",
}


@dataclass(frozen=True)
class _RowKey:
    """MediaRow 的稳定地址 — `(channel_id, telegram_msg_id, media_idx)`。"""

    channel_id: int
    telegram_msg_id: int
    media_idx: int


def _channel_name(ch: ChannelDTO) -> str:
    """频道名优先的展示串 — title 为主,缺失才退 `#id`。

    2026-11-22 修:`ChannelDTO.display` 对公开频道返回 `@username`,在媒体页
    下拉和「所属频道」列里看不到频道名。这里统一改成 title 优先;完整信息
    (`@username` / `#id title`)留给 tooltip,不占列表横向空间。
    """
    return ch.title.strip() if ch.title else f"#{ch.id}"


def _format_size(n: int | None) -> str:
    """人类可读大小(1.2MB / 800KB);None / 0 → '?'。"""
    if not n:
        return "?"
    units = ("B", "KB", "MB", "GB")
    size = float(n)
    unit_idx = 0
    while size >= 1024 and unit_idx < len(units) - 1:
        size /= 1024
        unit_idx += 1
    return f"{size:.1f}{units[unit_idx]}" if unit_idx > 0 else f"{int(size)}B"
