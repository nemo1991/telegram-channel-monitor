"""SettingsStore 单测 — .env 解析/序列化/保形更新。"""
from __future__ import annotations

from pathlib import Path

import pytest

from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend, Settings
from tgmonitor.core.settings_store import (
    EditableSettings,
    parse_env_file,
    update_env_with_settings,
    write_env_file,
)


def test_parse_env_with_comments(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text(
        "# top comment\n"
        "\n"
        "TG_API_ID=123\n"
        "TG_API_HASH=abc\n"
        'TG_PHONE="+86 138"\n'
        "TG_DB_BACKEND=postgres\n"
        "\n"
        "# bottom comment\n",
        encoding="utf-8",
    )
    env = parse_env_file(p)
    assert env.pairs["TG_API_ID"] == "123"
    assert env.pairs["TG_API_HASH"] == "abc"
    assert env.pairs["TG_PHONE"] == "+86 138"
    # 注释与空行保留
    assert any("# top comment" in ln for ln in env.raw_lines)
    assert "" in env.raw_lines


def test_write_env_preserves_format(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text(
        "# header\n"
        "TG_API_ID=1\n"
        "TG_API_HASH=h\n"
        "TG_PHONE=+1\n",
        encoding="utf-8",
    )
    env = parse_env_file(p)
    env.pairs["TG_API_ID"] = "999"          # 改
    env.pairs["TG_DB_BACKEND"] = "jsonl"    # 新增
    write_env_file(env, p)
    text = p.read_text(encoding="utf-8")
    assert "# header" in text
    assert "TG_API_ID=999" in text
    assert "TG_DB_BACKEND=jsonl" in text
    assert text.endswith("\n")  # 末尾换行


def test_update_env_with_settings(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text(
        "# user comment\n"
        "TG_API_ID=1\n"
        "TG_API_HASH=old\n"
        "TG_PHONE=+1\n"
        "TG_DB_BACKEND=postgres\n"
        "TG_OBJECTSTORE_BACKEND=local\n"
        "TG_OBJECTSTORE_ROOT=./data/media\n",
        encoding="utf-8",
    )
    s = Settings(  # type: ignore[call-arg]
        api_id=42,
        api_hash="new-hash",
        phone="+1234",
        db_backend=DBBackend.JSONL,
        objectstore_backend=ObjectStoreBackend.FOLDER,
        objectstore_root=Path("./data/sharded"),
        media_policy=MediaPolicy.FULL,
    )
    update_env_with_settings(p, s)
    text = p.read_text(encoding="utf-8")
    # 注释保留
    assert "# user comment" in text
    # 关键字段被覆盖
    assert "TG_API_ID=42" in text
    assert "TG_API_HASH=new-hash" in text
    assert "TG_PHONE=+1234" in text
    assert "TG_DB_BACKEND=jsonl" in text
    assert "TG_OBJECTSTORE_BACKEND=folder" in text
    assert "TG_MEDIA_POLICY=full" in text


def test_editable_settings_validate():
    e = EditableSettings(api_id=0, api_hash="", phone="+1")
    errs = e.validate()
    assert any("API_ID" in x for x in errs)
    assert any("API_HASH" in x for x in errs)

    e = EditableSettings(api_id=1, api_hash="abcdef0123456789", phone="+8613800000000")
    assert e.validate() == []

    e = EditableSettings(
        api_id=1, api_hash="abcdef0123456789", phone="8613800000000",
        db_backend="nosuch", objectstore_backend="local", media_policy="thumbnail",
    )
    errs = e.validate()
    assert any("DB_BACKEND" in x for x in errs)


def test_editable_to_settings_roundtrip():
    e = EditableSettings(
        api_id=1, api_hash="h" * 32, phone="+1",
        session_dir="./s",
        db_backend="jsonl", db_dsn="", db_root="./m",
        objectstore_backend="folder", objectstore_root="./o",
        objectstore_endpoint="", objectstore_region="us-east-1",
        objectstore_access_key="", objectstore_secret_key="",
        objectstore_bucket="b",
        media_policy="full", data_root="./d",
    )
    s = e.to_settings()
    assert s.api_id == 1
    assert s.db_backend == DBBackend.JSONL
    assert s.objectstore_backend == ObjectStoreBackend.FOLDER
    assert s.media_policy == MediaPolicy.FULL
