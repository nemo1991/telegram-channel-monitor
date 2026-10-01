"""Per-domain controller 包 — `MainWindow` 拆细后业务逻辑落点。

2026-10-01 v1.12.x:6 个低风险 controller(theme / tray / header_action /
settings_reaction / export / sync)。复杂 4 个(live_selection /
media_actions / search / message_stream)留 `main_window.py`,跟随后续 PR。

每个 controller 是 plain class(`MainWindowCtx` dataclass,用法见 `_base.py`),
非 QObject。MainWindow 仍持真实 widget 状态,controller 通过 ctx 访问 —
测试 thin delegate 模式:`MainWindow._on_X(...)` → `self._X_ctrl.method(...)`,
保留 9 个 `test_main_window_*.py` 的 `cast(MainWindow, win)` 调用方式。
"""

from tgmonitor.ui.controllers._base import MainWindowCtx
from tgmonitor.ui.controllers.export_controller import ExportController
from tgmonitor.ui.controllers.header_action_controller import HeaderActionController
from tgmonitor.ui.controllers.settings_reaction_controller import SettingsReactionController
from tgmonitor.ui.controllers.sync_controller import SyncController
from tgmonitor.ui.controllers.theme_controller import ThemeController
from tgmonitor.ui.controllers.tray_controller import TrayController

__all__ = [
    "MainWindowCtx",
    "ThemeController",
    "TrayController",
    "HeaderActionController",
    "SettingsReactionController",
    "ExportController",
    "SyncController",
]
