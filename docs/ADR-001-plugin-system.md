# ADR-001: 3.0.0 进程内能力插件系统

## 状态

**Accepted**（2026-11-26）。设计已定，实现尚未开始；落地进度以各阶段 PR 与 `CHANGELOG.md` 为准，
本文件只记录决策本身与理由，不随实现进度改写。适用于 **3.0.0**。

## 背景

2.1.7 已有四类可插拔机制，但都不是「插件系统」：

| 现有机制 | 注册时机 | 发现方式 | 缺口 |
|---|---|---|---|
| `core/export/base.py` 的 `ExporterRegistry` | import 时装饰器注册 | 静态 `ExportFormat` 枚举 + 注册表 | 新增格式必须改枚举；无启停、无配置 |
| `core/storage/factory.py` / `core/objectstore/factory.py` | 运行时工厂 | 静态枚举 + if/elif | 第三方无法加入实现 |
| `core/telegram/client.py` 的 `TelegramClient` Protocol | 运行时工厂 | 凭据存在性分派 | 无第三方实现入口 |
| `core/events.py` 的 `EventBus` | 运行时订阅 | 按事件类 | 可观察，但无身份、无生命周期、无权限边界 |

同时存在六条硬约束，决定了插件不能「随便挂进去」：

1. `tests/test_app_service_surface.py` 用 allowlist 锁死 `AppService` 公开表面，不能加插件入口；
2. `tests/test_architecture.py` 对 `core/` 做 AST 扫描，任何位置（含延迟 import）都禁止 PySide6 / qasync；
3. `tests/test_cli.py` 断言磁盘 `cli/*.py == _DISPATCH 模块 + {_common, main}`，外部模块不能零改注入命令；
4. `ui/nav_bar.py` 的 `_NAV_LABELS_SRC` / `_NAV_ICONS` 是硬编码 tuple 且 `strict=True` zip，无注册 API；
5. 桌面包走 PyInstaller（`tgmonitor.spec` 显式 `datas`），不能假设运行时可以 `pip install`；
6. `EventBus.subscribe_all` 没有 `unsubscribe_all`，退订必须由宿主记账。

## 决策

### 1. 总体形态：进程内能力插件 + 唯一接缝

新增顶层包 `src/tgmonitor/plugins/`（与 `core/`、`cli/`、`ui/` 平级，天然避开 core/cli 的 AST 守护）。
插件是**可信的进程内代码**，经唯一接缝 `PluginHost` 接入；宿主只向插件暴露 `PluginContext`，
绝不交出 `AppService`、Telethon 客户端或 Qt 对象。跨边界载荷一律是 dataclass DTO，
datetime 用 aware UTC（沿用 `core/dto.py` 约定）。

不做沙箱。进程内换来低摩擦与既有注册表复用，代价是插件等于可信代码；
缓解手段是「发现 ≠ 启用」、manifest 能力声明、窄门面。可逆性由 DTO 契约保证：
未来若要把插件移到子进程，只需把 `PluginContext` 换成 IPC 代理，插件代码基本不用改。

### 2. 双通道发现，发现 ≠ 启用

- **entry-points**：`importlib.metadata.entry_points(group="tgmonitor.plugins")`，覆盖源码 / wheel 安装。
- **本地目录**：`<user_data_dir>/plugins/` 下扫描带 `plugin.toml` 的模块或包，用
  `importlib.util.spec_from_file_location` 加载，覆盖 PyInstaller 场景（用户不能往冻结包里 pip install）。

两条来源统一产出 `PluginManifest`，按 `id` 去重（先发现者优先，冲突记 warning）。
`manifest` 只表示「已发现」，**默认不启用**；只有 `<user_data_dir>/plugins/enabled.json` 显式列出的 id
才在 `bootstrap()` 中激活。这条规则是为了让装了坏插件也能进应用。

`min_app_version` 与宿主版本不兼容的插件跳过并记日志，不阻断启动。

### 3. 窄门面开放写操作

经 `PluginApi` 开放**经过挑选的写操作**，不是 `AppService` 的别名：

