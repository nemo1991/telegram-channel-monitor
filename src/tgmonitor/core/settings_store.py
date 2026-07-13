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
from typing import Iterable

from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend, Settings


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

    raw_lines: list[str]      # 原始行(含注释/空行),用于保形输出
    pairs: dict[str, str]     # 解析出的 key -> value
    # key 在 raw_lines 中的 index(便于覆盖时直接改行)
    indices: dict[str, int]


def parse_env_file(path: Path) -> EnvFile:
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
    return {
        "TG_API_ID": str(s.api_id),
        "TG_API_HASH": s.api_hash,
        "TG_PHONE": s.phone,
        "TG_SESSION_DIR": str(s.session_dir),
        "TG_DB_BACKEND": s.db_backend.value,
        "TG_DB_DSN": s.db_dsn,
        "TG_DB_ROOT": str(s.db_root),
        "TG_OBJECTSTORE_BACKEND": s.objectstore_backend.value,
        "TG_OBJECTSTORE_ROOT": str(s.objectstore_root),
        "TG_OBJECTSTORE_ENDPOINT": s.objectstore_endpoint or "",
        "TG_OBJECTSTORE_REGION": s.objectstore_region,
        "TG_OBJECTSTORE_ACCESS_KEY": s.objectstore_access_key or "",
        "TG_OBJECTSTORE_SECRET_KEY": s.objectstore_secret_key or "",
        "TG_OBJECTSTORE_BUCKET": s.objectstore_bucket,
        "TG_MEDIA_POLICY": s.media_policy.value,
        "TG_DATA_ROOT": str(s.data_root),
    }


def update_env_with_settings(env_path: Path, settings: Settings) -> None:
    """把当前 settings 写回 .env(保留注释/空行/已有 key 顺序)。"""
    env = parse_env_file(env_path)
    new_pairs = settings_to_pairs(settings)
    # 覆盖 + 新增
    for k, v in new_pairs.items():
        env.pairs[k] = v
    write_env_file(env, env_path)


# ---- 可编辑模型(给 UI 用) ----

@dataclass
class EditableSettings:
    """UI 用的可编辑设置(类型友好,无 pydantic 依赖)。"""

    api_id: int = 0
    api_hash: str = ""
    phone: str = ""
    session_dir: str = "./data/session"

    db_backend: str = "postgres"     # DBBackend.value
    db_dsn: str = ""
    db_root: str = "./data/messages"  # jsonl 用

    objectstore_backend: str = "local"
    objectstore_root: str = "./data/media"
    objectstore_endpoint: str = ""
    objectstore_region: str = "us-east-1"
    objectstore_access_key: str = ""
    objectstore_secret_key: str = ""
    objectstore_bucket: str = "tgmonitor"

    media_policy: str = "thumbnail"
    data_root: str = "./data"

    @classmethod
    def from_settings(cls, s: Settings) -> "EditableSettings":
        return cls(
            api_id=s.api_id,
            api_hash=s.api_hash,
            phone=s.phone,
            session_dir=str(s.session_dir),
            db_backend=s.db_backend.value,
            db_dsn=s.db_dsn,
            db_root=str(s.db_root),
            objectstore_backend=s.objectstore_backend.value,
            objectstore_root=str(s.objectstore_root),
            objectstore_endpoint=s.objectstore_endpoint or "",
            objectstore_region=s.objectstore_region,
            objectstore_access_key=s.objectstore_access_key or "",
            objectstore_secret_key=s.objectstore_secret_key or "",
            objectstore_bucket=s.objectstore_bucket,
            media_policy=s.media_policy.value,
            data_root=str(s.data_root),
        )

    def validate(self) -> list[str]:
        errs: list[str] = []
        if self.api_id <= 0:
            errs.append("TG_API_ID 必须为正整数")
        if not self.api_hash or len(self.api_hash) < 16:
            errs.append("TG_API_HASH 长度应 ≥ 16")
        if not self.phone.startswith("+"):
            errs.append("TG_PHONE 必须以 + 开头(含国家区号)")
        if self.db_backend not in {b.value for b in DBBackend}:
            errs.append(f"TG_DB_BACKEND 非法: {self.db_backend}")
        if self.objectstore_backend not in {b.value for b in ObjectStoreBackend}:
            errs.append(f"TG_OBJECTSTORE_BACKEND 非法: {self.objectstore_backend}")
        if self.media_policy not in {p.value for p in MediaPolicy}:
            errs.append(f"TG_MEDIA_POLICY 非法: {self.media_policy}")
        return errs

    def to_settings(self) -> Settings:
        return Settings(  # type: ignore[call-arg]
            api_id=self.api_id,
            api_hash=self.api_hash,
            phone=self.phone,
            session_dir=Path(self.session_dir),
            db_backend=DBBackend(self.db_backend),
            db_dsn=self.db_dsn,
            db_root=Path(self.db_root),
            objectstore_backend=ObjectStoreBackend(self.objectstore_backend),
            objectstore_root=Path(self.objectstore_root),
            objectstore_endpoint=self.objectstore_endpoint or None,
            objectstore_region=self.objectstore_region,
            objectstore_access_key=self.objectstore_access_key or None,
            objectstore_secret_key=self.objectstore_secret_key or None,
            objectstore_bucket=self.objectstore_backend == "s3" and self.objectstore_bucket or self.objectstore_bucket,
            media_policy=MediaPolicy(self.media_policy),
            data_root=Path(self.data_root),
        )


# ---- Settings 重建(用于热重载) ----

# settings 不变(同进程),可绕过 pydantic 重新构造以让字段生效
def reload_settings(env_path: Path | None = None, *, env: dict[str, str] | None = None) -> Settings:
    """从 .env 重新构造 Settings(env 可显式覆盖以测热重载)。"""
    if env is not None:
        return Settings(_env_file=None, **env)  # type: ignore[arg-type]
    return Settings(_env_file=str(env_path) if env_path else None)  # type: ignore[arg-type]
