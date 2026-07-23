"""配置 — pydantic-settings。

从环境变量 / .env 读取,集中定义所有后端选择与凭据。
UI 永远不直接读环境变量,所有配置都通过 `AppService.config()` 间接访问。
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path

from platformdirs import user_data_dir
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DBBackend(str, Enum):
    POSTGRES = "postgres"
    MONGO = "mongo"
    JSONL = "jsonl"     # 文件:每频道一个 .jsonl(轻量,无需 DB 服务)


class ObjectStoreBackend(str, Enum):
    LOCAL = "local"     # 平铺本地(无分片)
    FOLDER = "folder"   # 本地两级分片(适合大量文件)
    S3 = "s3"


class MediaPolicy(str, Enum):
    """媒体下载策略。"""

    METADATA = "metadata"      # 仅元数据
    THUMBNAIL = "thumbnail"    # 元数据 + 缩略图(默认)
    FULL = "full"              # 元数据 + 缩略图 + 原文件


# ---- platform-native 路径 ----
# v1.0.1 起,所有数据 + .env 写到 OS 标准 user-data 目录:
#   - macOS:  ~/Library/Application Support/tgmonitor
#   - Linux:  $XDG_DATA_HOME/tgmonitor (fallback ~/.local/share/tgmonitor)
#   - Windows: %APPDATA%/tgmonitor
# 这样双击 /Applications/tgmonitor.app 启动也能写(cwd=/,无写权限)。
# `user_data_dir` 是纯 stdlib 派生 + 缓存安全(不依赖运行时状态)。
@lru_cache(maxsize=1)
def _user_data_dir() -> Path:
    """user-data 目录下的 tgmonitor/ 子目录,跨平台走 platformdirs。"""
    return Path(user_data_dir("tgmonitor", appauthor=False))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TG_",
        env_file=str(_user_data_dir() / ".env"),  # platform-native,不再 cwd-relative
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Telegram 凭据(my.telegram.org 申请) ----
    # 全部可选,启动时不要求 .env 已就绪(让用户能先打开设置对话框填好再启动监听)
    api_id: int = Field(default=0, description="Telegram API ID")
    api_hash: str = Field(default="", description="Telegram API hash")
    phone: str = Field(default="", description="登录手机号(含国家区号)")
    session_dir: Path = Field(default_factory=lambda: _user_data_dir() / "session")

    # 可选代理(目前只支持 SOCKS5)。
    # 格式:`socks5://[user:pass@]host:port`
    # 中国国内网络直连 Telegram 不通,走代理常用。
    proxy: str | None = Field(default=None, description="socks5://[user:pass@]host:port")

    # TDLib 内部日志级别:0=fatal,1=error,2=warning,3=info,4=debug,5=verbose。
    # 默认 0 — 故障排查时调到 3 能在 aiotdlib.tdjson logger 看到 TDLib 自己报的
    # 401/429 等内部错误。
    tdlib_verbosity: int = Field(default=0, ge=0, le=1023)

    # ---- 数据库后端 ----
    # 默认 JSONL:开箱即用,无需任何 DB 服务
    db_backend: DBBackend = Field(default=DBBackend.JSONL)
    db_dsn: str = Field(default="postgresql://tgmonitor:tgmonitor@localhost:5432/tgmonitor")
    db_root: Path = Field(default_factory=lambda: _user_data_dir() / "messages")  # jsonl 用

    # ---- 对象存储后端 ----
    # 默认 FOLDER:两级分片,文件多时不慢;亦可改 local(平铺)
    objectstore_backend: ObjectStoreBackend = Field(default=ObjectStoreBackend.FOLDER)
    objectstore_root: Path = Field(default_factory=lambda: _user_data_dir() / "media")  # local/folder
    objectstore_endpoint: str | None = None                      # s3 用
    objectstore_region: str = "us-east-1"
    objectstore_access_key: str | None = None
    objectstore_secret_key: str | None = None
    objectstore_bucket: str = "tgmonitor"

    # ---- 业务策略 ----
    media_policy: MediaPolicy = Field(default=MediaPolicy.THUMBNAIL)
    # 单文件下载上限(bytes);0 = 无限制。MediaDownloader 拒绝 > 此值的文件,
    # 防止 FULL 模式误订 GB 级视频把本地 / 对象存储爆掉。
    # .env 字段名:`TG_MEDIA_MAX_BYTES`(沿用 TG_ 前缀 + 大写蛇形)。
    # Default 用 binary 200 MB(209_715_200 = 200 * 1024 * 1024),跟
    # EditableSettings.media_max_mb 整除对齐 — 避免 round-trip 漂 10 MB。
    media_max_bytes: int = Field(
        default=209_715_200,  # 200 MB (binary)
        ge=0,
    )
    data_root: Path = Field(default_factory=_user_data_dir)

    # ---- 全量同步(防封号)— 用户在 UI 多选频道触发时的默认节奏 ----
    # `chat_delay_ms` 单条 API 间隔(每个 GetSupergroup / getChatHistory 之间)
    sync_chat_delay_ms: int = Field(default=500, ge=50, le=60000)
    # `page_delay_ms` getChatHistory 整页之间(每 100 条)
    sync_page_delay_ms: int = Field(default=1000, ge=100, le=60000)
    # `resume_from_saved` 续拉:True 时从 storage 已有 max_msg_id 之后拉
    sync_resume_from_saved: bool = Field(default=True)

    def ensure_dirs(self) -> None:
        """确保本地目录存在(仅在本地 backend / session 落盘时调用)。"""
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.data_root.mkdir(parents=True, exist_ok=True)
        if self.objectstore_backend in (ObjectStoreBackend.LOCAL, ObjectStoreBackend.FOLDER):
            self.objectstore_root.mkdir(parents=True, exist_ok=True)
        if self.db_backend == DBBackend.JSONL:
            self.db_root.mkdir(parents=True, exist_ok=True)
