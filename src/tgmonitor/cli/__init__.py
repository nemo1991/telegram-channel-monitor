"""CLI 子命令包:`tgmonitor sync …` / `tgmonitor monitor …`。

被 `tgmonitor.__main__` 在收到 sync / monitor 子命令时调用,本包内任何
模块都不 import PySide6 / qasync / `tgmonitor.ui.*`(纯 asyncio 即可)。
CLI 入口走 `core.runtime.bootstrap()`,与 GUI 共用 composition root。

2026-09-27 refactor:与 `app.py` 一同抽出,提供 headless(server / 容器 /
cron)环境跑监听与同步的能力。
"""

from __future__ import annotations