```python
class PluginApi(Protocol):
    # 读
    async def list_messages(...) -> list[MessageDTO]: ...
    async def list_media(...) -> list[tuple[MessageDTO, int, MediaDTO]]: ...
    async def list_monitored_channels() -> list[ChannelDTO]: ...
    # 写（有界集合）
    async def set_favorite(cid: int, mid: int, value: bool) -> None: ...
    async def set_tags(cid: int, mid: int, tags: list[str]) -> None: ...
    async def set_notes(cid: int, mid: int, notes: str) -> None: ...
    async def delete_message(cid: int, mid: int) -> None: ...
    async def retry_media(cid: int, mid: int, idx: int) -> None: ...
    async def download_media(cid: int, mid: int, idx: int) -> None: ...
```

底层直接委托给既有域服务（`MessageService` / `MediaService` / `ChannelService`），
写操作仍走它们原有的事件发布与幂等语义，不另开一条数据通路。

`PluginApi` 用类似 `tests/test_app_service_surface.py` 的 allowlist 守护：
新增方法必须显式改测试并说明理由，防止门面腐烂成第二个 god facade。

### 4. SPI 骨架

```python
@dataclass(frozen=True)
class PluginManifest:
    id: str
    name: str
    version: str
    min_app_version: str = "3.0.0"
    capabilities: tuple[str, ...] = ()   # events|export|cli|ui|schedule
    description: str = ""

class Plugin(Protocol):
    manifest: PluginManifest
    # 装配期（runtime.bootstrap / shutdown 驱动）
    def activate(self, ctx: PluginContext) -> None: ...
    def deactivate(self) -> None: ...
    # 运行期（AppService 驱动，阶段 3 新增，可选钩子）
    def start(self) -> None: ...
    def stop(self) -> None: ...

@dataclass(frozen=True)
class PluginContext:
    bus: EventBusView      # 包装 subscribe/publish，deactivate 时统一退订
    api: PluginApi         # 窄写门面
    config: PluginConfig   # 按 plugin id 隔离的 KV，原子写
    settings: SettingsView # 只读快照
    log: Logger
    def notify(self, level: str, title: str, body: str) -> None: ...
```

`notify` 转成既有 `NotificationRequested` 事件，托盘与状态栏兜底订阅者自动呈现，不碰 UI 代码。

### 5. 五类能力全部进 3.0.0

| 能力 | 落点 | 宿主改动 |
|---|---|---|
| `events` | `EventBus.subscribe(event_type, fn)` / `publish` | 零改动；host 记账退订（`subscribe_all` 不可注销） |
| `export` | `ExporterRegistry` | key 从 `ExportFormat` 放宽到 `str`；`ExportFormat` 已是 `str, Enum`，值比较向后兼容；UI 下拉与 CLI choices 已派生自注册表 |
| `cli` | `CliCommandProvider` + 独立 entry-point group `tgmonitor.cli.commands` | `build_parser(cli_providers=...)` 合并；`main()` 发现后路由；内置命令重名时优先 |
| `ui` | `UiContribution`（nav_label / icon_name / page_factory） | `VerticalNavBar.add_item`；`UiAssembler.build_central` 遍历 ACTIVE 插件注入 |
| `schedule` | host 内统一任务表 | shutdown 时统一 cancel，不允许插件自生野任务 |

### CLI / UI 扩展点形态（阶段 4 细化）

**CLI 走独立 entry-point 通道，不进 PluginHost**：`build_parser()` 在 `main()` 里、
`bootstrap()` 之前执行，而插件 `activate(ctx)` 需要 `app_services`（域服务），后者
只有 `cli_context()` bootstrap 之后才存在。若命令走统一 host 通道，会形成
「parser 要命令 → 命令要激活 → 激活要 bootstrap → bootstrap 在 parse 之后」的死结。
因此 `CliCommandProvider`（`name` + `configure_parser` + async `run`）从独立 group
发现，`run(args)` 内部自行 `cli_context()` 拿 app；`build_parser()` 默认零插件
（守护测试 / 冷启动行为不变），`main()` 把发现的 provider 传入。内置命令与插件
重名时内置优先（插件不能劫持）。`tests/test_cli.py` 的内置集合精确断言**保持**
—— 插件命令不进 `CLI_SUBCOMMANDS` / `_DISPATCH`，该断言守护的是内置表面。

