"""示例插件 — 展示已落地的四类能力（events / export / ui / schedule）与两层生命周期。

这是**活文档**：`tests/test_plugin_examples.py` 会把它复制到临时 plugins_dir，
用真实的 `discover_local` + `PluginHost` 加载并执行全链。代码腐烂会被 CI 抓住，
不是 README 里一段永远不对齐的片段。

顶层只 import 宿主契约，**不 import PySide6** —— 本地目录插件在 CLI 进程也会被
`PluginHost.activate_all()` 激活，顶层 import Qt 会破坏 CLI 零 GUI 依赖。
UI 的 Qt import 推迟到 `page_factory()` 内部，只有 GUI 装配期才触发。

`schedule` 走宿主统一任务表：插件只 `ctx.scheduler.every(...)` 登记，任务的
物化、pause/resume 启停、shutdown 取消回收全部由宿主负责；插件**不**自己
`asyncio.create_task()`。CLI 命令走独立的 `tgmonitor.cli.commands` entry-point
通道，见 `examples/cli_provider/`。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from tgmonitor.core.dto import ChannelDTO, MessageDTO
from tgmonitor.core.events import MessageReceived
from tgmonitor.core.export.base import Exporter
from tgmonitor.plugins.api import PluginContext, PluginManifest, UiContribution

# manifest 是类属性 —— 发现阶段无需实例化即可读（entry-point 通道尤其如此）。
# 本地目录通道以 plugin.toml 为准，两边保持一致避免切换通道时行为漂移。
MANIFEST = PluginManifest(
    id="demo_plugin",
    name="示例插件",
    version="1.0.0",
    min_app_version="3.0.0",
    capabilities=("events", "export", "ui", "schedule"),
    description="演示事件订阅、自定义导出格式、UI 页面、定时任务与两层生命周期",
)


class DemoExporter(Exporter):
    """export 能力：声明一个宿主原生没有的导出格式。

    host 在 `activate()` 成功后统一注册、`deactivate` 时统一注销；插件自己不碰
    全局 `EXPORTERS` —— 激活失败或停用都不会留下「幽灵格式」。
    """

    format = "demo-json"
    extension = ".demo.json"

    async def render(
        self,
        out_path: Path,
        channels: dict[int, ChannelDTO],
        messages: list[MessageDTO],
        *,
        object_store: Any = None,
        include_thumbnails: bool = False,
        include_metadata: bool = True,
        storage: Any = None,
    ) -> int:
        payload = {
            "plugin": self.format,
            "channel_count": len(channels),
            "message_count": len(messages),
            "include_metadata": include_metadata,
            "messages": [asdict(m) for m in messages],
        }
        # default=str 兼容 datetime 等非 JSON 原生字段，示例无需关心序列化细节。
        # 与内置 exporter 一致：已在 IO 阻塞路径，同步写盘即可，不切线程。
        out_path.write_text(  # noqa: ASYNC240 — 同内置 exporter，写文件同步完成
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return out_path.stat().st_size  # noqa: ASYNC240 — 同步 stat 写盘后的路径


class DemoPlugin:
    """Plugin 契约实现 —— 结构化契约，不需要继承任何基类。"""

    manifest = MANIFEST
    exporters = (DemoExporter(),)

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._running = False
        self.message_count = 0
        self.tick_count = 0

    # ---- 装配期：由 runtime.bootstrap() 驱动 ----
    def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        # config 是按插件 id 隔离的 KV，不进主 .env / Settings 链。
        # 类型可以是 JSON 支持的任意值（数字 / 布尔 / 数组），管理页会保留类型。
        if ctx.config.get("greeting") is None:
            ctx.config.set("greeting", "hello from demo_plugin")
        # 订阅记在 ctx.bus 里，宿主 deactivate 时统一退订；不要自己存裸 EventBus。
        ctx.bus.subscribe(MessageReceived, self._on_message)
        # schedule 能力：只登记「做什么、多久一次」。Task 的物化 / pause 取消 /
        # resume 重启 / shutdown 回收全由宿主任务表负责，插件不碰 asyncio API。
        # 默认 60 秒避免示例在正常运行时刷屏；测试通过 config 改成极小间隔验证。
        tick_seconds = float(ctx.config.get("tick_seconds", 60))
        ctx.scheduler.every(
            tick_seconds,
            self._on_tick,
            name="demo-tick",
            run_immediately=True,
        )

    def deactivate(self) -> None:
        # 退订 / 任务回收由宿主统一处理，这里只清自有状态。
        self._ctx = None
        self._running = False

    # ---- 运行期：由 AppService.start / pause / resume / shutdown 驱动 ----
    def start(self) -> None:
        """会话就绪后开始干活。pause/resume 会反复触发，必须幂等。"""
        if self._running:
            return
        self._running = True
        if self._ctx is not None:
            self._ctx.notify("info", "示例插件", "会话已就绪，示例插件开始工作")

    def stop(self) -> None:
        """暂停 / 退出时停下。同样幂等。"""
        if not self._running:
            return
        self._running = False

    # ---- 能力声明 ----
    @property
    def ui_contribution(self) -> UiContribution:
        """只返回数据 + factory，不构造任何 Qt 对象（plugins 包零 Qt 依赖）。"""
        return UiContribution(
            nav_label="示例插件",
            icon_name="nav_dashboard",  # 复用宿主内置图标集
            page_factory=self._make_page,
        )

    # ---- 内部 ----
    async def _on_message(self, event: MessageReceived) -> None:
        self.message_count += 1

    def _on_tick(self) -> None:
        """定时任务回调：同步 callable 也支持，宿主会在事件循环里调用它。"""
        self.tick_count += 1

    def _make_page(self) -> Any:
        """只有 GUI 装配期才会被调用 —— Qt import 留在这里，CLI 进程永不触发。"""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("示例插件")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        layout.addWidget(QLabel("这个页面由插件贡献，宿主不认识它的内容。"))
        layout.addWidget(QLabel(f"已收到消息：{self.message_count}"))
        layout.addWidget(QLabel(f"定时任务触发：{self.tick_count}"))
        return page
