"""配置 — pydantic-settings。

从环境变量 / .env 读取,集中定义所有后端选择与凭据。
UI 永远不直接读环境变量,所有配置都通过 `AppService.config()` 间接访问。
"""

from __future__ import annotations

import logging
from enum import Enum
from functools import lru_cache
from pathlib import Path

from platformdirs import user_data_dir
from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


class DBBackend(str, Enum):
    """消息仓储后端选择。

    v2.0.0:SQLITE 为默认(单文件,零部署);POSTGRES 可选(多设备/大规模)。
    Mongo / JSONL 后端已删 — 旧 .env 的 `mongo` / `jsonl` 等非法值由
    `Settings.load_tolerant()` 在启动时回退 SQLITE 并提示,不阻断启动。
    """

    SQLITE = "sqlite"
    POSTGRES = "postgres"


class ObjectStoreBackend(str, Enum):
    """对象存储后端选择。"""

    LOCAL = "local"  # 平铺本地(无分片)
    FOLDER = "folder"  # 本地两级分片(适合大量文件)
    S3 = "s3"


class MediaPolicy(str, Enum):
    """媒体下载策略。"""

    METADATA = "metadata"  # 仅元数据
    THUMBNAIL = "thumbnail"  # 元数据 + 缩略图(默认)
    FULL = "full"  # 元数据 + 缩略图 + 原文件


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
    """应用全局配置 — pydantic-settings 从 env / .env 读取。"""

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
    # 2026-11-21:登录只走二维码扫码,phone 字段已删除。旧 .env 里残留的
    # TG_PHONE 由 pydantic `extra="ignore"` 静默忽略,不会导致启动失败。
    session_dir: Path = Field(default_factory=lambda: _user_data_dir() / "session")

    # 可选代理(目前只支持 SOCKS5)。
    # 格式:`socks5://[user:pass@]host:port`
    # 中国国内网络直连 Telegram 不通,走代理常用。
    proxy: str | None = Field(default=None, description="socks5://[user:pass@]host:port")

    # ---- 数据库后端 ----
    # v2.0.0 默认 SQLITE:单文件零部署;POSTGRES 留给多设备/大规模场景。
    db_backend: DBBackend = Field(default=DBBackend.SQLITE)
    db_dsn: str = Field(default="postgresql://tgmonitor:tgmonitor@localhost:5432/tgmonitor")
    # SQLite 数据库文件路径(db_backend=sqlite 时用)。
    sqlite_path: Path = Field(default_factory=lambda: _user_data_dir() / "tgmonitor.db")
    # 历史字段,删 JSONL 后不再使用;保留只为兼容旧 .env。`EditableSettings`
    # 与 settings_page 都不再编辑/显示此字段。
    db_root: Path = Field(
        default_factory=lambda: _user_data_dir() / "messages"
    )  # legacy: 删 JSONL 后无 consumer

    # ---- 对象存储后端 ----
    # 默认 FOLDER:两级分片,文件多时不慢;亦可改 local(平铺)
    objectstore_backend: ObjectStoreBackend = Field(default=ObjectStoreBackend.FOLDER)
    objectstore_root: Path = Field(
        default_factory=lambda: _user_data_dir() / "media"
    )  # local/folder
    objectstore_endpoint: str | None = None  # s3 用
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

    # 2026-09-04 v1.6.6:暂停监听状态 — tray 点暂停后,quit + restart 应保持暂停。
    # .env 字段:TG_PAUSED=true|false(默认 false = 恢复监听)。
    # AppService 启动时从 settings 读此字段;若为 True,app.py:_setup_then_show
    # 跳过 monitor.start() + bootstrap(),tray / window / VM 读 app.is_paused
    # 自动显 ⏸。pause/resume_monitor() 切换时通过 settings_store.update_env_paused
    # 写回单 key(不走 update_env_with_settings 批量写,避免覆盖 SettingsPage
    # 正在编辑的其它字段)。
    paused: bool = Field(default=False)

    # 2026-11-24:关闭窗口按钮的行为。True(默认)= 真退出进程(走 aboutToQuit
    # 清理);False = 隐藏到托盘继续后台运行(点 X 不退出)。.env 字段
    # `TG_CLOSE_ON_EXIT`。缺省 / 老用户无此 key 时按 True 处理。
    close_on_exit: bool = Field(default=True)

    def ensure_dirs(self) -> None:
        """确保本地目录存在(仅在本地 backend / session 落盘时调用)。

        `db_root` 字段保留(默认 JSONL 时代的 `messages` 子目录),但
        不再 mkdir — JSONL 后端删除后,`db_root` 仅作 legacy 兼容旧 .env。
        """
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.data_root.mkdir(parents=True, exist_ok=True)
        if self.objectstore_backend in (ObjectStoreBackend.LOCAL, ObjectStoreBackend.FOLDER):
            self.objectstore_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_test(cls, **overrides) -> Settings:
        """PR 1a:测试用 Settings factory — 消 `# type: ignore[call-arg]` 噪音。

        默认值:
        - `_env_file=None`(不读 .env)
        - api_id=1, api_hash="x"*32
        - session_dir / data_root / db_root / objectstore_root 用 tempfile.mkdtemp
        - media_policy=METADATA / db_backend=SQLITE / objectstore_backend=LOCAL
          (v2.0.0 默认 SQLite,离线可跑;测试若需真连 PG 应
          `monkeypatch` build_storage 拦截,见
          `tests/test_reconfigure.py` 3 处 reconfigure test)

        `overrides` 优先级最高。`ensure_dirs()` 已被调用。

        复用 `tests/fixtures/_settings.py::make_test_settings` 的实现,但挂到
        Settings 类本身上,让测试 import `from tgmonitor.core.config import Settings`
        就能直接 `Settings.for_test()` — 不用再 import 测试 fixtures。
        """
        import tempfile

        base = Path(tempfile.mkdtemp(prefix="tgmon-test-"))
        defaults: dict = dict(
            _env_file=None,
            api_id=1,
            api_hash="x" * 32,
            session_dir=base / "session",
            data_root=base,
            db_root=base / "messages",  # legacy: 删 JSONL 后无 consumer,仅兼容旧 .env
            objectstore_root=base / "media",
        )
        defaults.update(overrides)
        s = cls(**defaults)
        s.ensure_dirs()
        return s

    @classmethod
    def from_env_file(cls, env_file: Path | str, **overrides) -> Settings:
        """PR 1a:从指定 .env 文件读配置 — 消 `# type: ignore[call-arg]`。

        等价于 `Settings(_env_file=str(env_file), **overrides)`,但 `_env_file`
        是 pydantic 私有 kwarg,type-checker 看不到 — 用本 helper 暴露 public 入口。
        `ensure_dirs()` 已被调用。
        """
        defaults: dict = dict(
            _env_file=str(env_file),
            api_id=1,
            api_hash="x" * 32,
        )
        defaults.update(overrides)
        s = cls(**defaults)
        s.ensure_dirs()
        return s

    @classmethod
    def load_tolerant(cls) -> tuple[Settings, list[str]]:
        """启动容错加载 — 非法字段回退默认值,不让配置错误阻断启动。

        合法配置等价于 `cls()`;遇 ValidationError(如旧版遗留
        `TG_DB_BACKEND=jsonl`)时,把非法字段以声明默认值覆盖后重试一次
        (init kwargs 优先级高于 env / .env),其余字段(凭据等)保留。
        返回 `(settings, warnings)`,warnings 为每个被重置字段的中文说明,
        供启动方提示用户去设置页修正。不改写 .env — 原值保留,用户在
        设置页保存后才落盘。
        """
        try:
            return cls(), []
        except ValidationError as exc:
            overrides: dict = {}
            warnings: list[str] = []
            for err in exc.errors():
                loc = err.get("loc") or ()
                name = loc[0] if loc else None
                if not isinstance(name, str) or name not in cls.model_fields:
                    continue
                if name in overrides:
                    continue
                default = cls.model_fields[name].get_default(call_default_factory=True)
                overrides[name] = default
                shown = default.value if isinstance(default, Enum) else default
                warnings.append(f"TG_{name.upper()}={err.get('input')!r} 非法,已回退默认值 {shown}")
            settings = cls(**overrides)
            for w in warnings:
                log.warning("[config] %s", w)
            return settings, warnings