**UI 走 host 收集**：`UiContribution` 只含数据 + `Callable[[], QWidget]`，**不持有
QWidget / QIcon**（plugins 包顶层不 import Qt）。插件声明 `ui` 能力 + `ui_contribution`
属性，host 在 `activate()` 成功后收进 `PluginRecord`；`UiAssembler.build_central`
在 GUI 装配期遍历 ACTIVE 插件调 `page_factory()`，`nav.add_item` 与 `stack.addWidget`
同索引成对添加。单个插件 factory 抛错 / 返回非 QWidget 只记日志跳过。CLI 进程
不调 `page_factory`，插件的 Qt import 只发生在 GUI —— 零 GUI 依赖契约不变。
`VerticalNavBar` 内置的 4 项硬断言继续守护「默认导航」，插件项是追加场景，不放宽该断言。

**「插件管理」页**：落在设置页「账户凭证」分组内的一个按钮 + 独立对话框
（`PluginManagerDialog`），**不新增导航项 / 分组** —— 导航 4 项与设置 7 分组
两处硬断言继续守护「默认布局」，管理页是按需打开的覆盖层，不是常驻页面。
对话框构造注入 `PluginHost` 单例，只经公开 API 操作（enable / disable /
records / config_for / save_config / refresh_discovered），不碰启用清单与配置
文件的读写细节。

**启停语义 = 真热切换**（阶段 4 确认）：`enable` / `disable` 立即生效、无需重启。
为此宿主在 `activate_all` 时留三份依赖快照 —— 发现缓存（热启用要重建
`PluginContext`）、bus + 域服务映射、settings 视图；运行期 `enable` 走与启动
**同一条** `_activate_one` 路径（版本门禁 / activate / exporter 注册 / ui 贡献
读取完全一致），再写 `enabled.json` 持久化意图，若宿主已在运行期则立即调该
插件 `start()`；`disable` 逆向拆除（stop → 注销 exporter → 退订 → deactivate
→ 移出启用清单 → 标 DISABLED）。每步失败只记日志，保证停用总能走完。
代码改动仍需重启 —— Python 模块不可真卸载，热切换只覆盖「已发现插件的
启用/停用」，不覆盖插件代码更新。

**配置编辑 = 通用 KV 表格**：`config.json` 无 schema，`{plugin_id: {key: value}}`
的 value 可能是数字 / 布尔 / 数组 / 对象。表格 value 走 `json.loads` 字面量
解析：合法 JSON 按解析值写回（保类型），非法输入当字符串；`save_config` 只覆盖
目标 id 子树，其它插件配置不变。这样无 schema 也不破坏插件期望的值类型。

**UI 页增量同步**：管理对话框热切换后发 `plugins_changed`，MainWindow 接线到
`UiAssembler.refresh_plugin_pages()`。按 `plugin_id` 记账只增删状态变化的页，
不重调未变化插件的 `page_factory()` —— 否则停用 A 会让 B 的页面状态（滚动位置
/ 表单）丢失，不满足「真热切换」。`VerticalNavBar.remove_item(plugin_id)` 精确
移除并同步后续按钮索引；内置 4 项不受影响。

UI 插件代码只在 GUI 进程加载；CLI 路径不得 import 任何 Qt 模块，保持
`tests/test_cli.py` 的零 GUI 依赖契约。`cli/` 与 `core/` 下不得新增插件实现文件。

### 6. 生命周期与失败隔离

生命周期分两层，**装配期**钩子由 runtime 驱动，**运行期**钩子由 AppService 驱动：

```
runtime.bootstrap() 装配完成
  → 发现 + 读 enabled.json + 版本门禁
  → activate(ctx)  ── 异常 → 标记 FAILED + 日志，不阻断启动
  → 装配期能力就位：事件订阅 / 导出格式 / CLI 命令 / UI 页面
  ↓
app.start() 会话就绪(state == "ready")
  → start_all()  ── 运行期工作开始（定时任务 / 消费等）
  ↓
运行期：pause → stop_all()；resume 成功 → start_all()
  ↓
退出：app.shutdown() → stop_all()；runtime.shutdown() → deactivate_all()（内部先 stop_all 兜底）
        → monitor.stop() → app.shutdown() → 关 client / storage / objects
```

**运行期钩子（阶段 3）的决策**：

- `start` / `stop` 是**可选钩子**：阶段 1/2 只实现 `activate`/`deactivate` 的插件视为
  no-op（宿主用 `getattr` 检测，发现阶段不检查），保证老插件零改动可用。
- **触发点**：`start()` 只在 `state == "ready"` 后触发——paused 短路与 bootstrap
  非 ready（error / phone_required）都不 start（会话没连上，「就绪即干活」前提不成立）。
  `pause_monitor()` 成功路径触发 stop，`resume_monitor()` 成功路径触发 start。
