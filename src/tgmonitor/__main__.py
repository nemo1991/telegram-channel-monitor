"""入口:`tgmon` 启动桌面 GUI(2.1.2 起不再分流 CLI)。

历史:
  - 2026-09-27:argv 分流 — 无参数 / `gui` → GUI,CLI 子命令 → `cli.main()`
  - 2.1.1:未知首参不再回退 GUI,stderr 报错 + 退出码 2
  - 2.1.2:**命令体系收敛** — CLI 全部移到 `tgmon-cli`,本模块只负责 GUI。
    `tgmonitor` console script 删除;`tgmon` 无参数 / `gui` 启动桌面界面,
    其余参数一律报错并提示用 `tgmon-cli`。

`python -m tgmonitor` 仍走本模块(纯 GUI);CLI 请用 `tgmon-cli` 或
`python -m tgmonitor.cli.main`。

退出码:
  0 — 正常退出 / `--help`
  1 — 未捕获异常(已 stderr 打印)
  2 — 无法识别的参数
  130 — KeyboardInterrupt(SIGINT,跟 shell 约定一致)
"""

from __future__ import annotations

import sys


def main() -> int:
    """`tgmon` 进程入口 — 纯 GUI 启动器,不接任何 CLI 子命令。"""
    argv = sys.argv[1:]
    if argv and argv[0] in ("--help", "-h", "help"):
        print("用法: tgmon [gui] — 启动桌面界面")
        print("命令行操作请用 tgmon-cli <cmd>,总览: tgmon-cli help")
        return 0

    if argv and argv != ["gui"]:
        print(f"[tgmon] 无法识别的参数: {' '.join(argv)}", file=sys.stderr)
        print("[tgmon] tgmon 只启动桌面界面;命令行操作请用 tgmon-cli", file=sys.stderr)
        return 2

    from tgmonitor.app import run

    try:
        run()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"[tgmon] fatal: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
