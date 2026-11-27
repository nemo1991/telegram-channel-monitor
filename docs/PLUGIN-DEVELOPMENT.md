# 插件开发指南（3.0.0）

> **适用版本**：插件系统已在 `main` 落地，随 **3.0.0** 发布；2.1.7 及更早的发行包不含插件系统。
> 插件契约版本 `PLUGIN_API_VERSION = 3.0.0` 与应用营销版本号解耦 —— 它只表示插件 SPI 的
> 兼容基线，不代表你装的发行包一定是 3.0.0。
>
> 本文只写**现状与用法**。设计取舍见 [`ADR-001-plugin-system.md`](ADR-001-plugin-system.md)，
> 最小可运行代码见 [`../examples/README.md`](../examples/README.md)（示例被 CI 真实加载执行，是活文档）。

---

## 1. 30 秒概览

插件是**进程内可信 Python 代码**，经唯一接缝 `PluginHost` 接入。宿主只把窄门面
`PluginContext` 交给插件；插件拿不到 `AppService`、Telethon 客户端或 Qt 对象。

三句话记住边界：

1. **发现 ≠ 启用**：装到插件目录里不会自动生效，必须显式启用；
2. **失败隔离**：坏插件被标 `FAILED` 并记日志，不阻断应用启动；
3. **热切换只覆盖启停**：`enable` / `disable` 立即生效，**改了插件代码必须重启**。

---

## 2. 使用者：安装、启用与配置

### 2.1 两种安装通道

| 通道 | 适合场景 | 落点 | 启用方式 |
|---|---|---|---|
| **本地目录** | 冻结包（AppImage / .app / onedir zip）、个人脚本、不能 `pip install` 的环境 | `<user_data_dir>/plugins/<id>/` | 管理页启用 |
| **entry-points** | 随 wheel / 源码分发的第三方包 | `[project.entry-points."tgmonitor.plugins"]` | 管理页启用 |

`<user_data_dir>` 由 platformdirs 决定，与 `session/` / `media/` 同级：

| 平台 | 插件目录 |
|---|---|
| macOS | `~/Library/Application Support/tgmonitor/plugins/` |
| Linux | `$XDG_DATA_HOME/tgmonitor/plugins/`（默认 `~/.local/share/tgmonitor/plugins/`） |
| Windows | `%LOCALAPPDATA%\tgmonitor\plugins\` |

**本地目录插件**至少两个文件，目录名就是插件 id（与 `plugin.toml` 里的 `id` 不一致时**以目录名为准**）：

```
<user_data_dir>/plugins/demo_plugin/
├── plugin.toml    # 发现阶段只读元数据，无需实例化插件
└── plugin.py      # 实现 Plugin 契约的类，类名随意
```

```toml
# plugin.toml
id = "demo_plugin"
name = "示例插件"
version = "1.0.0"
min_app_version = "3.0.0"
capabilities = ["events", "export", "ui"]
description = "演示事件订阅、导出格式与 UI 页面"
```

id 只允许 `[A-Za-z0-9_.-]+` —— 它同时是目录名、启用清单 key 和配置命名空间。

**entry-points 插件**在包自己的 `pyproject.toml` 声明，入口指向**类**（不是实例）：

```toml
[project.entry-points."tgmonitor.plugins"]
my_plugin = "my_package:MyPlugin"
```

同 id 时本地目录优先（用户本地覆盖优先于已装包）；两条通道的发现失败都只记日志并跳过。

### 2.2 发现 ≠ 启用

`<user_data_dir>/plugins/enabled.json` 是唯一的启用真源：

```json
{ "enabled": ["demo_plugin", "my_plugin"] }
```

文件不存在或等于空列表时，**没有任何插件被启用**。这是刻意的默认安全策略：装了坏插件也能进应用。

GUI 入口：**设置 → 账户凭证 → 插件管理**。对话框列出全部已发现插件及状态，可：

- **启用 / 停用**：立即生效（真热切换，无需重启）。停用会撤销事件订阅、注销导出格式、移除 UI 页面并停运行期钩子；
- **重新发现**：重新扫描双通道发现结果（本地目录与 entry-points），登记新插件；
- **编辑配置**：见下节。

### 2.3 插件配置

每个插件有独立 KV 命名空间，存在 `<user_data_dir>/plugins/config.json`：

```json
{
  "demo_plugin": { "greeting": "hello", "limit": 10, "enabled_flags": [true, false] }
}
```

它**不进主 `.env`**、不参与设置页的热重载链。管理页的「值」列按 **JSON 字面量**解析：
`true` / `42` / `[1,2]` / `{"k":1}` / `null` 会保留类型，其它文本按普通字符串保存。
插件代码用 `ctx.config.get(key, default)` / `set(key, value)` / `all()` 读写，写失败会抛异常由插件自行降级。

### 2.4 状态与排错

管理页显示四种状态：

| 状态 | 含义 | 常见原因 |
|---|---|---|
| `已发现` / `已停用` | 发现过但未启用 | `enabled.json` 没列它 |
| `已启用`（ACTIVE） | 能力已注册、运行期正常 | — |
| `失败`（FAILED） | 激活被隔离 | `activate()` 抛错、导出格式冲突、UI 贡献 getter 抛错、`min_app_version` 过高 |

失败详情显示在管理页详情区（`rec.error`），完整栈在 `<user_data_dir>/logs/tgmonitor.log`。
查找关键字 `plugin`、`activate 失败`、`插件 <id>`。单个插件失败不影响其它插件，也不影响监听主流程。

---

## 3. 作者：Plugin 契约

插件是**结构化契约**，不需要继承任何基类；`manifest` 必须是**类属性**（发现阶段不实例化插件）。

```python
from tgmonitor.plugins.api import PluginContext, PluginManifest, UiContribution

