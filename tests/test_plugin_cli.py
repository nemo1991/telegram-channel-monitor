"""CLI 扩展点测试 — ADR-001 阶段 4:独立 entry-point 通道 + parser 合并 + 路由。

手法:entry-point 发现用 monkeypatch 换成假 provider(不依赖真实装包);
`main()` 路由用假 provider 的 run AsyncMock 断言被调用,不触发真 bootstrap。
"""

from __future__ import annotations

import pytest

from tgmonitor.cli.main import CLI_SUBCOMMANDS, build_parser, main
from tgmonitor.plugins.cli import CLI_COMMAND_GROUP, discover_cli_commands


class _FakeProvider:
    """测试用 CliCommandProvider — 结构化契约,不继承 Protocol。

    `configure_parser` / `run` 定义在类上(契约检查按类属性做);`name` 由
    `__init__` 设成实例属性。run 调用记录走实例级 `_calls`。
    """

    def __init__(self, name: str, *, run_result: int | None = 0, fail_parser: bool = False) -> None:
        self.name = name
        self.run_result = run_result
        self.fail_parser = fail_parser
        self.calls: list[object] = []

    def configure_parser(self, subparsers) -> None:
        if self.fail_parser:
            raise RuntimeError("parser boom")
        subparsers.add_parser(self.name, help=f"插件命令 {self.name}")

    async def run(self, args) -> int | None:
        self.calls.append(args)
        return self.run_result


@pytest.fixture(autouse=True)
def _no_real_entry_points(monkeypatch):
    """默认不依赖真实安装的 entry-points;具体用例自行 monkeypatch。"""
    monkeypatch.setattr("tgmonitor.plugins.cli.entry_points", lambda **kw: [])


def test_entry_point_group_name() -> None:
    assert CLI_COMMAND_GROUP == "tgmonitor.cli.commands"


def test_discover_isolates_bad_entry_points(monkeypatch) -> None:
    """坏 entry-point(load 抛错 / 契约不符)被跳过,好 provider 照常发现。"""

    class _EP:
        def __init__(self, name, obj):
            self.name = name
            self._obj = obj

        def load(self):
            if isinstance(self._obj, Exception):
                raise self._obj
            return self._obj

    class _GoodProvider(_FakeProvider):
        name = "good"  # 类属性 — entry-point 宿主无参实例化

        def __init__(self) -> None:
            super().__init__("good")

    monkeypatch.setattr(
        "tgmonitor.plugins.cli.entry_points",
        lambda **kw: [
            _EP("exploding", RuntimeError("load boom")),
            _EP("not_a_class", object()),  # 实例而非类
            _EP("good", _GoodProvider),  # 类 — 宿主无参实例化
        ],
    )
    found = discover_cli_commands()
    assert [p.name for p in found] == ["good"]


def test_discover_no_entry_points_returns_empty() -> None:
    assert discover_cli_commands() == []


def test_build_parser_merges_plugin_command() -> None:
    provider = _FakeProvider("hello")
    parser = build_parser(cli_providers=[provider])
    args = parser.parse_args(["hello"])
    assert args.cmd == "hello"


def test_build_parser_builtin_wins_on_name_collision() -> None:
    """插件与内置重名时被丢弃,内置命令行为不变。"""
    provider = _FakeProvider("status")  # 内置命令
    parser = build_parser(cli_providers=[provider])
    args = parser.parse_args(["status", "--json"])
    assert args.cmd == "status" and args.json is True


def test_build_parser_bad_provider_does_not_block() -> None:
    """provider 的 parser 构造抛错被吞,内置命令仍可解析。"""
    bad = _FakeProvider("broken", fail_parser=True)
    parser = build_parser(cli_providers=[bad])
    args = parser.parse_args(["status"])
    assert args.cmd == "status"


def test_default_build_parser_has_no_plugins() -> None:
    """默认 build_parser() 零插件 —— 守护测试与冷启动路径行为不变。"""
    subparsers = next(
        a for a in build_parser()._actions if a.__class__.__name__ == "_SubParsersAction"
    )
    assert set(subparsers.choices) == set(CLI_SUBCOMMANDS)


def test_main_routes_plugin_command(monkeypatch) -> None:
    """main() 发现插件 provider 后路由到其 run。"""
    provider = _FakeProvider("hello", run_result=7)
    monkeypatch.setattr("tgmonitor.plugins.cli.discover_cli_commands", lambda: [provider])
    assert main(["hello"]) == 7
    assert len(provider.calls) == 1


def test_main_builtin_not_shadowed(monkeypatch) -> None:
    """重名内置命令仍走内置,插件 run 不被调用(用 help 避免真 bootstrap)。"""
    provider = _FakeProvider("help")
    monkeypatch.setattr("tgmonitor.plugins.cli.discover_cli_commands", lambda: [provider])
    assert main(["help"]) == 0
    assert provider.calls == []


def test_main_discovery_failure_still_runs_builtin(monkeypatch) -> None:
    """发现通道整体抛错时内置命令不受影响(help 不崩)。"""
    monkeypatch.setattr(
        "tgmonitor.plugins.cli.discover_cli_commands",
        lambda: (_ for _ in ()).throw(RuntimeError("discovery boom")),
    )
    assert main(["help"]) == 0
