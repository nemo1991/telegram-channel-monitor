"""CLI 公共基础设施 — 2.1.0。

统一收口 cmd 之间重复的样板:上下文装配 / 输出通道 / 错误 / 进度订阅 /
日期解析。设计见 `docs/PRD-2.1.0-CLI.md` §4.4。

架构铁律:本模块(及整个 cli 包)绝不 import PySide6 / qasync / ui / app。
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import json
import sys
from collections.abc import AsyncIterator, Iterable
from typing import Any

from tgmonitor.core.app_service import AppService
from tgmonitor.core.config import Settings
from tgmonitor.core.events import (
    BatchProgress,
    ChannelSyncProgress,
    EventBus,
    ExportProgress,
    MediaDownloadProgress,
)
from tgmonitor.core.monitor.service import MonitorService
from tgmonitor.core.runtime import bootstrap, shutdown


class CliError(Exception):
    """业务错误 — `main()` 捕获后 stderr 打印 + 退出码 1。

    与 argparse 的用法错误(退出码 2)区分:这里是"命令合法但执行失败"
    (未登录 / 校验失败 / 后端不可达 / 操作失败)。
    """


def progress(msg: str) -> None:
    """进度事件 → stderr(不污染 stdout 的数据通道)。"""
    print(f"[progress] {msg}", file=sys.stderr, flush=True)


def info(msg: str) -> None:
    """普通提示 → stderr(写操作结果摘要等,不进数据通道)。"""
    print(msg, file=sys.stderr, flush=True)


def _json_default(value: Any) -> Any:
    """终极兜底(正常情况 `_to_plain` 已处理完):未知类型转字符串。

    不用 `TypeError` 抛出 — CLI 输出宁可降级为字符串也不能因一个字段炸掉。
    """
    return str(value)


def _to_plain(value: Any) -> Any:
    """递归把 DTO / Enum / datetime 转成 JSON 原生的 dict/list/scalar。"""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if hasattr(value, "value") and type(value).__module__ != "builtins":
        # Enum 实例:有 .value 且非内置类型;用 isinstance(value, Enum) 更准,
        # 这里刻意 duck typing 以支持 IntEnum / StrEnum 等所有变体。
        import enum

        if isinstance(value, enum.Enum):
            return _to_plain(value.value)
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_plain(v) for v in value]
    return value


def emit_json(data: Any) -> None:
    """统一 JSON 输出 → stdout。DTO / Enum / datetime 递归转原生类型。"""
    print(json.dumps(_to_plain(data), ensure_ascii=False, default=_json_default), flush=True)


def emit_table(rows: Iterable[str]) -> None:
    """统一文本表格输出 → stdout,每行一条。"""
    for row in rows:
        print(row, flush=True)


def emit(data: Any, *, as_json: bool, text_rows: Iterable[str] | None = None) -> None:
    """输出统一出口:`--json` 走 JSON,否则走调用方提供的文本行。"""
    if as_json:
        emit_json(data)
    elif text_rows is not None:
        emit_table(text_rows)


def parse_date(value: str) -> dt.datetime:
    """解析 `YYYY-MM-DD` → UTC 午夜 datetime;非法输入抛 ValueError。

    CLI 的 `--from/--to` 走这里,与 MessageService 的 date_from/date_to 对齐。
    """
    return dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=dt.UTC)


def parse_csv_list(value: str | None) -> list[str]:
    """解析逗号分隔参数(--tags a,b,c 等),空串→空 list。"""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@contextlib.asynccontextmanager
async def cli_context(
    args: Any,
    *,
    app: AppService | None = None,
    monitor: MonitorService | None = None,
    need_backend: bool = True,
    skip_ready: bool = False,
) -> AsyncIterator[tuple[AppService, MonitorService | None, Settings]]:
    """CLI 命令的统一上下文。

    - `need_backend=True`(默认):`runtime.bootstrap()` → 就绪检查,
      `finally shutdown()`。未 ready 抛 `CliError`。
    - `need_backend=False`:只读 `Settings.load_tolerant()`,不建后端
      (config show/get 等纯配置命令用)。
    - `app` / `monitor` 入参:测试 DI,注入 fakes,跳过 bootstrap/shutdown。

    `skip_ready`:登录命令用 — bootstrap 装配后端但不强制 ready(未登录时需登录)。
    `args` 需带 `json` 属性(供就绪错误信息格式化的兜底,非必需)。
    """
    if app is not None:
        # 测试注入:不 bootstrap、不 shutdown、不强制 ready 检查由命令自行决定
        assert app is not None
        yield app, monitor, app.settings
        return

    if not need_backend:
        settings, _warnings = Settings.load_tolerant()
        # 轻量 app 仅承载 settings / env,不接后端;命令只读不改。
        light = AppService.__new__(AppService)
        light.settings = settings
        yield light, None, settings
        return

    real_app, real_monitor, settings, _ = await bootstrap()
    try:
        if not skip_ready:
            state, detail = await real_app.bootstrap()
            if state != "ready":
                raise CliError(
                    f"未就绪(state={state}{', ' + detail if detail else ''});"
                    "请先执行 tgmonitor login"
                )
        yield real_app, real_monitor, settings
    finally:
        await shutdown(real_app, real_monitor)


_PROGRESS_EVENTS = (ChannelSyncProgress, ExportProgress, BatchProgress, MediaDownloadProgress)


async def attach_progress(bus: EventBus, *, detail: Any = None) -> None:
    """订阅 4 类进度事件 → stderr。

    订阅者是 async(bus.subscribe_all 契约)。返回后调用方应 `await bus.flush()`
    或在命令末尾统一 flush。
    """
    del detail  # 预留扩展(如 --quiet),当前统一输出

    async def _on_progress(e: Any) -> None:
        cls_name = type(e).__name__
        # 各事件字段不完全一致,只挑通用的可读信息
        bits = [f"{k}={v}" for k, v in vars(e).items() if k != "occurred_at" and v is not None]
        progress(f"{cls_name} " + " ".join(bits))

    for event_cls in _PROGRESS_EVENTS:
        bus.subscribe(event_cls, _on_progress)