MANIFEST = PluginManifest(
    id="my_plugin",
    name="我的插件",
    version="1.0.0",
    min_app_version="3.0.0",
    capabilities=("events", "export", "ui"),
    description="做点什么",
)

class MyPlugin:
    manifest = MANIFEST

    # 装配期：runtime.bootstrap() 驱动
    def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        ctx.bus.subscribe(MessageReceived, self._on_message)

    def deactivate(self) -> None:
        # 退订由宿主统一处理，这里只清自有状态
        self._ctx = None

    # 运行期（可选）：AppService 驱动，必须幂等
    def start(self) -> None: ...
    def stop(self) -> None: ...
```

`PluginManifest` 字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | `[A-Za-z0-9_.-]+`；本地目录通道以目录名覆盖 |
| `name` | 是 | 管理页显示名 |
| `version` | 否 | 插件自身版本，默认 `0.1.0` |
| `min_app_version` | 否 | 要求的最低**插件 API 版本**，默认 `3.0.0`；宿主比它低则跳过本插件 |
| `capabilities` | 否 | `events` / `export` / `cli` / `ui` / `schedule`；未知名字记 warning 但不拒绝加载 |
| `description` | 否 | 管理页详情说明 |

### 3.1 两层生命周期（别混用）

| 层 | 钩子 | 谁驱动 | 什么时候 | 适合做什么 |
|---|---|---|---|---|
| **装配期** | `activate(ctx)` / `deactivate()` | `runtime.bootstrap()` / `shutdown()` | 服务装配完成后、会话就绪前 | 订阅事件、注册导出格式、读 UI 贡献、初始化配置 |
| **运行期** | `start()` / `stop()`（**可选**） | `AppService.start()` / `pause` / `resume` / `shutdown` | 仅会话 `ready` 后 | 定时任务、消费队列等「会话就绪才能干活」的工作 |

关键约束：

- `start()` **只在 `state == "ready"` 时触发**。未登录 / `phone_required` / error 状态下不会启动；
- `pause_monitor()` 成功路径调 `stop()`，`resume_monitor()` 成功路径调 `start()` —— 可反复触发；
- 因此 `start()` / `stop()` **必须幂等可重入**（示例插件用 `_running` 布尔位守卫）；
- 钩子未实现视为 no-op，老插件零改动可用；发现阶段不检查这两个方法；
- 钩子抛错只记日志、不拖垮启动 / 暂停 / 恢复 / 退出。

### 3.2 失败隔离保证

宿主对每个插件单独做版本门禁 → `activate()` → 导出格式注册 → UI 贡献读取，任一步失败：

- 该插件标 `FAILED`，记录错误文本；
- **回滚**已登记的事件订阅与已注册的导出格式（不留「幽灵能力」）；
- 其它插件继续激活，应用照常启动。

停用 (`disable`) 是逆过程：`stop()` → 注销导出格式 → 退订 → `deactivate()` → 移出启用清单。
每步失败只记日志，保证停用总能走完。

---

## 4. `PluginContext`：唯一可用的宿主能力面

`PluginContext` 是 frozen dataclass，字段集合被 `tests/test_plugin_api_surface.py` 锁死：

| 字段 / 方法 | 用途 | 边界 |
|---|---|---|
| `ctx.bus.subscribe(event_type, async_fn)` | 订阅领域事件 | 订阅由宿主记账，`deactivate` 时统一退订；**不开放 `subscribe_all`**（宿主无法注销它） |
| `ctx.bus.publish(event)` / `await publish_and_wait(event)` | 发布领域事件 | fire-and-forget / 等待版 |
| `ctx.api` | 有界读写域操作 | 方法集合被 allowlist 锁死，见下表 |
| `ctx.config` | 自己的 KV 命名空间 | 读写立即落盘，不影响其它插件 |
| `ctx.settings` | 只读快照 | `data_root` / `media_policy` / `db_backend` / `objectstore_backend` |
| `ctx.manifest` | 自己的清单 | 只读 |
| `ctx.notify(level, title, body)` | 用户通知 | 转 `NotificationRequested` 事件，托盘 / 状态栏既有订阅者呈现 |

`PluginApi` 的全部方法（新增必须改 allowlist 并说明原因）：

- 读：`list_messages()` / `list_media()` / `list_monitored_channels()`
- 写：`set_favorite()` / `set_tags()` / `set_notes()` / `delete_message()` / `retry_media()` / `download_media()`

写操作复用既有域服务的事件发布与幂等语义，不另开数据通路。**拿不到** `AppService`、
Telethon client、`StorageRepository`、裸 `EventBus` 或任何 Qt 对象。跨边界载荷一律是 DTO，
时间用 aware UTC。

> 安全提醒：插件是**可信进程内代码**，没有沙箱。只启用你信任来源的插件；API allowlist
> 防的是「门面腐烂」，不是恶意代码的隔离边界。

---

## 5. 五类能力：哪些能用，怎么用

| 能力 | 状态 | 落点 / 用法 |
|---|---|---|
| `events` | ✅ 可用 | `ctx.bus.subscribe()` / `publish()`；订阅者必须是 `async def` |
| `export` | ✅ 可用 | 插件提供 `Exporter` 实现，宿主在 `activate()` 成功后注册到 `EXPORTERS` |
| `cli` | ✅ 可用，但**独立通道** | `tgmonitor.cli.commands` entry-point group；不走 `PluginHost` |
| `ui` | ✅ 可用 | `UiContribution(nav_label, icon_name, page_factory)`，宿主在 GUI 装配期注入页面 |
| `schedule` | ⚠️ **尚未落地** | 能力名已登记，但宿主没有统一任务表与取消通道；**不要声明它**（见 5.4） |

### 5.1 导出（export）

```python
class MyExporter(Exporter):
    format = "my-json"          # 导出格式 key（str，不再是固定枚举）
    extension = ".my.json"

    async def render(self, out_path, channels, messages, *, object_store=None,
                     include_thumbnails=False, include_metadata=True, storage=None) -> int:
        ...
        return out_path.stat().st_size

