"""Settings.load_tolerant 容错加载测试。

启动不应被非法配置阻断(如旧版遗留 `TG_DB_BACKEND=jsonl`):非法字段
回退声明默认值,其余字段保留,返回中文 warnings 供 UI 提示;.env 不被改写。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tgmonitor.core.config import DBBackend, MediaPolicy, Settings


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离的 .env 路径 — 替换 model_config.env_file 并清掉本机 TG_* 环境变量。"""
    path = tmp_path / ".env"
    monkeypatch.setitem(Settings.model_config, "env_file", str(path))
    for key in list(os.environ):
        if key.startswith("TG_"):
            monkeypatch.delenv(key)
    return path


def test_legacy_jsonl_backend_falls_back_to_sqlite(env_file: Path) -> None:
    """v1.x 遗留 TG_DB_BACKEND=jsonl → 回退 SQLITE,凭据等合法字段保留。"""
    original = (
        "TG_DB_BACKEND=jsonl\n"
        "TG_API_ID=12345\n"
        "TG_API_HASH=hhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhh\n"
        "TG_PHONE=+8613800000000\n"
    )
    env_file.write_text(original, encoding="utf-8")

    s, warnings = Settings.load_tolerant()

    assert s.db_backend is DBBackend.SQLITE
    assert s.api_id == 12345
    assert s.phone == "+8613800000000"
    assert len(warnings) == 1
    assert "TG_DB_BACKEND" in warnings[0]
    assert "jsonl" in warnings[0]
    # 容错加载不改写 .env — 原值保留,用户在设置页保存后才落盘
    assert env_file.read_text(encoding="utf-8") == original


def test_legacy_mongo_backend_via_env_var(env_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量来源的非法值同样容错(不仅限 .env)。"""
    monkeypatch.setenv("TG_DB_BACKEND", "mongo")

    s, warnings = Settings.load_tolerant()

    assert s.db_backend is DBBackend.SQLITE
    assert len(warnings) == 1
    assert "mongo" in warnings[0]


def test_multiple_invalid_fields_all_reset(env_file: Path) -> None:
    """枚举 / Literal / int 范围 / int 类型多种非法同时出现,各自回退默认。"""
    env_file.write_text(
        "TG_MEDIA_POLICY=bogus\n"
        "TG_SYNC_CHAT_DELAY_MS=1\n"  # ge=50
        "TG_LANG=fr\n"
        "TG_API_ID=not-a-number\n"
        "TG_PHONE=+8613800000000\n",
        encoding="utf-8",
    )

    s, warnings = Settings.load_tolerant()

    assert s.media_policy is MediaPolicy.THUMBNAIL
    assert s.sync_chat_delay_ms == 500
    assert s.lang == "zh_CN"
    assert s.api_id == 0
    assert s.phone == "+8613800000000"
    assert len(warnings) == 4
    joined = "\n".join(warnings)
    for key in ("TG_MEDIA_POLICY", "TG_SYNC_CHAT_DELAY_MS", "TG_LANG", "TG_API_ID"):
        assert key in joined


def test_invalid_bool_reset(env_file: Path) -> None:
    """bool 字段非法值(如 TG_PAUSED=maybe)回退默认 False。"""
    env_file.write_text("TG_PAUSED=maybe\n", encoding="utf-8")

    s, warnings = Settings.load_tolerant()

    assert s.paused is False
    assert len(warnings) == 1
    assert "TG_PAUSED" in warnings[0]


def test_valid_config_passthrough(env_file: Path) -> None:
    """全部合法时等价于 Settings(),无 warnings。"""
    env_file.write_text(
        "TG_DB_BACKEND=postgres\nTG_API_ID=42\nTG_LANG=en_US\n",
        encoding="utf-8",
    )

    s, warnings = Settings.load_tolerant()

    assert warnings == []
    assert s.db_backend is DBBackend.POSTGRES
    assert s.api_id == 42
    assert s.lang == "en_US"


def test_missing_env_file_ok(env_file: Path) -> None:
    """.env 不存在 → 全默认值,无 warnings(首次启动零配置)。"""
    s, warnings = Settings.load_tolerant()

    assert warnings == []
    assert s.db_backend is DBBackend.SQLITE
    assert s.api_id == 0
