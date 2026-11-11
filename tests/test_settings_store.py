"""SettingsStore 单测 — .env 解析/序列化/保形更新。"""

from __future__ import annotations

from pathlib import Path

from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend, Settings
from tgmonitor.core.settings_store import (
    EditableSettings,
    diff_settings,
    parse_env_file,
    settings_to_pairs,
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
        "# header\nTG_API_ID=1\nTG_API_HASH=h\nTG_PHONE=+1\n",
        encoding="utf-8",
    )
    env = parse_env_file(p)
    env.pairs["TG_API_ID"] = "999"  # 改
    env.pairs["TG_DB_BACKEND"] = "postgres"  # 新增(非默认值,验证写入)
    write_env_file(env, p)
    text = p.read_text(encoding="utf-8")
    assert "# header" in text
    assert "TG_API_ID=999" in text
    assert "TG_DB_BACKEND=postgres" in text
    assert text.endswith("\n")  # 末尾换行


def test_update_env_with_settings(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text(
        "# user comment\n"
        "TG_API_ID=1\n"
        "TG_API_HASH=old\n"
        "TG_PHONE=+1\n"
        "TG_DB_BACKEND=postgres\n"  # seed 非默认值,验证被 for_test 默认覆盖
        "TG_OBJECTSTORE_BACKEND=local\n"
        "TG_OBJECTSTORE_ROOT=./data/media\n",
        encoding="utf-8",
    )
    s = Settings.for_test(
        api_id=42,
        api_hash="new-hash",
        phone="+1234",
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
    # for_test 继承类默认 SQLITE,覆盖 seed postgres
    assert "TG_DB_BACKEND=sqlite" in text
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
        api_id=1,
        api_hash="abcdef0123456789",
        phone="8613800000000",
        db_backend="nosuch",
        objectstore_backend="local",
        media_policy="thumbnail",
    )
    errs = e.validate()
    assert any("DB_BACKEND" in x for x in errs)


def test_editable_to_settings_roundtrip():
    e = EditableSettings(
        api_id=1,
        api_hash="h" * 32,
        phone="+1",
        session_dir="./s",
        db_backend="postgres",
        db_dsn="",
        # 2026-10-12 plan-batch3 目标 3 step 3.3:db_root 字段已删
        objectstore_backend="folder",
        objectstore_root="./o",
        objectstore_endpoint="",
        objectstore_region="us-east-1",
        objectstore_access_key="",
        objectstore_secret_key="",
        objectstore_bucket="b",
        media_policy="full",
        data_root="./d",
    )
    s = e.to_settings()
    assert s.api_id == 1
    assert s.db_backend == DBBackend.POSTGRES
    assert s.objectstore_backend == ObjectStoreBackend.FOLDER
    assert s.media_policy == MediaPolicy.FULL


# ---- diff_settings(给 AppService.reconfigure 用)----


def _settings(**kw) -> Settings:
    """最小可工作 Settings(default 全相同)。

    v2.0.0:db_backend 类默认 SQLITE;本 helper 显式 POSTGRES 作 diff 基准。
    """
    base = dict(
        api_id=1,
        api_hash="a" * 32,
        phone="+1",
        session_dir=Path("/tmp/s"),
        db_backend=DBBackend.POSTGRES,
        db_dsn="",
        objectstore_backend=ObjectStoreBackend.FOLDER,
        objectstore_root=Path("/tmp/o"),
        media_policy=MediaPolicy.METADATA,
        data_root=Path("/tmp"),
    )
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def test_diff_settings_noop():
    s = _settings()
    d = diff_settings(s, s)
    assert d.changed is False
    assert d.needs_relogin is False
    assert d.storage_changed is False
    assert d.objects_changed is False


def test_diff_settings_credentials_trigger_relogin():
    old = _settings()
    new = _settings(api_id=2)
    d = diff_settings(old, new)
    assert d.needs_relogin is True
    assert d.storage_changed is False
    assert d.changed is True


def test_diff_settings_storage_change():
    # 2026-10-12 plan-batch3 目标 3 step 3.3:db_root 字段已删,改测 db_dsn 变化触发 storage_changed
    old = _settings(db_dsn="postgresql://a")
    new = _settings(db_dsn="postgresql://b")
    d = diff_settings(old, new)
    assert d.storage_changed is True
    assert d.needs_relogin is False


def test_diff_settings_objectstore_change():
    old = _settings(objectstore_bucket="b1")
    new = _settings(objectstore_bucket="b2")
    d = diff_settings(old, new)
    assert d.objects_changed is True
    assert d.storage_changed is False


def test_diff_settings_proxy_change_needs_restart():
    old = _settings(proxy=None)
    new = _settings(proxy="socks5://127.0.0.1:1080")
    d = diff_settings(old, new)
    assert d.client_changed is True
    assert d.storage_changed is False
    assert d.objects_changed is False
    assert d.needs_relogin is False
    assert d.changed is True


def test_diff_settings_session_dir_change_needs_restart():
    old = _settings(session_dir=Path("/tmp/s1"))
    new = _settings(session_dir=Path("/tmp/s2"))
    d = diff_settings(old, new)
    assert d.client_changed is True
    assert d.changed is True


# ---- sqlite_path(v2.0.0 Phase 4-A:SQLite 默认后端,UI 可编辑路径)----


def test_editable_sqlite_path_roundtrip():
    """EditableSettings.sqlite_path 显式路径 → to_settings 保留。"""
    e = EditableSettings(
        api_id=1,
        api_hash="h" * 32,
        phone="+1",
        db_backend="sqlite",
        sqlite_path="/custom/dir/my.db",
    )
    s = e.to_settings()
    assert s.sqlite_path == Path("/custom/dir/my.db")
    # from_settings 回读一致
    assert EditableSettings.from_settings(s).sqlite_path == "/custom/dir/my.db"


def test_editable_sqlite_path_empty_falls_back_to_default():
    """空 sqlite_path → to_settings 兜底 platform-native 默认路径。"""
    from tgmonitor.core.config import _user_data_dir

    e = EditableSettings(api_id=1, api_hash="h" * 32, phone="+1", sqlite_path="")
    assert e.to_settings().sqlite_path == _user_data_dir() / "tgmonitor.db"


def test_settings_to_pairs_includes_sqlite_path():
    """settings_to_pairs 序列化 TG_SQLITE_PATH(写回 .env 用)。"""
    s = _settings(db_backend=DBBackend.SQLITE, sqlite_path=Path("/x/y.db"))
    pairs = settings_to_pairs(s)
    assert pairs["TG_SQLITE_PATH"] == "/x/y.db"


def test_diff_settings_sqlite_path_change():
    """sqlite_path 变化触发 storage_changed(热重载重建 storage)。"""
    old = _settings(db_backend=DBBackend.SQLITE, sqlite_path=Path("/x/a.db"))
    new = _settings(db_backend=DBBackend.SQLITE, sqlite_path=Path("/x/b.db"))
    d = diff_settings(old, new)
    assert d.storage_changed is True
    assert d.needs_relogin is False