class MyPlugin:
    exporters = (MyExporter(),)
    manifest = MANIFEST  # capabilities 含 "export"
```

宿主统一注册 / 注销，插件不碰全局 `EXPORTERS`；格式名与内置或其它插件冲突时该插件 `FAILED`，
其余插件不受影响。UI 下拉与 `tgmon-cli export --format` 的 choices 自动包含插件格式。
导出实现在阻塞 IO 路径上同步写盘即可（与内置 exporter 一致）。

### 5.2 UI（ui）

```python
@property
def ui_contribution(self) -> UiContribution:
    return UiContribution(
        nav_label="我的插件",
        icon_name="nav_settings",   # 复用宿主内置图标名
        page_factory=self._make_page,
    )

def _make_page(self):
    from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget  # ← 只在这里 import Qt
    ...
```

硬性要求：

- `UiContribution` 只含**数据 + factory**，不持有 QWidget / QIcon；宿主在 CLI 进程**不会**调 `page_factory`；
- 插件模块**顶层禁止 import PySide6 / qasync** —— 本地插件在 CLI 进程也会被激活，顶层 import Qt 会破坏 CLI 零 GUI 依赖。Qt import 放进 factory 内部（CI 有子进程守护）；
- `factory` 抛错或返回非 QWidget 只记日志跳过，不影响其它页面。插件页追加在内置 4 项导航之后；
- `icon_name` 用宿主已有图标名（如 `nav_dashboard` / `nav_settings`）。

管理页热切换后，宿主按 `plugin_id` **增量**同步导航与页面：停用 A 不会重建 B 的页面，
B 的滚动位置 / 表单状态不会丢。

### 5.3 CLI 命令（cli）

CLI 命令走**独立** entry-point group，不经过 `PluginHost`：

```toml
[project.entry-points."tgmonitor.cli.commands"]
demo_hello = "my_package:DemoHelloCommand"
```

```python
from tgmonitor.cli._common import CliError

class DemoHelloCommand:
    name = "demo-hello"                       # 顶层子命令名

    def configure_parser(self, subparsers) -> None:
        p = subparsers.add_parser(self.name, help="...")
        p.add_argument("--name", default="world")

    async def run(self, args) -> int:
        if not args.name:
            raise CliError("--name 不能为空")   # → 退出码 1，stderr 一行提示
        return 0
