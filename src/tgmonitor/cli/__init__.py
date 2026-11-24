"""CLI 子命令包 — 2.1.0 起为完备命令体系。

被 `tgmonitor.__main__` 在收到 `CLI_SUBCOMMANDS` 中的子命令时调用,本包内
任何模块都不 import PySide6 / qasync / `tgmonitor.ui.*`(纯 asyncio 即可)。
CLI 入口走 `core.runtime.bootstrap()`,与 GUI 共用 composition root。

2026-09-27 refactor:与 `app.py` 一同抽出,提供 headless(server / 容器 /
cron)环境跑监听与同步的能力。
2026-11-24 v2.1.0:扩充为 status / login / logout / channels / pause /
resume / messages / media / export / config 全功能,`_common.py` 收口样板。
"""

from __future__ import annotations
