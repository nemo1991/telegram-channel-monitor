"""SettingsStore — 读 / 改 / 写 .env 文件。

- 解析:支持 `KEY=value` / `KEY="value with spaces"` / `# 注释` / 空行
- 序列化:在原文件基础上**保形更新** — 注释、空行、key 顺序尽量保留
- 写:缺省的 TG_* key 追加到末尾(若不存在);已存在的覆盖

> 为什么不用 pydantic-settings 反向序列化:它无"原地更新 .env"的语义,
> 自己写一个轻量解析器更可控。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tgmonitor.core.config import (
    DBBackend,
    MediaPolicy,
    ObjectStoreBackend,
    Settings,
    _user_data_dir,
)

_LINE = re.compile(r"^([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$")


def _strip_quotes(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1]
    return v


def _needs_quote(v: str) -> bool:
    return any(c.isspace() for c in v) or "#" in v or "=" in v


def _quote(v: str) -> str:
    if not _needs_quote(v):
        return v
    esc = v.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{esc}"'


@dataclass
class EnvFile:
    """解析后的 .env 文件(可序列化回原格式)。"""

    raw_lines: list[str]  # 原始行(含注释/空行),用于保形输出
    pairs: dict[str, str]  # 解析出的 key -> value
    # key 在 raw_lines 中的 index(便于覆盖时直接改行)
    indices: dict[str, int]


def parse_env_file(path: Path) -> EnvFile:
    """解析 .env 文件 — 返回 EnvFile(可序列化回原格式,保形)。"""
    raw: list[str] = []
    pairs: dict[str, str] = {}
    indices: dict[str, int] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            raw.append(line)
            m = _LINE.match(line)
            if m:
                k, v = m.group(1), _strip_quotes(m.group(2))
                pairs[k] = v
                indices[k] = len(raw) - 1
    return EnvFile(raw_lines=raw, pairs=pairs, indices=indices)


def write_env_file(env: EnvFile, path: Path) -> None:
    """把 EnvFile 落盘(保留注释/空行,只更新已存在的 key,新增 key 追加到末尾)。"""
    lines = list(env.raw_lines)
    # 已存在 key 直接覆盖
    for k, v in env.pairs.items():
        if k in env.indices:
            lines[env.indices[k]] = f"{k}={_quote(v)}"
    # 新 key 追加
    new_keys = [k for k in env.pairs if k not in env.indices]
    if new_keys:
        if lines and lines[-1].strip() != "":
            lines.append("")
        for k in new_keys:
            lines.append(f"{k}={_quote(env.pairs[k])}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---- 高层 API:Settings <-> EnvFile ----


def settings_to_pairs(s: Settings) -> dict[str, str]:
    """Settings → .env 字段 dict(供 UI 编辑后写回)。

    `TG_DB_ROOT` 字段不再序列化(原 JSONL 后端专属,删 JSONL 后无
    consumer)。`Settings.db_root` 字段仍保留(默认 `_user_data_dir() / messages`),
    只为兼容老用户 .env;
    step 3.4 联动 settings_page 删 FormSection `db_root` 字段。
    """
    return {
        "TG_API_ID": str(s.api_id),
        "TG_API_HASH": s.api_hash,
        "TG_SESSION_DIR": str(s.session_dir),
        "TG_PROXY": s.proxy or "",
        "TG_DB_BACKEND": s.db_backend.value,
        "TG_DB_DSN": s.db_dsn,
        "TG_SQLITE_PATH": str(s.sqlite_path),
        "TG_OBJECTSTORE_BACKEND": s.objectstore_backend.value,
        "TG_OBJECTSTORE_ROOT": str(s.objectstore_root),
        "TG_OBJECTSTORE_ENDPOINT": s.objectstore_endpoint or "",
        "TG_OBJECTSTORE_REGION": s.objectstore_region,
        "TG_OBJECTSTORE_ACCESS_KEY": s.objectstore_access_key or "",
        "TG_OBJECTSTORE_SECRET_KEY": s.objectstore_secret_key or "",
        "TG_OBJECTSTORE_BUCKET": s.objectstore_bucket,
        "TG_MEDIA_POLICY": s.media_policy.value,
        "TG_MEDIA_MAX_BYTES": str(s.media_max_bytes),
        "TG_DATA_ROOT": str(s.data_root),
        "TG_SYNC_CHAT_DELAY_MS": str(s.sync_chat_delay_ms),
        "TG_SYNC_PAGE_DELAY_MS": str(s.sync_page_delay_ms),
        "TG_SYNC_RESUME_FROM_SAVED": "true" if s.sync_resume_from_saved else "false",
        # 2026-11-24:关窗行为走设置页「关闭窗口时退出」保存。缺省 true。
        "TG_CLOSE_ON_EXIT": "true" if s.close_on_exit else "false",
    }


def update_env_with_settings(env_path: Path, settings: Settings) -> None:
    """把当前 settings 写回 .env(保留注释/空行/已有 key 顺序)。"""
    env = parse_env_file(env_path)
    new_pairs = settings_to_pairs(settings)
    # 覆盖 + 新增
    for k, v in new_pairs.items():
        env.pairs[k] = v
    write_env_file(env, env_path)


def update_env_credentials(env_path: Path, *, api_id: int, api_hash: str) -> None:
    """只把 2 个 Telegram 凭据写入 .env(保形更新其余 key)。

    供设置页「二维码登录」使用:用户点登录时把当场填写的 API ID / Hash
    落盘,但**不该**连带提交其它分组尚未点「保存」的改动(数据库 /
    对象存储 / 策略等)。`update_env_with_settings` 会重写全部 TG_* 字段,
    这里只动这 2 个 key,语义与按钮职责对齐。登录已不依赖手机号,故不写
    TG_PHONE(旧 .env 里若残留该行,保形更新会原样保留,但不再被读取)。
    """
    env = parse_env_file(env_path)
    env.pairs["TG_API_ID"] = str(api_id)
    env.pairs["TG_API_HASH"] = api_hash
    write_env_file(env, env_path)


def update_env_paused(env_path: Path, paused: bool) -> None:
    """2026-09-04 v1.6.6:写 TG_PAUSED=true|false 到 .env(只动这一行)。

    与 `update_env_with_settings` 区别:不重写全部 TG_* 字段,只更新单 key。
    SettingsPage 编辑其它字段(`update_env_with_settings` 批量写)与本
    helper 并发时,last-write-wins 退化为:谁后写谁赢;但本 helper 只读 +
    改 + 写 `TG_PAUSED` 一行,不重写其它 key,不存在 SettingsPage 的
    「主题」被本 helper 覆盖丢失的风险。

    `parse_env_file` 保形 — 注释 / 空行 / 其它 key 顺序全部保留。
    """
    env = parse_env_file(env_path)
    env.pairs["TG_PAUSED"] = "true" if paused else "false"
    write_env_file(env, env_path)


# 2026-11-24:closeEvent 在退出前直接读 .env(而不是 app.settings)—— 设置页
# 保存时若只改了本字段,core 的 reconfigure diff 判无变化直接 return,内存
# Settings 仍是旧值;从 .env 读能保证「保存后立即生效」。
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def read_close_on_exit(env_path: Path | None) -> bool:
    """读 `TG_CLOSE_ON_EXIT`;env_path 为空 / key 缺失 / 读失败 → True(真退出)。"""
    if env_path is None:
        return True
    try:
        raw = parse_env_file(env_path).pairs.get("TG_CLOSE_ON_EXIT")
    except OSError:
        return True
    if raw is None:
        return True
    return raw.strip().lower() in _TRUE_ENV_VALUES


# ---- 可编辑模型(给 UI 用) ----


@dataclass
class EditableSettings:
    """UI 用的可编辑设置(类型友好,无 pydantic 依赖)。"""

    api_id: int = 0
    api_hash: str = ""
    session_dir: str = "./data/session"

    db_backend: str = "sqlite"  # DBBackend.value
    db_dsn: str = ""
    # SQLite 数据库文件路径(db_backend=sqlite 时用)。空串 = 走 platform-native
    # 默认(`_user_data_dir() / tgmonitor.db`),to_settings() 兜底。
    sqlite_path: str = ""
    # db_root 字段已删除 — 原 JSONL 后端专属,删 JSONL 后无 consumer。
    # `Settings.db_root` 仍保留默认(`_user_data_dir() / messages`),
    # 只为兼容老用户 .env,UI 不再编辑/显示。

    objectstore_backend: str = "local"
    objectstore_root: str = "./data/media"
    objectstore_endpoint: str = ""
    objectstore_region: str = "us-east-1"
    objectstore_access_key: str = ""
    objectstore_secret_key: str = ""
    objectstore_bucket: str = "tgmonitor"

    media_policy: str = "thumbnail"
    # 2026-11-22 v2.0.9 FR-05(D4=β):UI「同步时下载媒体本身」BOOL 镜像字段。
    # 只控制是否下载原文件;缩略图属元数据侧、始终保留。
    # - None(默认)= 未由 UI 设置 → to_settings() 按既有 media_policy 走(向后
    #   兼容既有直接构造 EditableSettings(media_policy=...) 的调用方/测试);
    # - True → MediaPolicy.FULL;False → THUMBNAIL(元数据侧)。
    # from_settings() 总是显式赋值(由 policy == FULL 推导),UI 往返无漂移。
    media_download_on_sync: bool | None = None
    # 单文件下载上限(MB);0 = 无限制。Settings 里是 bytes,
    # 这里是 MB(UI 显示友好)。
    media_max_mb: int = 200
    data_root: str = "./data"

    # 可选代理 URL(目前只支持 socks5://)
    proxy: str = ""

    # 全量同步默认值(UI 跑 sync 时可覆盖)
    sync_chat_delay_ms: int = 500
    sync_page_delay_ms: int = 1000
    sync_resume_from_saved: bool = True

    # 2026-11-24:UI「关闭窗口时退出」开关。默认 True — 点 X 真退出;
    # 关掉则恢复 v1.5.0 起的「隐藏到托盘后台运行」。
    close_on_exit: bool = True

    @classmethod
    def from_settings(cls, s: Settings) -> EditableSettings:
        """Settings → EditableSettings(给 UI 编辑用)。bytes/MB 单位转换在这里做。"""
        return cls(
            api_id=s.api_id,
            api_hash=s.api_hash,
            session_dir=str(s.session_dir),
            db_backend=s.db_backend.value,
            db_dsn=s.db_dsn,
            sqlite_path=str(s.sqlite_path),
            objectstore_backend=s.objectstore_backend.value,
            objectstore_root=str(s.objectstore_root),
            objectstore_endpoint=s.objectstore_endpoint or "",
            objectstore_region=s.objectstore_region,
            objectstore_access_key=s.objectstore_access_key or "",
            objectstore_secret_key=s.objectstore_secret_key or "",
            objectstore_bucket=s.objectstore_bucket,
            media_policy=s.media_policy.value,
            # FR-05:只有 FULL 是「媒体本身侧」→ 开关勾选;metadata/thumbnail
            # 都属元数据侧 → 未勾选(纠正 v1.0 把 thumbnail 误归开的映射)。
            media_download_on_sync=s.media_policy == MediaPolicy.FULL,
            # Settings 里是 bytes,UI 显示 MB。整除:999_999 bytes 显示 0 MB
            # 不够精细,但跟"无配置 = 200 MB"对齐;用户改 MB 后 × 1024*1024。
            media_max_mb=s.media_max_bytes // (1024 * 1024),
            data_root=str(s.data_root),
            proxy=s.proxy or "",
            sync_chat_delay_ms=s.sync_chat_delay_ms,
            sync_page_delay_ms=s.sync_page_delay_ms,
            sync_resume_from_saved=s.sync_resume_from_saved,
            close_on_exit=s.close_on_exit,
        )

    def validate(self) -> list[str]:
        """校验 UI 输入,返错误消息列表(空 = 全部合法)。

        登录只走二维码扫码,只需 api_id / api_hash;无手机号字段。
        """
        errs: list[str] = []
        if self.api_id <= 0:
            errs.append("TG_API_ID 必须为正整数")
        if not self.api_hash or len(self.api_hash) < 16:
            errs.append("TG_API_HASH 长度应 ≥ 16")
        if self.db_backend not in {b.value for b in DBBackend}:
            errs.append(f"TG_DB_BACKEND 非法: {self.db_backend}")
        if self.objectstore_backend not in {b.value for b in ObjectStoreBackend}:
            errs.append(f"TG_OBJECTSTORE_BACKEND 非法: {self.objectstore_backend}")
        if self.media_policy not in {p.value for p in MediaPolicy}:
            errs.append(f"TG_MEDIA_POLICY 非法: {self.media_policy}")
        if self.media_max_mb < 0:
            errs.append("TG_MEDIA_MAX_BYTES(MB) 不能为负;0 表示无限制")
        if self.sync_chat_delay_ms < 50 or self.sync_chat_delay_ms > 60000:
            errs.append("TG_SYNC_CHAT_DELAY_MS 应在 50-60000")
        if self.sync_page_delay_ms < 100 or self.sync_page_delay_ms > 60000:
            errs.append("TG_SYNC_PAGE_DELAY_MS 应在 100-60000")
        proxy_err = _validate_proxy_url(self.proxy)
        if proxy_err:
            errs.append(proxy_err)
        return errs

    def to_settings(self) -> Settings:
        """UI 表单 → 完整 Settings(已 validate 过的前提)。"""
        # FR-05:UI BOOL 开关是 media_policy 的唯一真值源(非 None 时)。
        # True → FULL(下原文件);False → THUMBNAIL(元数据侧;旧 metadata 值
        # 借此规范化为默认档)。None = 无 UI 参与,保留既有 media_policy 字段。
        policy_value = self.media_policy
        if self.media_download_on_sync is True:
            policy_value = MediaPolicy.FULL.value
        elif self.media_download_on_sync is False:
            # 关侧统一规范化为默认档 thumbnail(含旧 metadata 值)。
            policy_value = MediaPolicy.THUMBNAIL.value
        return Settings(
            api_id=self.api_id,
            api_hash=self.api_hash,
            session_dir=Path(self.session_dir),
            db_backend=DBBackend(self.db_backend),
            db_dsn=self.db_dsn,
            sqlite_path=Path(self.sqlite_path)
            if self.sqlite_path.strip()
            else _user_data_dir() / "tgmonitor.db",
            objectstore_backend=ObjectStoreBackend(self.objectstore_backend),
            objectstore_root=Path(self.objectstore_root),
            objectstore_endpoint=self.objectstore_endpoint or None,
            objectstore_region=self.objectstore_region,
            objectstore_access_key=self.objectstore_access_key or None,
            objectstore_secret_key=self.objectstore_secret_key or None,
            objectstore_bucket=self.objectstore_backend == "s3"
            and self.objectstore_bucket
            or self.objectstore_bucket,
            media_policy=MediaPolicy(policy_value),
            media_max_bytes=self.media_max_mb * 1024 * 1024,
            data_root=Path(self.data_root),
            proxy=(self.proxy.strip() or None),
            sync_chat_delay_ms=self.sync_chat_delay_ms,
            sync_page_delay_ms=self.sync_page_delay_ms,
            sync_resume_from_saved=self.sync_resume_from_saved,
            close_on_exit=self.close_on_exit,
        )


def _validate_proxy_url(s: str) -> str | None:
    """校验代理 URL(目前只支持 socks5://[user:pass@]host:port)。空 = 不设置。"""
    if not s.strip():
        return None
    if not s.startswith(("socks5://", "SOCKS5://")):
        return "TG_PROXY 目前只支持 socks5:// 协议"
    rest = s.split("://", 1)[1]
    if "@" in rest:
        _, hostport = rest.rsplit("@", 1)
    else:
        hostport = rest
    if ":" not in hostport:
        return "TG_PROXY 必须含 host:port"
    host, _, port = hostport.rpartition(":")
    if not host or not port.isdigit():
        return "TG_PROXY 格式错误(期望 socks5://[user:pass@]host:port)"
    return None


# ---- Settings 重建(用于热重载) ----


# settings 不变(同进程),可绕过 pydantic 重新构造以让字段生效
def reload_settings(env_path: Path | None = None, *, env: dict[str, str] | None = None) -> Settings:
    """从 .env 重新构造 Settings(env 可显式覆盖以测热重载)。

    pydantic-settings `BaseSettings.env_file` 接受 `str | os.PathLike | None`,
    传 None 表示不读 .env(让 `_env_file=...` 这种历史 typo 失效)。
    """
    if env is not None:
        return Settings(env_file=None, **env)  # type: ignore[call-arg,arg-type]
    return Settings(env_file=str(env_path) if env_path else None)  # type: ignore[call-arg]


# ---- Settings diff(给 AppService.reconfigure 用) ----


@dataclass(frozen=True)
class SettingsDiff:
    """两个 Settings 之间的差异 — `AppService.reconfigure` 用。

    字段:
      needs_relogin: api_id / api_hash 之一变化(Telegram 凭据)
      storage_changed: db_backend / db_dsn / sqlite_path 之一变化
        (db_root 字段已删,不再列入 diff)
      objects_changed: objectstore_* 字段之一变化
      client_changed: proxy / session_dir 之一变化(TelegramClient 构造参数,运行时
        不重建 client,需重启生效 — 2026-08-18 新增,此前纯代理/会话目录变更
        diff.changed=False,reconfigure 直接 return,UI 却弹「已保存并热重载」,
        属假成功)
    """

    needs_relogin: bool
    storage_changed: bool
    objects_changed: bool
    client_changed: bool

    @property
    def changed(self) -> bool:
        """任一字段变化即 True — `reconfigure` 据此 short-circuit。"""
        return (
            self.needs_relogin
            or self.storage_changed
            or self.objects_changed
            or self.client_changed
        )


def diff_settings(old: Settings, new: Settings) -> SettingsDiff:
    """比对两个 Settings,返回 SettingsDiff。

    单纯比较字段值,不构造任何资源;`reconfigure` 据此决定是否重建
    storage / objects / 是否需要重登。
    """
    needs_relogin = old.api_id != new.api_id or old.api_hash != new.api_hash
    storage_changed = (
        old.db_backend != new.db_backend
        or old.db_dsn != new.db_dsn
        or old.sqlite_path != new.sqlite_path
    )
    objects_changed = (
        old.objectstore_backend != new.objectstore_backend
        or old.objectstore_root != new.objectstore_root
        or old.objectstore_endpoint != new.objectstore_endpoint
        or old.objectstore_bucket != new.objectstore_bucket
        or old.objectstore_access_key != new.objectstore_access_key
        or old.objectstore_secret_key != new.objectstore_secret_key
        or old.objectstore_region != new.objectstore_region
    )
    client_changed = old.proxy != new.proxy or old.session_dir != new.session_dir
    return SettingsDiff(
        needs_relogin=needs_relogin,
        storage_changed=storage_changed,
        objects_changed=objects_changed,
        client_changed=client_changed,
    )
