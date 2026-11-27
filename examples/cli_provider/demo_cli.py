"""CLI 命令 provider 示例 — 独立的 `tgmonitor.cli.commands` entry-point 通道。

为什么不走 `tgmonitor.plugins` 统一通道：`build_parser()` 在 `bootstrap()` 之前
执行，而插件 `activate(ctx)` 需要 bootstrap 后才有的域服务，统一通道会形成
「parser 要命令 → 命令要激活 → 激活要 bootstrap → bootstrap 在 parse 之后」的死结。
所以 CLI 命令 provider 自带 parser 定义与 async handler，handler 内部若需要宿主
能力，自行用 `cli_context()` 获取。

本模块零 Qt import —— CLI 的 `sys.modules` 守护测试会检查这一点。
"""

from __future__ import annotations

from typing import Any

from tgmonitor.cli._common import CliError


class DemoHelloCommand:
    """第三方子命令示例：`tgmon-cli demo-hello --name ...`。

    契约是结构化的（configure_parser + run + name），不需要继承 Protocol。
    entry-point 指向**类**，宿主负责实例化。
    """

    name = "demo-hello"

    def configure_parser(self, subparsers: Any) -> None:
        parser = subparsers.add_parser(self.name, help="示例插件命令：打招呼")
        parser.add_argument("--name", default="world", help="要打招呼的名字")

    async def run(self, args: Any) -> int:
        if not args.name:
            # 业务失败抛 CliError → main() 转成退出码 1 + stderr 提示，不打 traceback。
            raise CliError("--name 不能为空")
        print(f"hello, {args.name}!")
        return 0
