"""CLI `config` 子命令 — 配置展示 / 读取 / 设置 / 校验。

写链与 GUI 设置页完全同源(PRD §6.2):
    EditableSettings.from_settings → 改字段 → validate
    → validate_backends → reconfigure → update_env_with_settings
绝不直接改 .env 绕过 reconfigure。敏感字段(api_hash / 密钥)打码。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tgmonitor.core.app_service import AppService
from tgmonitor.core.config import Settings, _user_data_dir
from tgmonitor.core.monitor.service import MonitorService
from tgmonitor.core.settings_store import (
    EditableSettings,
    update_env_with_settings,
)
from tgmonitor.core.telegram.proxy import redact_url_credentials

# 敏感字段(展示打码)
_SENSITIVE = {"api_hash", "objectstore_access_key", "objectstore_secret_key"}
# URL 型字段:可能内嵌账号密码(`postgresql://u:p@h/db` / `socks5://u:p@h:port`)。
# 2026-11-26 2.1.7 P1-8:此前不在打码范围,带密码的 DSN / 代理 URL 明文进
# stdout。不整体打码 —— host / 库名 / 端口留着便于核对"生效的是哪个"。
_URL_FIELDS = frozenset({"db_dsn", "proxy"})
# 不接受设置(paused 用 pause/resume;db_root 遗留无 consumer)
_BLOCKED = {"paused", "db_root"}


def _mask(key: str, value: object) -> object:
    if not value:
        return value
    if key in _URL_FIELDS:
        return redact_url_credentials(str(value))
    if key in _SENSITIVE:
        return "****"
    return value


def _settings_to_dict(settings: Settings, *, mask: bool) -> dict:
    """Settings → dict(枚举取 value / Path 取 str / 敏感项打码)。"""
    out: dict[str, object] = {}
    for name in Settings.model_fields:
        val = getattr(settings, name)
        if hasattr(val, "value"):
            val = val.value
        elif isinstance(val, Path):
            val = str(val)
        out[name] = _mask(name, val) if mask else val
    return out


async def run_config(
    args: argparse.Namespace,
    *,
    app: AppService | None = None,
    monitor: MonitorService | None = None,
) -> int:
    """路由到 config show / get / set / validate。"""
    from tgmonitor.cli._common import CliError, cli_context, emit

    cmd = getattr(args, "config_cmd", None)

    if cmd == "show":
        settings, _warnings = Settings.load_tolerant()
        data = _settings_to_dict(settings, mask=True)
        emit(
            data,
            as_json=getattr(args, "json", False),
            text_rows=[f"{k} = {v}" for k, v in data.items()],
        )
        return 0

    if cmd == "get":
        settings, _warnings = Settings.load_tolerant()
        key = args.key
        if key not in Settings.model_fields:
            raise CliError(f"未知配置键: {key}")
        val = getattr(settings, key)
        if hasattr(val, "value"):
            val = val.value
        elif isinstance(val, Path):
            val = str(val)
        shown = _mask(key, val)
        emit(
            {"key": key, "value": shown},
            as_json=getattr(args, "json", False),
            text_rows=[str(shown)],
        )
        return 0

    if cmd == "validate":
        async with cli_context(args, app=app, monitor=monitor, skip_ready=True) as (
            ctx,
            _mon,
            settings,
        ):
            await ctx.validate_backends(settings)
            emit(
                {"ok": True},
                as_json=getattr(args, "json", False),
                text_rows=["后端可达性校验通过"],
            )
        return 0

    if cmd == "set":
        return await _config_set(args, app=app, monitor=monitor)

    raise CliError("用法:tgmon-cli config {show|get|set|validate} ...")


async def _config_set(
    args: argparse.Namespace,
    *,
    app: AppService | None = None,
    monitor: MonitorService | None = None,
) -> int:
    """设置 + 校验 + 热重载 + 写盘。"""
    from tgmonitor.cli._common import CliError, cli_context, emit

    if not getattr(args, "yes", False):
        raise CliError("config set 会写盘并热重载,请加 --yes 确认")

    key, value = args.key, args.value
    if key in _BLOCKED:
        raise CliError(f"{key} 不接受 config set(暂停请用 pause/resume)")

    async with cli_context(args, app=app, monitor=monitor, skip_ready=True) as (
        ctx,
        _mon,
        settings,
    ):
        editable = EditableSettings.from_settings(settings)
        _apply_field(editable, key, value)

        errs = editable.validate()
        if errs:
            raise CliError("配置校验失败: " + "; ".join(errs))

        new_settings = editable.to_settings()

        if not getattr(args, "no_validate", False):
            await ctx.validate_backends(new_settings)
        await ctx.reconfigure(new_settings)

        env_path = getattr(ctx, "env_path", None) or _user_data_dir() / ".env"
        update_env_with_settings(Path(env_path), new_settings)

        shown = _mask(key, getattr(new_settings, key, value))
        if hasattr(shown, "value"):
            shown = shown.value
        elif isinstance(shown, Path):
            shown = str(shown)
        emit(
            {"ok": True, "key": key, "value": shown},
            as_json=getattr(args, "json", False),
            text_rows=[f"已设置 {key} = {shown}"],
        )
    return 0


def _apply_field(editable: EditableSettings, key: str, value: str) -> None:
    """把命令行字符串值写入 EditableSettings 对应字段,带类型解析。

    特殊映射:
    - `media_max_bytes`(Settings 单位 bytes)→ `media_max_mb`(UI 单位 MB)
    - `media_download_on_sync` / `media_policy` 联动:改 policy 时同步 BOOL 镜像,
      避免 to_settings() 用旧 BOOL 覆盖新 policy(v2.0.9 FR-05 约定)。
    """
    if key not in EditableSettings.__dataclass_fields__:
        # 2.1.6 P2 #16:业务错误走 CliError(main() 捕获 → stderr `error: ...`
        # + 退出码 1),不能裸 ValueError 让 traceback 直出。
        from tgmonitor.cli._common import CliError

        raise CliError(f"未知配置键: {key}")

    if key == "media_max_bytes":
        try:
            editable.media_max_mb = int(int(value) // (1024 * 1024))
        except ValueError as exc:
            raise ValueError("media_max_bytes 需为整数(bytes)") from exc
        return

    field = EditableSettings.__dataclass_fields__[key]
    field_type = field.type
    # from __future__ import annotations → field.type 可能是字符串,这里按已知字段处理
    parsed: object = value
    if key in {"api_id", "sync_chat_delay_ms", "sync_page_delay_ms", "media_max_mb"}:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"{key} 需为整数") from exc
    elif key in {
        "sync_resume_from_saved",
        "close_on_exit",
        "media_download_on_sync",
    }:
        parsed = value.strip().lower() in {"1", "true", "yes", "on"}
    del field_type

    setattr(editable, key, parsed)

    # policy 与 BOOL 镜像联动
    if key == "media_policy":
        editable.media_download_on_sync = value.strip().lower() == "full"
