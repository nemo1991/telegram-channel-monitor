# 插件示例

两个最小可运行示例，对应两条不同的发现通道。它们不是装饰性代码：
`tests/test_plugin_examples.py` 会把示例文件复制到临时目录，用**真实的发现器与
宿主**加载执行，因此示例与宿主契约的漂移会被 CI 抓住。

| 示例 | 通道 | 展示内容 |
|---|---|---|
| [`plugins/demo_plugin/`](plugins/demo_plugin/) | 本地目录（`<user_data_dir>/plugins/<id>/`） | events / export / ui / schedule + 装配期与运行期两层生命周期 + 窄门面 |
| [`cli_provider/`](cli_provider/) | 独立 entry-point `tgmonitor.cli.commands` | 第三方子命令 `demo-hello` |

## 本地目录示例：demo_plugin

把整个 `plugins/demo_plugin/` 目录拷到用户数据目录下的 `plugins/`：

```
<user_data_dir>/plugins/demo_plugin/
├── plugin.toml   # 身份、版本、能力声明（发现阶段读取，无需实例化）
└── plugin.py     # Plugin 契约实现
```

然后在「设置 → 账户 → 插件管理」里启用它。发现 ≠ 启用：仅拷贝目录不会激活插件。

它展示的契约点：

- **manifest** 是类属性，发现阶段只读；本地目录的 `id` 以**目录名**为准。
- **装配期** `activate(ctx)` / `deactivate()`：拿上下文、订阅 `MessageReceived`、
  写自己的 KV 配置。订阅由宿主的 `ctx.deactivate_hooks()` 统一退订。
- **运行期** `start()` / `stop()`：由 `AppService.start()` / `pause` / `resume` /
  `shutdown` 驱动，语义是「会话就绪」，可反复触发，**必须幂等**。
- **窄门面**：只拿到 `ctx.bus` / `ctx.api` / `ctx.config` / `ctx.scheduler` /
  `ctx.settings` / `ctx.notify()`；拿不到 AppService、Telethon client 或 Qt 对象。
- **export**：声明 `DemoExporter`，格式 `demo-json`。注册与注销由宿主驱动，
  插件不碰全局注册表。
- **ui**：`ui_contribution` 只返回数据与 factory；Qt import 在 `page_factory()`
  内部，因为本地插件在 CLI 进程也会被激活，顶层 import Qt 会破坏 CLI 零 GUI 依赖。
- **schedule**：`ctx.scheduler.every()` 登记周期任务（`demo-tick`），插件**不**自己
  `create_task()`；宿主负责 start 物化、pause 取消、resume 重启、shutdown 回收。
  默认间隔 60 秒，测试把 config 的 `tick_seconds` 调小以真实观察执行。

## CLI 命令示例：cli_provider

```bash
pip install -e examples/cli_provider
tgmon-cli demo-hello --name plugin
```

CLI 命令走**独立** entry-point group，不经过 `PluginHost`：解析器构造早于
bootstrap，而插件激活依赖 bootstrap 后的域服务，统一通道会形成死结。
provider 自带 parser 定义与 async handler，业务失败抛 `CliError` 由主入口转退出码。

## 能力边界

五类能力全部有对应示例或独立通道：events / export / ui / schedule 在
`demo_plugin`，cli 在 `cli_provider`（独立 entry-point 通道）。调度粒度只有
「周期」与「延时单次」，不做 cron 解析；需要 cron 的插件在自己的回调里判断
时间窗口。完整契约见 `docs/PLUGIN-DEVELOPMENT.md` 5.4 与 ADR-001 决策 5。
