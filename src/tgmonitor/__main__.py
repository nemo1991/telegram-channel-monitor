"""入口:`python -m tgmonitor` 启动桌面应用或 CLI 子命令。

2026-09-27 refactor:argv 分流 —
  - 无参数或首参 `gui`:`app.run()`(桌面 GUI,既有路径)
  - 首参属于 `CLI_SUBCOMMANDS`:`cli.main()`(CLI 子命令,**不**加载 PySide6)
  - `--help` / `-h`:argparse 帮助(经 `cli.main`)

2.1.0:CLI 子命令白名单从 `cli.main.CLI_SUBCOMMANDS` 单一真源读取,新增
子命令不再需要改两处。CLI 路径严格不 import `tgmonitor.app`,从而保证
PySide6 / qasync 永不加载(守门测试 `test_cli_does_not_import_qt_or_qasync`)。

退出码:
  0 — 正常退出
  1 — 未捕获异常(已 stderr 打印)
  130 — KeyboardInterrupt(SIGINT,跟 shell 约定一致)
"""

from __future__ import annotations

import sys


def main() -> int:
    """进程入口 — argv 分流到 GUI / CLI 子命令。"""
    argv = sys.argv[1:]
    if argv and argv[0] in ("--help", "-h"):
        from tgmonitor.cli.main import main as cli_main

        return cli_main(argv)

    # CLI 子命令白名单:单一真源(cli.main),函数内 import 不加载 Qt
    from tgmonitor.cli.main import CLI_SUBCOMMANDS

    if argv and argv[0] in CLI_SUBCOMMANDS:
        from tgmonitor.cli.main import main as cli_main

        return cli_main(argv)

    # 默认 / 显式 `gui` → 桌面应用(既有路径)
    from tgmonitor.app import run

    try:
        run()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"[tgmonitor] fatal: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