- **联动 pause/resume** 而非只用 start/shutdown：原因是 paused 冷启动的插件若只靠
  `app.start()` 触发，会在用户点「继续监听」后永久不启动（`resume_monitor()` 不重走
  `app.start()`）。联动堵住这个缺口。代价是 `start`/`stop` 可能反复调用，故要求插件
  钩子**幂等可重入**，且插件 start 失败只记日志、不拖垮监听态切换与启动。
- 钩子实现在 `PluginHost.start_all()` / `stop_all()`：只对 ACTIVE 插件生效、`_running`
  守卫保证幂等、单插件异常逐隔离。`deactivate_all()` 进入装配拆除前先 `stop_all()`
  兜底，覆盖「未经过 AppService 直接调 runtime.shutdown」的路径。
- 宿主不进 `AppService` 公开表面：接入用 `PLUGIN_HOST` 单例 + lazy import，surface
  allowlist 与 4 元组返回签名都不变。

插件自身异常一律降级：加载/激活失败记 `FAILED`；事件订阅者异常已被 `EventBus` 吞掉；
停用失败只记日志继续。`enable` / `disable` 可热切换（只涉及订阅与能力注册），
但**代码改动必须重启**——Python 模块无法真正卸载。

### 7. 目录与配置

对齐既有平台目录约定（`config.py::_user_data_dir()`，与 `session/`、`media/` 平级）：

```
<user_data_dir>/plugins/
├── enabled.json          # 已启用插件 id 列表
├── config.json           # {plugin_id: {key: value}}，按插件隔离命名空间
└── <local-plugin>/       # 本地插件包，带 plugin.toml
```

写盘沿用 `core/settings_store.py` 的临时文件 + rename + 0600 手法。
新增配置字段若要进主设置页，必须贯通 `Settings → settings_to_pairs → EditableSettings →
SettingsDiff → _what_label → reconfigure` 全链；插件自身配置不进主 `.env`，避免污染既有配置真源。

## 落地阶段

每阶段是一个可独立评审的 PR，顺序不可倒置：

1. **地基**：`plugins/` 骨架（manifest / loader / host / context / config / api）+ 双通道发现 +
   `enabled.json` + `events` 能力 + `PluginApi` allowlist 守护。
2. **导出能力**：`ExporterRegistry` key 放宽到 `str`，插件可提供新导出格式。
3. **生命周期门面**：先完成 2.1.7 审核遗留的 **P1-9**（`AppService.start()` 原子化，
   三处手写启动序列收敛并同步 surface allowlist），再接插件 start / stop 钩子。
   P1-9 未修时钩子点不稳，不能先接。运行期钩子的形态（可选 / 联动 pause/resume /
   幂等）与触发点见决策 6。
4. **CLI / UI 能力**：注册扩展点 + 导航注册 API +「插件管理」页（启用/停用/配置）。
   导航 4 项与设置 7 分组的守护断言**不放宽** —— 插件项/管理页是追加与覆盖层场景。
   管理页的真热切换语义、KV 配置类型保留、UI 增量同步见上文「插件管理页」段。
5. **收尾**：示例插件、用户文档、CI 门禁、错误场景测试。

## 后果

**变容易**：第三方无需改主程序即可响应事件、加导出格式、加命令、加页面；
启用/停用可热切换；插件失败不影响主流程。

**变困难**：多了一套协议与状态机要维护；导航与 CLI 从硬编码改为注册表，守护测试要重写；
`ExportFormat` key 放宽后，任何按枚举顺序或身份比较的调用点都要复查；
插件权限边界依赖 allowlist 守护，守护漏一项就会出现越权通道。

**明确不做（3.1+ 再评估）**：存储 / 对象存储后端的插件化。它们要联动改静态枚举、工厂、
`SettingsDiff` 与 UI 设置页，爆炸半径远大于本 ADR 的收益。

**风险与缓解**：进程内无隔离 → 默认禁用 + manifest 声明 + 窄门面；
`subscribe_all` 无法注销 → host 统一记账，禁止插件直接调用它；
Python 模块不可真卸载 → 明确文档化「代码变更需重启」；
插件命令注入改变 CLI 参数空间 → 保留 `CLI_SUBCOMMANDS` 内置集合的精确断言，只放宽外部来源。
