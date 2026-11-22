"""SettingsPage 消息存储分组 — sqlite_path 字段 + DB 后端显隐(v2.0.0 Phase 4-A)。

SQLITE 默认后端:`sqlite_path` 可编辑;POSTGRES 时显 `db_dsn`、藏 `sqlite_path`,
反之亦然(镜像 os_backend 的本地/S3 互斥显隐)。
"""

from __future__ import annotations

from unittest.mock import Mock

from PySide6.QtWidgets import QFormLayout

from tgmonitor.core.config import DBBackend, Settings
from tgmonitor.ui.widgets.form_section import find_form_row
from tgmonitor.ui.widgets.settings_page import SettingsPage


def _make_page(tmp_path, backend: DBBackend) -> SettingsPage:
    mock_app = Mock()
    mock_app.settings = Settings.for_test(db_backend=backend)
    return SettingsPage(app=mock_app, loop=Mock(), env_path=tmp_path / ".env")


def _row_hidden(page: SettingsPage, key: str) -> bool:
    """走 find_form_row 拿 FieldRole wrapper 的 isHidden()(整 row 显隐单位)。"""
    hit = find_form_row(page, key)
    assert hit is not None, f"field {key!r} not found"
    gb, idx = hit
    form = gb.findChild(QFormLayout)
    assert form is not None
    item = form.itemAt(idx, QFormLayout.ItemRole.FieldRole)
    assert item is not None and item.widget() is not None
    return item.widget().isHidden()


def test_storage_sqlite_default_shows_sqlite_path(qapp, tmp_path) -> None:
    sp = _make_page(tmp_path, DBBackend.SQLITE)
    assert _row_hidden(sp, "sqlite_path") is False
    assert _row_hidden(sp, "db_dsn") is True


def test_storage_postgres_shows_dsn(qapp, tmp_path) -> None:
    sp = _make_page(tmp_path, DBBackend.POSTGRES)
    assert _row_hidden(sp, "db_dsn") is False
    assert _row_hidden(sp, "sqlite_path") is True


def test_storage_backend_switch_toggles_visibility(qapp, tmp_path) -> None:
    sp = _make_page(tmp_path, DBBackend.SQLITE)
    # SQLITE → POSTGRES
    sp.cmb_db.setCurrentIndex(sp.cmb_db.findData(DBBackend.POSTGRES))
    assert _row_hidden(sp, "db_dsn") is False
    assert _row_hidden(sp, "sqlite_path") is True
    # POSTGRES → SQLITE
    sp.cmb_db.setCurrentIndex(sp.cmb_db.findData(DBBackend.SQLITE))
    assert _row_hidden(sp, "sqlite_path") is False
    assert _row_hidden(sp, "db_dsn") is True


def test_storage_collect_sqlite_path(qapp, tmp_path) -> None:
    sp = _make_page(tmp_path, DBBackend.SQLITE)
    sp.in_sqlite_path.setText("/custom/my.db")
    assert sp._collect().sqlite_path == "/custom/my.db"


def test_storage_load_sqlite_path_from_settings(qapp, tmp_path) -> None:
    sp = _make_page(tmp_path, DBBackend.SQLITE)
    assert sp.in_sqlite_path.text() == str(sp._app.settings.sqlite_path)


# ---- 2026-11-22 v2.0.9 FR-05:「同步时下载媒体本身」BOOL 开关 -------------


def test_media_download_checkbox_defaults_unchecked(qapp, tmp_path) -> None:
    """默认(thumbnail)→ 开关未勾选。"""
    sp = _make_page(tmp_path, DBBackend.SQLITE)
    assert sp.chk_media_download.isChecked() is False


def test_media_download_checkbox_reflects_full_policy(qapp, tmp_path) -> None:
    """Settings.media_policy=FULL → 开关勾选;thumbnail/metadata → 未勾选。"""
    for policy, expected in (("full", True), ("thumbnail", False), ("metadata", False)):
        settings = Settings.for_test(media_policy=policy)
        mock_app = Mock()
        mock_app.settings = settings
        sp = SettingsPage(app=mock_app, loop=Mock(), env_path=tmp_path / ".env")
        assert sp.chk_media_download.isChecked() is expected, f"{policy} 回显错误"
        sp.deleteLater()


def test_media_download_checkbox_collect_maps_to_policy(qapp, tmp_path) -> None:
    """勾选 → media_policy=full + 镜像字段 True;取消 → thumbnail + False。"""
    sp = _make_page(tmp_path, DBBackend.SQLITE)
    sp.chk_media_download.setChecked(True)
    e = sp._collect()
    assert e.media_download_on_sync is True
    assert e.media_policy == "full"
    assert e.to_settings().media_policy.value == "full"

    sp.chk_media_download.setChecked(False)
    e = sp._collect()
    assert e.media_download_on_sync is False
    assert e.media_policy == "thumbnail"
    assert e.to_settings().media_policy.value == "thumbnail"
