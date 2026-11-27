# ADR-002：`AppService` 生命周期门面与启动序列收敛（P1-9）

Status: Accepted（范围经维护者 2026-11-26 确认；决策 3 于 2026-11-27 经维护者确认细化）
关联：`CODE-REVIEW-2.1.7.md` P1-9；`ADR-001-plugin-system.md` 阶段 3 前置

## 背景

2.1.7 审核 P1-9 指出三件事：

1. `AppService.start_monitor()` 是空壳（只翻 `_running`），`subscribe_updates()` 生产代码零调用；
2. GUI（`app.py:_background_startup`）、`cli/monitor.py`、`cli/sync.py` 各自手写"白名单 → monitor → bootstrap"，名义上 UI 只依赖 `AppService`，实际启动顺序由三个调用方各自保证；
3. `reconfigure` 阶段 2 边构建边赋值，中途抛错会留下半完成态（monitor 已切新后端、app 引用未切或 settings 未提交）。

ADR-001 把修好 P1-9 定为阶段 3「插件 start/stop 钩子」的前置：钩子点不稳，不能先接。

## 现状取证（2026-11-26）

三处启动序列**步骤相同，顺序与策略不同**：

| 调用方 | 顺序 | 白名单来源 | paused 策略 | bootstrap 非 ready | 进度反馈 |
|---|---|---|---|---|---|
| `app.py:_background_startup` | 白名单 → monitor → bootstrap | storage | 跳过 monitor + bootstrap | 发 `ErrorOccurred`，继续运行 | `status_bar.show_activity` ≥3 处 |
| `cli/monitor.py` | bootstrap → 白名单 → monitor | storage | 返回 1，除非 `--no-resume` | 返回 1 | stderr print |
| `cli/sync.py` | bootstrap → 白名单 → sync | `args.channel_ids` | 不检查 | 返回 1 | stderr 事件订阅 |

死代码引用面（全仓 grep 确认）：

- `subscribe_updates()`：仅 `test_app_service_facade.py` 一个测试直接调用 + surface allowlist；生产零调用。
- `start_monitor()`：仅 `test_app_service.py:129` 的 batch fixture 间接调用；生产零调用。
- `_running`、`_update_streams`：只被上述两个空壳读写，无外部读者。
- `_reconfiguring`：只在 `_reconfigure.py` 写、构造函数初始化，**无人读取**。
- `bootstrap()`：三处调用方之外还有 `cli/_common.py:149`（CLI 上下文就绪检查），**必须保留公开**。

守护测试：`tests/test_app_startup.py` 用 7 个源码正则断言锁死 `_background_startup` 的具体步骤与顺序；`tests/test_app_service_surface.py` allowlist 锁死 11 个公开方法。

## 决策（范围已确认）

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 启动序列收敛力度 | **完整收敛**：统一顺序 + 三处调用方改造 + 删空壳 + 重写守护测试 |
| 2 | reconfigure 原子化深度 | **准原子**：三段式重排，不改 monitor API |
| 3 | `_reconfiguring` | **实现并发守卫**：拒绝重入（不排队）+ 专用异常 `ReconfigureInProgressError` + UI 文案分支 |
| 4 | PR 拆分 | **两个独立 PR**：A 启动序列、B 原子化 |

## 详细设计

### 决策 1：`AppService.start(options) → StartResult`

```python
@dataclass(frozen=True)
class StartOptions:
    whitelist: Sequence[int] | None = None
    # None = 从 storage.list_monitored_channels() 加载；显式序列 = CLI 指定
    start_monitor: bool = True
    # False = 只做白名单 + bootstrap，不启实时流（CLI sync）
    respect_paused: bool = True
    # True = is_paused 时跳过 monitor + bootstrap（GUI）；False = 忽略暂停（CLI --no-resume）
    on_step: Callable[[str], None] | None = None
    # 进度回调，步骤名 whitelist / monitor / bootstrap / paused；GUI 转 status_bar

@dataclass(frozen=True)
class StartResult:
    state: str               # "ready" | "error" | "paused"（+ client 的其它终态）
    detail: str | None
    whitelist_count: int
    monitor_started: bool
```

固定顺序：**白名单 → monitor → bootstrap**。理由：monitor 在 Telegram 会话连接前就绪，避免白名单未建好就开始收消息。CLI 原顺序是 bootstrap 先，改后 bootstrap 失败时 monitor 曾启动过——由调用方 finally 的 `shutdown()` 清理（幂等）。

三处调用方只保留**策略代码**：

- GUI：`result = await app_svc.start(StartOptions(on_step=...))`；`state == "paused"` 时显示提示并 return；保留 `ErrorOccurred` 发布与状态栏反馈。
- CLI monitor：`start(StartOptions(respect_paused=not args.no_resume))`；按 `state` 决定退出码与 stderr 提示。
- CLI sync：`start(StartOptions(whitelist=args.channel_ids, start_monitor=False, respect_paused=False))`；仍按 `state` 决定退出码。

**空壳清理**：删除 `subscribe_updates()`、`_update_streams`、`start_monitor()`、`_running`，以及 `_lifecycle.start_monitor()`；`stop_monitor()` 简化为只翻状态（若 `_running` 一并删除则改为幂等 no-op 或同步删除——以 surface 守护最终形态为准）。surface allowlist 从 11 方法变为 10（−2 +1：`start`）。`bootstrap()` 保留。

