"""v2.0.7 FR-08/FR-09 契约 — API ID 无箭头 + 删除「仅保存到 .env」入口。

- APPID 的 QSpinBox 不显示上下箭头,范围与校验不变;
- 设置页底栏只剩「保存并应用」,core validate_backends 公共 API 保留。
"""

from __future__ import annotations

from PySide6.QtWidgets import QPushButton, QSpinBox

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.ui.widgets.settings_page import SettingsPage


def test_api_id_spin_has_no_arrows(qapp, qloop, tmp_path) -> None:
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None

    spin = settings.in_api_id
    assert isinstance(spin, QSpinBox)
    assert spin.buttonSymbols() == QSpinBox.ButtonSymbols.NoButtons
    assert spin.minimum() == 0
    assert spin.maximum() == 2_000_000_000
    win.close()


def test_save_env_button_removed(qapp, qloop, tmp_path) -> None:
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None

    assert not hasattr(settings, "btn_save_env")
    assert not hasattr(settings, "_on_save_env")
    buttons = settings.findChildren(QPushButton)
    assert not any("仅保存到" in b.text() for b in buttons)
    assert len([b for b in buttons if b.text() == "保存并应用"]) == 1
    win.close()


def test_core_validate_backends_api_kept() -> None:
    from tgmonitor.core.app_service import AppService

    assert hasattr(AppService, "validate_backends")
