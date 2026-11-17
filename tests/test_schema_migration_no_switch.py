"""契约:schema schema 迁移对用户无感 —— 无 TG_SCHEMA_AUTO_REPAIR 开关。

2026-11-17 v2.0.3:曾短暂存在 `schema_auto_repair` 开关,默认 False 导致存量
SQLite 库补不上 tg_subscribed 列(线上报错)。PR #63 让 init_schema 无条件补列,
随后彻底删除该开关。本测试守住"不再回退成用户可选开关":

1. Settings 不含 schema_auto_repair 字段(连 .env 残留键都被 extra=ignore 静默忽略);
2. bootstrap 源码不含该开关的判断,repair 走无条件兜底。
"""

from __future__ import annotations

import inspect
from pathlib import Path

from tgmonitor.core.config import Settings

ROOT = Path(__file__).resolve().parent.parent


def test_settings_has_no_schema_auto_repair_field() -> None:
    """Settings 不再暴露 schema_auto_repair —— schema 迁移无需用户配置。"""
    assert "schema_auto_repair" not in Settings.model_fields


def test_settings_ignores_legacy_env_key(tmp_path) -> None:
    """.env 残留 TG_SCHEMA_AUTO_REPAIR=true 被 extra=ignore 静默忽略,不报错。"""
    env = tmp_path / ".env"
    env.write_text("TG_SCHEMA_AUTO_REPAIR=true\n", encoding="utf-8")
    s = Settings(_env_file=env)
    assert not hasattr(s, "schema_auto_repair")


def test_bootstrap_repairs_without_user_switch() -> None:
    """bootstrap 无开关判断,repair_schema 为无条件幂等兜底。"""
    from tgmonitor.core import runtime

    src = inspect.getsource(runtime.bootstrap)
    assert "schema_auto_repair" not in src
    assert "repair_schema" in src  # 仍保留幂等兜底
    assert "AUTO_REPAIR" not in src  # 无用户可见提示