**守护测试改法**：`test_app_startup.py` 的 7 个源码断言改为验证「调用 `app_svc.start(...)` + 传入正确 options + 保留状态栏/错误处理」，不再断言具体步骤顺序（顺序由 `start()` 单测守护）；`test_app_service_facade.py` 删 `test_facade_subscribe_updates_proxies_client`；`test_app_service.py:129` 改调新 API。

### 决策 2：reconfigure 阶段 2 三段式（准原子）

1. **预备**：阶段 1 已构建好新 storage / objects；在此构建新 `ChannelSyncService` 与新域服务，用局部变量持有新后端，不碰任何运行时引用。
2. **提交**：调 `monitor.update_backends(new_storage, new_objects, new_settings)`（`WhitelistManager.update_backends` 先纯赋值再容错重载白名单，几乎不抛），随后一次性把新引用赋给 `app`（storage / objects / downloader / channel_sync / 域服务 / settings）。提交段不做可能抛错的构造。
3. **收尾**：publish `SettingsChanged`，close 旧 storage / objects；失败仅 log，不影响已完成的切换。

严格原子（保证任意抛错点零半完成态）需改 monitor API 把 `MediaDownloader` 构造提前到预备段，本次不做。

### 决策 3：`_reconfiguring` 并发守卫（拒绝重入）

**决策**：`reconfigure()` 开头检查 `_reconfiguring`，已在重载中则抛专用异常 `ReconfigureInProgressError`，`finally` 复位。**拒绝而非排队**——配置保存是低频操作，重试成本极低；排队会累积多次后端重建，且用户在排队期间继续改表单会让 `.env` 写入顺序与后端切换顺序错开，更难诊断。

**为什么不是「直接删除标志」**——两种方案的失败模式不对称，这不是实现成本问题：

1. **竞态真实可达**：`settings_page.py` 把 `btn_apply.clicked` 直接连 `_on_apply`，无 `setEnabled(False)`、无防抖；`_on_apply` 每次经 `run_coro` 起协程。两段式提交的预备段要 `await` 新 storage 的 `connect + init_schema`、新 objectstore 的 `connect`——`asyncio` 单线程里多个协程仍会在 `await` 点交错，第二次点击可进入。
2. **后果是静默损坏而非报错**：交错路径下，A 提交 `app.storage ← SA` 后，B 的阶段 2 读到 `old_storage = SA`，提交 `SB` 后 close 掉 A 刚建的新后端。中间窗口内 monitor 的域服务引用与 `app.storage` 指向不同实例，实时消息可能落进即将被关闭的后端；`app.settings` 与 `.env` 写入也各自独立。三者一致性完全取决于时序，且不抛异常——用户只看到"消息偶尔丢失""配置没生效"，极难定位。
3. **PR-B 会放大窗口**：三段式把预备段独立出来，异步构建时间更长、可中断点更多。一边延长竞态窗口、一边拆掉唯一安全网，等于确定性提高故障概率。这两件事必须同期做。

`_reconfiguring` 是 P1-3「两段式提交」不变量的运行时表达：单次原子 ≠ 多次串行，提交段必须独占。删掉它等于宣布这个前提不再保证。守卫最坏是用户看到一次保存失败再点一次；无守卫最坏是 `app` 持有已关闭 storage，只能重启恢复。低频管理操作应优先保证"绝不进入不可恢复状态"。

**实施细节**：

- 专用异常 `ReconfigureInProgressError` 定义在 `core/app_service/`（不放 `cli/`），返回码/文案由各调用方决定。
- UI `_apply_failed` 目前把非 `OSError` 异常一律显示"后端配置未通过校验，已放弃保存"——守卫异常若走通用分支会误导用户去查数据库连接。须加专用分支显示"配置正在应用中，请稍后再试"。
- core 守卫与 UI 禁用「应用」按钮是**纵深防御，不互相替代**：按钮禁用给即时反馈，core 守卫防 CLI 入口、测试直接调用及未来任何绕过 UI 的调用方。
- 端点语义：重入失败发生在任何副作用之前（预备段构建之前），无需回滚。

## 影响面

**源码**：`core/app_service/{app_service,_lifecycle,_reconfigure}.py`、`app.py`、`cli/{monitor,sync}.py`、`tests/test_app_service_surface.py`；PR-B 另加 `ui/widgets/settings_page.py`（重入异常文案分支）。

**测试**：`test_app_startup.py`（7 个重写）、`test_app_service_facade.py`（删 1）、`test_app_service.py`（fixture 改调）、新增 `start()` 参数化单测、`test_reconfigure.py` 新增三段式/失败回滚/重入拒绝用例。

**调用方零改**：`core/runtime.py` 的 4 元组签名、`cli/_common.py` 的 `bootstrap()` 检查、`AppService` 导出/域服务表面均不动。

## 明确不做

- `cli/_common.py` 与各子命令的**双重 bootstrap**（cli_context 检查一次、子命令自己又调一次）属既有技术债，不在 P1-9 范围，另行记录。
- 严格原子 reconfigure（改 monitor API 提前构造 downloader）。
- 插件 start/stop 钩子接入——留给 ADR-001 阶段 3，本 ADR 只提供稳定钩子点。

## 落地顺序

- **PR-A（阶段 3a）**：`start()` 参数化 + 三处调用方改造 + 空壳清理 + 守护测试重写。
- **PR-B（阶段 3b）**：reconfigure 三段式 + 并发守卫 + 失败回滚测试。

每个 PR 独立走全套验证（pytest / ruff / mypy / CI 7 job）后 squash merge。