```

`capabilities` 里的 `"cli"` 只是**声明**，不会生成任何命令 —— 真正注册命令必须另写
`tgmonitor.cli.commands` entry-point（provider 不经过 `PluginHost`，也不读 manifest）。

为什么要独立通道：`build_parser()` 在 `main()` 里、`bootstrap()` 之前执行，而 `activate(ctx)`
需要 bootstrap 后才有的域服务。统一通道会形成「parser 要命令 → 命令要激活 → 激活要 bootstrap →
bootstrap 在 parse 之后」的死结。所以 provider 自带 parser 与 async handler，需要宿主能力时在
`run()` 内部自行用 `cli_context()`。

其它约定：

- entry-point 指向**类**，宿主无参实例化；`name` / `configure_parser` / `run` 是结构化契约；
- 与内置命令（`CLI_SUBCOMMANDS`）重名时内置优先，插件不能劫持；
- 业务失败抛 `CliError`（退出码 1），未捕获的普通异常会冒泡并保留 traceback，便于诊断第三方代码；
- provider 模块零 Qt import —— CLI 进程从入口就不加载 GUI。

### 5.4 schedule：能力名已登记，实现未落地

`PluginManifest.capabilities` 接受 `"schedule"`，`KNOWN_CAPABILITIES` 也包含它，但**宿主目前没有
统一任务表与 cancel 通道**。插件若声明它，只能自己起后台任务，而宿主在 `shutdown()` 时不会代为
取消 —— 这正是 ADR-001 要避免的「野任务」。

因此：**不要声明 `schedule`，也不要在插件里起无人回收的后台任务**。等宿主任务表落地后再补示例与门禁。

---

## 6. 兼容性、安全与已知限制

- **默认禁用**：必须显式写 `enabled.json` 或在管理页启用；
- **版本门禁**：`min_app_version` 与 `PLUGIN_API_VERSION` 比（朴素三段式，允许 `3.0.0rc1` 这样的后缀），
  宿主更低则该插件 `FAILED` 并跳过；
- **无沙箱**：插件等于可信代码，能读写当前用户能访问的一切。只启用可信来源；
- **代码更新需重启**：Python 模块不可真卸载，热切换只覆盖「已发现插件的启停」，不覆盖代码改动；
  `重新发现` 能发现新目录，但要让新代码真正加载仍需重启；
- **配置不进主 `.env`**：避免污染 `Settings → settings_to_pairs → EditableSettings → SettingsDiff` 链；
- **不要直接调 `subscribe_all`**：它没有对应的 `unsubscribe_all`，会造成幽灵订阅；
- **CLI 零 GUI 依赖 / core 零 Qt**：插件作者如果把 Qt import 放在模块顶层，CI 与运行时守护都会拦；
- **冻结包不能 pip install**：PyInstaller 打包的应用只能用本地目录通道；
- **API 扩展需走架构变更**：`PluginApi` / `PluginContext` 的 allowlist 是有意设置的治理门禁，不是 bug。

---

## 7. 测试与 CI 门禁

示例是**活文档**，不是截图式教程：

```bash
# 只跑插件相关守护（本地复现 CI plugin-contract job）
PYTHONPATH=src QT_QPA_PLATFORM=offscreen uv run pytest -v --tb=short \
  tests/test_plugin*.py tests/test_app_service_surface.py \
  tests/test_architecture.py tests/test_cli.py
```

| 守护测试 | 保证什么 |
|---|---|
| `test_plugin_examples.py` | 把 `examples/` 复制到临时目录，用**真实发现器 + 宿主**加载执行；子进程断言示例顶层零 Qt |
| `test_plugin_error_scenarios.py` | 激活失败后无幽灵订阅 / 幽灵 exporter、畸形发现输入隔离、热写失败不拖垮主流程 |
| `test_plugin_api_surface.py` | `PluginApi` 方法集合与 `PluginContext` 字段集合不腐烂 |
| `test_plugin_lifecycle_hooks.py` | `start/stop` 与 `pause/resume` 联动、幂等、失败隔离 |
| `test_plugin_cli.py` / `test_plugin_ui.py` / `test_plugin_hotswap.py` / `test_plugin_manager_ui.py` | CLI / UI / 管理页各扩展点契约 |
| `test_app_service_surface.py` | 宿主不进 `AppService` 公开表面 |
| `test_architecture.py` | `core/` 与 `plugins/` 零 Qt，`cli/` 零 UI |

CI 的 `plugin-contract` job 单独跑这组测试并解析 junit：用例数非零、关键守护用例确实执行过、
零 skip。任何一项不满足就让 PR 变红，避免「测试文件被删 / 改名 → job 空转全绿」。

完整架构决策与改动理由见 [`ADR-001-plugin-system.md`](ADR-001-plugin-system.md)；
最小示例见 [`../examples/README.md`](../examples/README.md)。
