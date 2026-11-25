"""v2.0.10 issue #131 — 设置页左侧分组导航 + 搜索过滤契约。

- 左侧导航项 = 7 个分组标题(账户/监听频道/代理/存储/对象存储/策略/同步);
- 点选某项:右侧只显示该分组,其余隐藏;
- 搜索框:按分组标题 / 字段 label / 字段 key 过滤导航项;
- 当前项被过滤掉时自动跳到首个可见项;
- 底部「保存」始终可见。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from PySide6.QtWidgets import (
    QGroupBox,
    QMessageBox,
)

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.core.settings_store import read_close_on_exit
from tgmonitor.ui.widgets.settings_page import SettingsPage

_QSS_PATH = Path(__file__).resolve().parents[1] / "src/tgmonitor/ui/resources/style.qss"

_EXPECTED_TITLES = (
    "账户凭证",
    "监听频道管理",
    "网络代理 (Proxy)",
    "消息存储 (Database)",
    "对象存储 (ObjectStore)",
    "策略",
    "同步参数",
)


def _nav_titles(settings: SettingsPage) -> list[str]:
    return [settings.nav_list.item(i).text() for i in range(settings.nav_list.count())]


def _visible_groups(settings: SettingsPage) -> list[QGroupBox]:
    """用 isHidden() 判断导航切换效果(窗口未 show 时 isVisible() 恒 False)。"""
    return [g for g in settings.findChildren(QGroupBox) if not g.isHidden()]


def test_nav_switch_shows_only_selected_group(qapp, qloop, tmp_path):
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None

    settings.nav_list.setCurrentRow(2)  # 网络代理
    visible = _visible_groups(settings)
    assert len(visible) == 1
    assert visible[0].objectName() == "settingsSection"
    assert "代理" in visible[0].title()

    settings.nav_list.setCurrentRow(1)  # 监听频道管理
    visible = _visible_groups(settings)
    assert len(visible) == 1
    assert visible[0].objectName() == "settingsChannelMonitorGroup"
    win.close()


def test_close_on_exit_uncheck_persists_and_reloads(qapp, qloop, tmp_path, monkeypatch) -> None:
    """取消勾选并保存后 .env 必须是 false,重开设置页也要回填 false。

    回归 #142:`_collect()` / `_load_from_settings()` 曾漏掉 close_on_exit,
    勾选框状态既不写盘也不回填,save 一直按默认 True 写回,关窗仍退出。
    """
    win = build_real_main_window(qloop, tmp_path)
    settings = win.findChild(SettingsPage)
    assert settings is not None

    settings.chk_close_on_exit.setChecked(False)
    assert settings._collect().close_on_exit is False

    # 把异步保存同步跑完,避免测试弹模态框。
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)

    def _sync_run(loop, coro, *, on_success=None, **kwargs):
        result = asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=10)
        if on_success is not None:
            on_success(result)

    monkeypatch.setattr("tgmonitor.ui.widgets.settings_page.run_coro", _sync_run)
    settings.btn_apply.click()

    env_path = tmp_path / ".env"
    assert env_path.exists()
    assert read_close_on_exit(env_path) is False

    # 模拟下次启动:Settings 从 .env 还原为 false → 复选框必须跟着取消勾选。
    win.app.settings = win.app.settings.model_copy(update={"close_on_exit": False})
    settings._load_from_settings()
    assert settings.chk_close_on_exit.isChecked() is False
    win.close()
