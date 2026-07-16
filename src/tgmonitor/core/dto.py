"""跨边界传输的纯数据对象(DTO)。

所有跨层(core 内部、core↔UI、core↔Exporter)传输都用 DTO;
绝不传递 TDLib 原生对象、ORM 行对象或框架特定的类型。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

# ---------- 频道 ----------

@dataclass
class ChannelDTO:
    """一个 Telegram 频道/群组的元数据 + 元数据同步时间 + 订阅标志。

    - `is_subscribed`: 用户是否把它加进了监听白名单(跟"已被全量 sync 发现"
      是两件事 — sync 可能发现很多频道,用户只挑其中几个订阅)。
    - `last_synced_at`: 元数据最近一次被 sync 刷新的时间;消息同步时间走
      `MessageDTO.date`,不要混。
    """

    id: int                                  # Telegram chat_id(全局唯一)
    title: str
    username: str | None = None              # 公开频道如 @example;私有无
    kind: str = "channel"                    # channel | supergroup | basic_group
    member_count: int | None = None
    created_at: datetime | None = None
    is_subscribed: bool = False
    last_synced_at: datetime | None = None

    @property
    def display(self) -> str:
        return f"@{self.username}" if self.username else f"#{self.id} {self.title}"


# ---------- 媒体 ----------

class MediaType(str, Enum):
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    STICKER = "sticker"
    ANIMATION = "animation"
    VIDEO_NOTE = "video_note"


@dataclass
class MediaDTO:
    """一条消息附带的媒体。

    二进制存于 ObjectStore,DB 只存 `object_key` + `backend` 引用。
    缩略图同样入 ObjectStore(`thumb_key` / `thumb_backend`)。
    """

    # 类型 & 元数据
    type: MediaType
    mime_type: str | None = None
    file_name: str | None = None
    file_size: int | None = None
    width: int | None = None
    height: int | None = None
    duration: int | None = None              # 秒

    # Telegram 侧标识
    telegram_file_id: str | None = None      # TDLib remote file_id,用于按需重下

    # ObjectStore 引用(原文件)
    object_key: str | None = None
    object_backend: str | None = None        # 'local' | 's3'

    # ObjectStore 引用(缩略图)
    thumb_key: str | None = None
    thumb_backend: str | None = None

    # Sticker 专属 — 关联的 emoji 字符(如 "😀");其它 type 始终 None
    emoji: str | None = None


# ---------- 消息 ----------

@dataclass
class MessageDTO:
    """一条已落库(或即将落库)的消息。"""

    id: int                                 # 自增主键,DB 分配
    channel_id: int                         # FK → channels.id
    telegram_msg_id: int                    # 在该频道内的 message_id
    author: str | None = None
    date: datetime = field(default_factory=datetime.utcnow)
    text: str = ""
    views: int | None = None
    forwards: int | None = None
    reply_to_msg_id: int | None = None
    edited: bool = False
    media: list[MediaDTO] = field(default_factory=list)
    raw: dict[str, Any] | None = None       # 可选:原始 TDLib payload 摘要(供高级导出)

    @property
    def has_media(self) -> bool:
        return bool(self.media)


# ---------- 导出 ----------

class ExportFormat(str, Enum):
    JSON = "json"
    CSV = "csv"
    MARKDOWN = "markdown"
    HTML = "html"


@dataclass
class ExportRequest:
    channel_ids: list[int]
    date_from: datetime | None = None
    date_to: datetime | None = None
    format: ExportFormat = ExportFormat.JSON
    out_path: str = ""
    include_media_meta: bool = True
    include_thumbnails: bool = False         # HTML 用:把缩略图内嵌


@dataclass
class ExportResult:
    out_path: str
    message_count: int
    bytes_written: int


# ---------- 全量同步(ChannelSyncService) ----------

@dataclass
class SyncOptions:
    """用户选的全量同步 options。"""

    include_metadata: bool = True
    include_history: bool = True
    history_limit: int | None = None      # None = 拉全部历史
    chat_delay_ms: int = 500               # 单条 API 间隔(防封号)
    page_delay_ms: int = 1000              # getChatHistory 分页间
    resume_from_saved: bool = True         # True: 从 storage max_msg_id 续拉


@dataclass
class ChannelSyncResult:
    """单个频道的同步结果。"""

    channel_id: int
    metadata_updated: bool = False
    messages_added: int = 0          # 本轮新落库的消息数(去重后)
    history_ended_at_msg_id: int | None = None  # 本轮拉到最早/最新的 msg_id
    error: str | None = None
    rate_limited: bool = False


@dataclass
class SyncResult:
    """全量同步整轮结果。"""

    per_channel: dict[int, ChannelSyncResult] = field(default_factory=dict)
    total_messages_added: int = 0
    rate_limited_seconds: float | None = None
    cancelled: bool = False
