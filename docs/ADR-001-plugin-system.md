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
    def activate(self, ctx: PluginContext) -> None: ...
    def deactivate(self) -> None: ...

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
| `cli` | 新增 `CliCommandProvider` 注册表，`build_parser()` 合并 | 改 `tests/test_cli.py` 相等断言为「内置模块相等 + 插件命令来自 entry point」 |
| `ui` | `UiContribution`（page factory + nav item + icon） | `VerticalNavBar` 加 `add_item`；`UiAssembler.build_central` 遍历注入；顺手补上 `assemble()` 的 `NotImplementedError` |
| `schedule` | host 内统一任务表 | shutdown 时统一 cancel，不允许插件自生野任务 |

UI 插件代码只在 GUI 进程加载；CLI 路径不得 import 任何 Qt 模块，保持
`tests/test_cli.py` 的零 GUI 依赖契约。`cli/` 与 `core/` 下不得新增插件实现文件。

### 6. 生命周期与失败隔离

```
runtime.bootstrap() 装配完成
  → 发现 + 读 enabled.json + 版本门禁
  → activate(ctx)  ── 异常 → 标记 FAILED + 日志，不阻断启动
  → 运行期：事件订阅 / 导出格式 / CLI 命令 / UI 页面 / 定时任务
  → runtime.shutdown()：先 deactivate（退订 + cancel 任务），再 monitor.stop()，最后 app.shutdown()
```

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
   P1-9 未修时钩子点不稳，不能先接。
4. **CLI / UI 能力**：注册扩展点 + 两处守护测试放宽 + 导航注册 API +「插件管理」页（启用/停用/配置）。
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
