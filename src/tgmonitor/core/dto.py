"""跨边界传输的纯数据对象(DTO)。

所有跨层(core 内部、core↔UI、core↔Exporter)传输都用 DTO;
绝不传递 TDLib 原生对象、ORM 行对象或框架特定的类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
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

    id: int  # Telegram chat_id(全局唯一)
    title: str
    username: str | None = None  # 公开频道如 @example;私有无
    kind: str = "channel"  # channel | supergroup | basic_group
    member_count: int | None = None
    created_at: datetime | None = None
    is_subscribed: bool = False
    last_synced_at: datetime | None = None

    @property
    def display(self) -> str:
        """`@username`(有) 或 `#id title`(无 username)的展示串。"""
        return f"@{self.username}" if self.username else f"#{self.id} {self.title}"


# ---------- 媒体 ----------


class MediaType(str, Enum):
    """Telegram 媒体类型枚举(与 TDLib MessageContent 类型对应)。"""

    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    STICKER = "sticker"
    ANIMATION = "animation"
    VIDEO_NOTE = "video_note"


class MediaDownloadStatus(str, Enum):
    """媒体下载状态 — 用户可观察:「对象存储里没有文件」时能看出是正在下 / 失败。

    - `PENDING`    : 未安排下载(元数据策略 / 旧数据 / 无 downloader)
    - `DOWNLOADING`: 下载中(已入队或正在下;应用重启后视为可重新下载)
    - `DONE`       : 下载成功,`object_key` / `object_backend` 已填
    - `FAILED`     : 下载失败或被跳过,`download_error` 有原因
    """

    PENDING = "pending"
    DOWNLOADING = "downloading"
    DONE = "done"
    FAILED = "failed"


class SortKey(str, Enum):
    """Media Manager 排序键(2026-08-25 v1.3.0 PR #6)。

    - `DATE`  : 按 message.date — 默认,UI 显示"最新优先"
    - `SIZE`  : 按 media.file_size — 找大文件 / 找小文件
    - `STATUS`: 按 download_status — 把失败 / 下载中聚到一起看
    """

    DATE = "date"
    SIZE = "size"
    STATUS = "status"


class SortDir(str, Enum):
    """Media Manager 排序方向(2026-08-25 v1.3.0 PR #6)。"""

    ASC = "asc"
    DESC = "desc"


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
    duration: int | None = None  # 秒

    # Telegram 侧标识
    telegram_file_id: str | None = None  # TDLib remote file_id,用于按需重下

    # ObjectStore 引用(原文件)
    object_key: str | None = None
    object_backend: str | None = None  # 'local' | 's3'

    # ObjectStore 引用(缩略图)
    thumb_key: str | None = None
    thumb_backend: str | None = None

    # 下载状态(异步下载队列写入;持久化到各仓储)
    download_status: MediaDownloadStatus = MediaDownloadStatus.PENDING
    download_error: str | None = None  # FAILED 时的原因(超限 / 超时 / 存储写入失败…)

    # Sticker 专属 — 关联的 emoji 字符(如 "😀");其它 type 始终 None
    emoji: str | None = None


# ---------- 消息 ----------


@dataclass
class MessageDTO:
    """一条已落库(或即将落库)的消息。

    2026-08-27 v1.4.0 PR #9:补 5 个 TDLib Message 字段,详情面板 / 转发
    链 / via bot / 相册 / 置顶 一直空白,本次映射对齐。
    2026-08-27 v1.4.0 PR #10:加 reactions — TDLib
    `updateMessageInteractionInfo` 推 reactions 增量时使用。
    """

    id: int  # 自增主键,DB 分配
    channel_id: int  # FK → channels.id
    telegram_msg_id: int  # 在该频道内的 message_id
    author: str | None = None
    date: datetime = field(default_factory=lambda: datetime.now(UTC))
    text: str = ""
    views: int | None = None
    forwards: int | None = None
    reply_to_msg_id: int | None = None
    edited: bool = False
    media: list[MediaDTO] = field(default_factory=list)
    raw: dict[str, Any] | None = None  # 可选:原始 TDLib payload 摘要(供高级导出)
    # 2026-08-27 v1.4.0 PR #9:TDLib Message 暴露但 v1.3.0 丢弃的字段。
    forward_origin: dict[str, Any] | None = (
        None  # messageOrigin* 扁平 dict(`{"@type": "messageOriginUser", ...}`)
    )
    via_bot_user_id: int | None = None  # 通过 inline bot 发送时的 bot id
    media_album_id: int | None = None  # 同一相册的多张图共享,UI 用来分组
    is_pinned: bool = False  # 是否被频道置顶
    # 2026-08-27 v1.4.0 PR #10:reactions — TDLib
    # updateMessageInteractionInfo.interactions.reactions[] 扁平为 `ReactionDTO` 列表。
    # None = 没推送过(老消息);空 list = 推过但已被撤回干净。
    reactions: list[ReactionDTO] | None = None

    @property
    def has_media(self) -> bool:
        """消息是否带媒体(`media` 列表非空)。"""
        return bool(self.media)


@dataclass
class ReactionDTO:
    """TDLib reaction 单条 → 扁平 dataclass。

    2026-08-27 v1.4.0 PR #10:
    - `type`:emoji 自定义 emoji(TDLib 区分 `reactionEmoji` /
      `reactionCustomEmoji`,本字段统一记 `@type` 字符串)
    - `count`:投该 reaction 的人数
    - `is_chosen`:当前用户(我)是否也投了 — UI 高亮用
    """

    type: str = ""  # 'emoji' | 'custom_emoji' | '@type' raw 字符串
    emoji: str = ""  # 标准 emoji 文本("😀")或自定义 emoji 占位符
    count: int = 0
    is_chosen: bool = False

    def to_dict(self) -> dict[str, Any]:
        """导出 / 落库序列化格式。"""
        return {
            "type": self.type,
            "emoji": self.emoji,
            "count": self.count,
            "is_chosen": self.is_chosen,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReactionDTO:
        """导出 / 落库反序列化。"""
        return cls(
            type=d.get("type", ""),
            emoji=d.get("emoji", ""),
            count=int(d.get("count", 0)),
            is_chosen=bool(d.get("is_chosen", False)),
        )


# ---------- 导出 ----------


class ExportFormat(str, Enum):
    """导出格式枚举 — UI 下拉框选项 + Exporter 注册 key。"""

    JSON = "json"
    CSV = "csv"
    MARKDOWN = "markdown"
    HTML = "html"
    # 2026-08-25 v1.3.0 PR #7:Media Manager 当前视图(filter + sort + page)
    # 一键导出 CSV — schema 与 ExportRequest 完全不同,走独立 dispatcher。
    MEDIA_CSV = "media_csv"


@dataclass
class ExportRequest:
    """用户触发的导出请求(ExportService.run 接收)。"""

    channel_ids: list[int]
    date_from: datetime | None = None
    date_to: datetime | None = None
    format: ExportFormat = ExportFormat.JSON
    out_path: str = ""
    include_media_meta: bool = True
    include_thumbnails: bool = False  # HTML 用:把缩略图内嵌


@dataclass
class MediaExportRequest:
    """per-media 导出请求 — 2026-08-25 v1.3.0 PR #7。

    schema 跟 `ExportRequest` 完全不同(per-message vs per-media 行),独立
    dataclass;`ExportService.run` 用 `isinstance` 调度。

    字段语义同 `storage.list_media` kwargs;`limit=100_000` 一次性拉当前
    filter 全量(避免大频道半截)。`sort` / `sort_dir` 复用 PR #6 引入的
    `SortKey` / `SortDir`。
    """

    channel_id: int | None = None
    status: MediaDownloadStatus | None = None
    media_type: MediaType | None = None
    search: str = ""
    sort: SortKey = SortKey.DATE
    sort_dir: SortDir = SortDir.DESC
    limit: int = 100_000
    offset: int = 0
    out_path: str = ""
    format: ExportFormat = ExportFormat.MEDIA_CSV


@dataclass
class ExportResult:
    """导出结果 — 用于 `ExportDone` 事件 payload。"""

    out_path: str
    message_count: int
    bytes_written: int


@dataclass(frozen=True)
class OpenMediaResult:
    """打开 media 的结果 — 2026-08-25 v1.3.0 PR #5。

    `success=True` 表示系统调用成功(QDesktopServices.openUrl 返 True);
    `success=False` 时 `error` 必填,描述原因(媒体未下载完成 / 消息不存在 /
    S3 拉取失败 / OS 调用失败等),UI 据此弹 QMessageBox。
    """

    success: bool
    error: str | None = None


@dataclass(frozen=True)
class RevealResult:
    """2026-08-27 v1.4.0 PR #16:Reveal in Folder 操作结果。

    - `success=True`:成功发起 OS 文件管理器(在 Finder/Explorer 中高亮该文件)
    - `success=False`:S3 后端 / 文件不存在 / OS 调用失败

    与 OpenMediaResult 同结构(便于复用 UI 失败提示),单独定义以保留未来
    字段扩展空间(revealed_path 等)。
    """

    success: bool
    error: str | None = None


@dataclass(frozen=True)
class CopyResult:
    """2026-08-27 v1.4.0 PR #16:Copy 路径 / URI 操作结果。

    - `success=True`:`copied_value` 是已写入剪贴板的字符串(Local/Folder:
      绝对路径;S3:`s3://<bucket>/<object_key>`)
    - `success=False`:`error` 描述失败原因(媒体未下载 / 不支持后端)
    """

    success: bool
    copied_value: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class DeleteChannelPreview:
    """Clear Channel 操作预览 — 2026-08-25 v1.3.0 PR #8。

    **严格只读**(preview_* 系列约定不调任何 `delete_*` API),给 UI 在
    `ClearChannelPreviewDialog` 显示:
    - `message_count`:该频道全部消息数(忽略 date_from/to)
    - `media_count`:全部媒体数(含 PENDING/FAILED,不只是 DONE)
    - `potential_orphan_bytes`:执行删除后**只属于该频道**的 object_key 字节数
      (跨频道共享的不计 — refcount > 1 的 key 不会被 `delete_by_channel` 清理)

    字段语义跟 `AppService.delete_by_channel` 一致,确保预览值与真实执行
    后释放的 bytes 接近。
    """

    channel_id: int
    message_count: int
    media_count: int
    potential_orphan_bytes: int


# ---------- 全量同步(ChannelSyncService) ----------


@dataclass
class SyncOptions:
    """用户选的全量同步 options。"""

    include_metadata: bool = True
    include_history: bool = True
    history_limit: int | None = None  # None = 拉全部历史
    chat_delay_ms: int = 500  # 单条 API 间隔(防封号)
    page_delay_ms: int = 1000  # getChatHistory 分页间
    resume_from_saved: bool = True  # True: 从 storage max_msg_id 续拉


@dataclass
class ChannelSyncResult:
    """单个频道的同步结果。"""

    channel_id: int
    metadata_updated: bool = False
    messages_added: int = 0  # 本轮拉到的消息数(不去重)
    new_messages_added: int = 0  # 本轮新落库的消息数(existed is None 时 +1)
    messages_skipped: int = 0  # 本轮发现已存、不重写的消息数(skip-if-stored)
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


@dataclass(frozen=True)
class ChannelStats:
    """2026-08-27 v1.4.0 PR #15:单频道聚合统计 — 替代 Dashboard 的 N+1
    `count_messages` + `count_media_by_channel` 循环。

    由 `StorageRepository.aggregate_per_channel(channel_ids)` 一次性返
    `{channel_id: ChannelStats}`,消除 N+1 round-trip。

    字段:
    - `messages`:已落库消息数(忽略 date_from/to)
    - `media`:所有 media 数(含 PENDING / FAILED / DONE)
    - `done_media`:download_status == DONE 的 media 数
    - `last_date`:最近一条消息的 date(无消息则为 None)
    """

    messages: int = 0
    media: int = 0
    done_media: int = 0
    last_date: datetime | None = None
