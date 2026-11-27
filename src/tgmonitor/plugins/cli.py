"""CLI 命令扩展点 — 独立 entry-point 通道,不依赖 PluginHost 激活态。

**为什么独立通道**(ADR-001 决策 5):`build_parser()` 在 `main()` 里、
`bootstrap()` 之前执行;而插件经 `PluginHost.activate(ctx)` 需要域服务
(`app_services`),后者只有 `cli_context()` bootstrap 之后才存在。若 CLI 命令
走统一 host 通道,会形成「parser 要命令 → 命令要激活 → 激活要 bootstrap →
bootstrap 在 parse 之后」的死结。因此 CLI provider 自带 parser 定义与 handler,
handler 内部自行用 `cli_context()` 拿 app,宿主只在 parse 期发现「命令」。

**零 GUI 依赖**:本模块与 provider 契约都不碰 PySide6 / qasync / ui。CLI 进程
的 `sys.modules` 守护测试继续有效(provider 若 import Qt 属插件作者违约)。

发现失败隔离:坏 entry-point / 契约不符的 provider 只记日志并跳过,绝不让
一个坏插件让整个 CLI 无法启动(`help` / 内置命令照常可用)。
"""

from __future__ import annotations

import logging
from importlib.metadata import EntryPoint, entry_points
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

# 独立 group — 与插件系统的 tgmonitor.plugins 分开,避免 build_parser 提前激活。
CLI_COMMAND_GROUP = "tgmonitor.cli.commands"


@runtime_checkable
class CliCommandProvider(Protocol):
    """第三方 CLI 命令 — 自包含(parser 定义 + async handler)。

    `name` 是顶层子命令名;与内置 `CLI_SUBCOMMANDS` 冲突时由 `main()` 丢弃
    (内置优先,插件不能劫持内置命令)。`configure_parser` 收到 argparse 的
    subparsers action,自行 `add_parser(name, ...)` 声明参数。`run(args)` 是
    async,返回退出码(int)或 None(等价 0);业务失败抛 `CliError`。

    provider 是**类**(entry-point 指向类,宿主实例化),契约检查按类属性做。
    """

    name: str

    def configure_parser(self, subparsers: Any) -> None: ...

    async def run(self, args: Any) -> int | None: ...


def _is_provider_cls(obj: object) -> bool:
    """结构化契约检查:有 configure_parser + run 两个方法即可,不强求继承。

    `name` 不在类上查 —— 它常由 `__init__` 设成实例属性(与 manifest 类属性
    不同),实例化后再校验。这样第三方 provider 写法更自然。
    """
    return all(hasattr(obj, attr) for attr in ("configure_parser", "run"))


def discover_cli_commands() -> list[CliCommandProvider]:
    """从 `tgmonitor.cli.commands` entry-point group 发现命令 provider。

    每个 entry-point 指向 provider 类;宿主实例化后返回。任一步失败只记日志
    并跳过该 provider,不影响其它命令与内置 CLI。
    """
    found: list[CliCommandProvider] = []
    eps: list[EntryPoint]
    try:
        eps = list(entry_points(group=CLI_COMMAND_GROUP))
    except Exception:
        log.exception("CLI 命令 entry-points 发现失败,跳过扩展通道")
        return found

    for ep in eps:
        try:
            obj = ep.load()
            if not (isinstance(obj, type) and _is_provider_cls(obj)):
                log.warning("CLI entry-point %s 未指向 CliCommandProvider 类,跳过", ep.name)
                continue
            provider = obj()
            if not isinstance(provider.name, str) or not provider.name:
                log.warning("CLI provider %s 的 name 非法,跳过", ep.name)
                continue
            found.append(provider)
        except Exception:
            log.exception("CLI entry-point %s 加载失败,跳过", ep.name)
    return found


__all__ = ["CLI_COMMAND_GROUP", "CliCommandProvider", "discover_cli_commands"]
