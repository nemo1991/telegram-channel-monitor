# 2.1.3 重构计划 — 代码规范 / 架构 / 设计模式 / 测试体系全面审核

> 状态：**P0 / P1 / P2 / P4 全部完成，P3 经复审决定不执行**（2026-11-25，PR #148–#157）
> 基线：`main` @ `e847b58`（tag `v2.1.2`，审核时）
> 审核日期：2026-11-25
> 方法：三路并行只读审查（架构与设计模式 / 规范与复杂度 / 测试体系）+ 主线程对高严重度发现的逐条源码复核
>
> **实施进度更新（全部完成，截至 PR #157）**：
> - **P0 已完成**（#148）：C2 修热重载活动写旧库；C1 清理死代码。
> - **P1 全部完成**（#148–#151）：N2 序列化 codec 去重、N3 Local/Folder FS 基类、N6 断连 helper、C3 ObjectStore 能力接口、E/M2 Exporter 元数据驱动 + 文档 ABC 7→8 对齐。
> - **P2 全部完成**（#152–#157）：M6 解循环、M3 域服务读白名单、M7 导出 DTO、N5a Telethon handler 抽 module、N5b Lightbox 控制条抽 module、N1 SQLite 按域拆 7 个 `_xxx_sqlite.py`（#155 第一步 + #156 余下）、M4 MonitorService 组件去宿主反向引用（引入 `MonitorRuntime`，#157）。
> - **P4 已完成**（#148）：ruff 开 `BLE` + `C90`（上限 15）；`ANN` 实测 94 处后暂缓。
> - **P3 不执行**（复审结论）：表 A 候选（79 函数）setup/断言异质，机械参数化只减行数不减 case 数/运行时间且损失可读性；表 B/C 需用户逐条确认。守护/安全/parity 测试全部保留。
> - 最终验证基线（main @ `607cfb7` 全量复跑）：**1772 passed / 11 skipped / 63 deselected**；mypy 166 files；ruff check + format 全绿；各 PR CI 8/8 全绿。（1772 = PR #157 验证时 1769 + M4 新增 3 个解耦守护测试，该 3 测试当时只跑专项未计入那次全套。）
>
> **方案调整记录**（相对本计划原文，均经用户拍板）：
> - **N1**：原写"拆 mixin"，实施改为**对齐 PG 端的 free-function 模块 + facade 薄委托**（项目已弃用 mixin）；AST 证明 48 方法体零行为漂移。
> - **M4**：原写"组件接收最小依赖"，实施为引入共享 `MonitorRuntime` + 组件只持 runtime + 宿主 property 转发 + runtime 晚绑定回调槽保 seam；AST 证明 22 方法体等价，新增 `test_monitor_runtime_decoupling.py` 守护。
> - 下文第 6 节各阶段描述保留原计划原文，作为后续执行依据。

---

## 0. 执行摘要（结论先行）

| 审核维度 | 结论 | 一句话判断 |
|---|---|---|
| 架构设计 | **主干真实落地，局部契约失守** | 分层边界、组合根、四套抽象都不是摆设；问题在"声明与实现脱节"，不在"缺模式" |
| 设计模式 | **够用且克制，无堆砌** | 14 类模式在用，多数解决真实变化点；只有 2 处属"为模式而模式"（ISP 空壳、模板方法复制） |
| 代码规范 | **中上，风格严 / 结构松** | ruff 门禁有效、async 任务全受控、无可变默认参数；但类型系统被 mypy 配置架空，8 个 575~1461 行大文件无复杂度门禁 |
| 单元测试 | **不"过度"，但结构性冗余** | 测试 37489 行 > 源码 33709 行有正当理由（GUI + 三后端 parity + 历史回归）；**不能精简"大部分"**，安全空间仅约 5% |

**最重要的一句话**：这个项目的核心风险不是"写得不好"，而是**三处契约失守**（事件字段漂移、热重载闭包持有旧后端、可插拔点被 `isinstance` 击穿）和**双实现重复**（SQLite/PG 序列化逻辑逐字双写）。重构应优先修契约与去重，而不是大拆文件、更不是删测试。

**改变主意的证据**（审查中修正的初始判断）：
- 事件字段漂移（H1）代码确凿，但 GUI 全量同步按钮已于 v2.0.5 删除，`ui/` 目录对 `channel_sync` **只有注释、无实际调用** → 该链路当前是死代码，**从"线上 Bug"降级为"死代码 + 契约漂移证据"**。
- 测试规模超过源码看似"过度"，但 111 处 mock 断言集中在 5 个控制器测试，7 个最大测试文件的 mock 调用断言数为 **0** → "测试过度"的直觉不成立，问题是合并不足而非泛滥。

---

## 1. 事实基线（全部实测）

```text
仓库/分支：telegram-channel-monitor / main @ e847b58 (v2.1.2)，工作树有 4 个未跟踪历史 PRD（非本次产出，不动）
Python/环境：~=3.13.0 / uv + hatchling / .venv 已就绪
框架：PySide6(<6.13) + qasync + Telethon 1.46 + pydantic-settings 2.x
数据库/存储：SQLite(aiosqlite,默认 WAL) + PostgreSQL(asyncpg,可选)；ObjectStore = Local/Folder/S3
源码规模：152 个 .py / 33,709 行
测试规模：159 个 .py / 37,489 行；1,567 个 test_ 函数（851 同步 + 716 异步）
测试基线：约 1748 passed / 11 skipped / 64 deselected（-m 'not integration'）
校验命令：uv sync --all-extras --group dev --frozen → pytest / mypy src/tgmonitor / ruff check / ruff format --check src tests
门禁现状：ruff select=E,F,I,B,UP,N,SIM,ASYNC（无 ANN/D/C90/PLR/BLE）；mypy 放低（disallow_untyped_defs=false 等）；13 个第三方依赖 ignore_missing_imports
```

**架构边界实测**：
- `core/` 零 PySide6 / qasync import（命中均为注释）✅
- Telethon 类型不出 `core/telegram/`，出口为 `map_message()` ✅
- UI / CLI 无 ORM / Telethon import ✅
- GUI 与 CLI 共享 `core.runtime.bootstrap()/shutdown()` 组合根 ✅
- `AppService` 公开表面由 `tests/test_app_service_surface.py` allowlist 守护 ✅

---

## 2. 架构与设计模式审核

### 2.1 现有模式逐项评价（是否"用得合适"）

| 模式 | 位置证据 | 判断 | 说明 |
|---|---|---|---|
| Repository + Factory | `core/storage/repository.py:35-557`、`storage/factory.py:16-26` | ✅ 合适 | 两套后端是真实变化点；但 ISP 未落地（见 2.3） |
| Adapter | `core/telegram/telethon_messages.py:408` `map_message` | ✅ 合适 | 本项目最干净的一层，Telethon 类型被彻底挡在边界内 |
| Strategy + Registry + Decorator | `core/export/base.py:53-89`、6 个 `@exporter` | ⚠️ 部分合适 | 分发是真变化点，但注册表未成为 UI/CLI 唯一真源（见 2.3） |
| Null Object | `core/telegram/unconfigured.py:59-198` | ✅ 合适 | 凭据缺失时 GUI 照常启动，避免假 ready |
| Observer / EventBus | `core/events.py:446-533` | ⚠️ 合适但脆弱 | 串行 await + 全量吞异常让契约漂移不可见（见 2.2） |
| DI / 组合根 | `core/runtime.py:29-200`、`app_service/app_service.py:72-123` | ✅ 合适 | CLI/GUI 共享装配属实，allowlist 守护有效 |
| Facade | `AppService` `app_service.py:59-260` | ✅ 合适 | 只留生命周期/热重载/导出；`MediaService` 薄壳另议 |
| Command / 注册表分发 | `cli/main.py` 的 `CLI_SUBCOMMANDS` + `_DISPATCH` | ✅ 务实 | 没为凑 Command 对象造类，argparse + callable 表足够 |
| Template Method | `core/export/service.py:122/226/302` | ❌ 不理想 | 三段复制的模板，而非抽象出的模板（见 5.D） |
| Mixin | UI 侧 `monitor_vm.py:41`、`message_list_model.py:155` | ⚠️ 可接受有债 | core 已移除 mixin；UI 侧跨 mixin 读私有字段，MRO 敏感 |
| Decorator | `_media_os_actions.py:55-80` `@_guard_inflight` | ✅ 合适 | 真实解决并发重复下载同一 object_key |
| Callback Bridge | `core/media_service/_qt_bridge.py:35-64` | ✅ 合适 | core 零 Qt import 的关键机制；风险是模块级全局单例 |
| 显式组合（替代 mixin） | `core/monitor/service.py:131-136` + 5 组件 | ⚠️ 形式达成 | 用"双向引用 + 宿主 delegate"换掉了 mixin，状态所有权名义集中、实际分散 |

**结论**：14 类模式多数解决真实变化点，没有 GoF 名词堆砌问题。不需要"引入更多模式"来救架构，需要的是**修好已引入模式的契约**。

### 2.2 三处契约失守（高优先级，已源码复核）

#### C1. 事件字段漂移 + 异常吞噬（已复核，当前影响低、未来风险高）
- **位置**：`ui/controllers/sync_controller.py:41,50-52` 读 `e.done` / `e.new_messages` / `e.failures`；但 `core/events.py:382-386` 的 `ChannelSyncProgress` 字段是 `progress/total/detail/stage`，`ChannelSyncDone:407-410` 只有 `result`。三者**都不存在**。
- **放大因素**：`core/events.py:483-492` 对每个订阅者 `try/except Exception` 吞异常 → `AttributeError` 只进日志。
- **复核修正**：`ui/` 全目录对 `channel_sync` 只有注释（v2.0.5 已删桌面端同步按钮），当前 GUI 不消费这两个事件 → **不是正在发生的线上 Bug**。同一事件在 `core/activity.py:93-103` 被正确消费（读 `e.result.total_messages_added`），证明是 UI 侧残留。
- **建议**：清理死代码 `SyncController` 的两个 handler + 对应 VM signal/QSS 连线，或对齐字段并加契约测试；二选一，不要留着漂移代码。

#### C2. 热重载后活动持久化仍写旧库（已复核，真 Bug）
- **位置**：`core/runtime.py:126` `bus.subscribe_all(make_persistence_subscriber(storage))` 在 bootstrap 时**闭包捕获具体 storage 实例**；`core/app_service/_reconfigure.py:199-258` 只替换各域服务的 storage，从不重注册订阅者。
- **后果**：切到 PG 后活动记录仍写旧 SQLite；`core/activity.py:205-212` 把失败吞成 warning → 完全静默。
- **`EventBus` 无 `unsubscribe_all`**：`core/events.py` 只有 `unsubscribe()`（:465），且全代码库无调用点 → 订阅者无法成批替换。
- **建议**：订阅者改绑 `Callable[[], StorageRepository]`（惰性取 `app.storage`），或补齐 `unsubscribe_all` 并在 reconfigure 中重注册。配套回归测试。

#### C3. ObjectStore 可插拔点被 `isinstance` 击穿（已复核）
- **位置**：`core/media_service/_media_os_actions.py:179-220,240-259,312-324` 三处 `isinstance(ms._objects, (LocalObjectStore, FolderObjectStore))` / `S3ObjectStore`，并直接调具体类的私有 `_path()`；未匹配返回"不支持的对象存储后端"（:217-220）。
- **后果**：新增第 4 种后端在"打开/定位/复制路径"三条路径静默不可用，与 `docs/ARCHITECTURE.md:306` 的扩展承诺相悖。
- **建议**：在 `ObjectStore` ABC 增加能力方法 `local_path(key) -> Path | None`（默认 None）与 `uri(key) -> str`，用能力探测替代类型判断。

### 2.3 声明与实现不一致（中优先级）

- **M1 ISP 空壳**：`core/storage/repository.py` 定义了 **8 个** ABC（预算/审查实测：`SchemaLifecycle/ChannelStore/MetaStore/MessageStore/MediaStore/ThumbnailStore/UserMetaStore/ActivityStore`），但所有领域服务仍注入完整 `StorageRepository`（`channel_service.py:53`、`message_service.py:37` 等）→ 接口隔离只停留在类型声明，无任何实际收益。文档还写"7 个 ABC"（`docs/ARCHITECTURE.md:166`、`AGENTS.md:114`），与实际 8 个不符。
  - **决策点**：要么让服务签名改为所需最小接口（真实收益：测试桩与后端切换的最小契约），要么承认单接口并删除 8 段拆分叙述。不要维持现状。
- **M2 Exporter 注册表非唯一真源**：`core/export/base.py:53-89` 的注册表是真扩展点，但 GUI 下拉由 `ExportFormat` 枚举驱动（`ui/widgets/export_dialog.py:117`），格式名字典缺 `MEDIA_CSV`（:27-33）；CLI 硬编码 `choices=["json","csv","markdown","html","zip"]`（`cli/main.py:235`）。`docs/ARCHITECTURE.md:202,307` 的"新增格式 = 加一个文件，UI 自动出现"不成立。
- **M3 UI 直依赖 core 内部 `MonitorService`**：`ui/controllers/_base.py:37,67`（`MainWindowCtx.monitor`）、`ui/main_window.py:635`、`ui/controllers/export_controller.py:33` 越过域服务直取 monitor 状态，与 `AGENTS.md` 的分层约束矛盾。
- **M4 MonitorService 显式组合仍是双向耦合**：5 个组件全部反向持有 `svc: MonitorService` 并读写宿主私有状态（如 `core/monitor/backfill.py:57` 有 28 处 `self._svc.`），用双向引用换掉了 mixin，状态所有权名义集中、实际分散。
- **M5 历史拉取职责重叠**：`core/monitor/backfill.py:79-95`（周期补拉）与 `core/channel_sync/service.py:313-371`（手动同步）重复 `get_max_telegram_msg_id → iter_chat_history → save_message → 下载` 整条链，语义漂移风险高。
- **M6 循环依赖**：`ui/widgets/message_item_delegate.py:48` ↔ `ui/widgets/message_view.py:229` 为几个 Role 枚举反向导入，应改为直接 import `message_list_model`。
- **M7 DTO purity 缺口**：`core/export/service.py:293-300` 给 DTO 动态塞 `_media_idx`，`media_list_csv_exporter.py:93` 用 `getattr(-1)` 读；`core/events.py:86-91` 事件携带后端绝对路径；`ChannelSyncDone.result: object` 占位迫使消费方无类型可依。

---

## 3. 代码规范与复杂度审核

**总体评价：中上，高于同类规模项目。短板是"风格严、结构松"。**

正面（不要动）：
- ruff 全规则集 CI 门控 + format 一致；142 处 `noqa: BLE001` 是知情抑制
- 151 处 `except Exception` 中 142 处带 noqa；9 处 `except BaseException` 全部用于 cleanup-then-reraise，语义正确；0 处裸 except、0 处 `except Exception: pass`
- 10 处 create_task/ensure_future **全部有引用或异常收集**（`core/events.py:504` 用 `_inflight` 集合 + done_callback；`orchestrator.py` 赋值持有；`app.py:295` 目标协程内部全量捕获）
- 0 处可变默认参数（ruff B006 生效）；132 处 `with`/`async with`；无同步 DB 驱动滥用（唯一 `aiosqlite.connect`）

需处理（按优先级）：

| # | 问题 | 证据 | 建议 |
|---|---|---|---|
| N1 | `sqlite_repo.py` 1461 行，8 类职责混杂 | `repository.py:35-543` 已定义 8 个 ABC；PG 端已有 `_messages_pg.py`/`/_media_pg.py`/`/_lifecycle_pg.py` 拆分先例 | 按 ABC 边界拆为 mixin，复用 PG 端既有结构；单一最大可维护性债务 |
| N2 | SQLite/PG 序列化逻辑逐字重复 | `sqlite_repo.py:309-327` 与 `_media_pg.py:75-92`（`_media_status`/`_status_to_db` 完全一致，含 `'pending'` 兼容）；排序列映射 :273-277 与 :68-72 相同 | 提取到共享 `_media_codec.py`，一次修改避免两端语义漂移 |
| N3 | 两个 ObjectStore 后端 5 个方法逐字重复 | `local_store.py:93/101/107/114/127` 与 `folder_store.py:100/108/114/121/135` 函数体相同 | 提取共享基类/mixin |
| N4 | core 边界 `Any` 扩散 | 全库 98 处 Any，core 66 处；`telethon_client.py:141/187/537-569` 项目自有 `Event` 基类却标 `Any` | 收窄到 `Event`/DTO；DB row 映射处的 Any 合理，保留 |
| N5 | `telethon_client.py` 1057 行 / `lightbox_dialog.py` 992 行 | 前者混认证状态机+事件 handler+下载；后者混渲染+资源释放+控制条 | 前者抽 `_event_handlers.py`(537-691)；后者抽控制条 widget(734-954) |
| N6 | Qt 信号安全断连重复 4 处 | `batch_progress_dialog.py:230`、`export_progress_dialog.py:113/126`、`media_actions_controller.py:210` | 提取 `_disconnect_quietly()` |
| N7 | 无复杂度门禁 | pyproject 未选 C90/PLR，无 ANN | 见 P4，渐进引入，不一步到位 |

---

## 4. 单元测试体系审核 —— "能否精简大部分"的明确回答

### 4.1 结论：不能。安全空间约 5%，且大部分"精简"不减少运行时间

用户问题："单元测试代码是否过度，是否可以精简大部分单元测试等情况。"

**明确回答：否，不建议精简大部分。** 理由（实测）：

1. **测试 > 源码有正当理由**：GUI 应用（大量几何/信号/布局回归）+ 三后端 parity（InMemory/SQLite/PG）+ 项目历史上大量真实 bug 回归（命名正名、列迁移、emoji、DPI 对齐、64bit ID、CSV/MD/ZIP 注入）。这些是资产，删掉等于撤销历史修复。
2. **"过度"的直觉位置错了**：111 处 mock 调用断言集中在 5 个控制器测试（`test_app_state_controller.py` 34 处、`test_message_stream_controller.py` 21 处、`test_lifecycle_controller.py` 18 处等）；而 7 个最大测试文件的 mock 断言数为 **0**（它们验证真实语义）。
3. **最大的"冗余"是参数化不足，不是测试泛滥**：约 79 个函数属于明显参数化候选（`test_message_view.py` 27 + `test_media_manager.py` 35 + `test_storage_backends.py` 17），可合并为约 16 个参数化函数。
   - **关键警示**：参数化合并只减少定义行数（约 1500-1800 行），**pytest 收集的 case 数不变 → 1748 passed 基本不变，运行时间不降**。这是"消除重复维护"，不是"精简测试"。
   - 必须逐个甄别，像 `test_pr9_append_constant_time_per_call`（`test_message_view.py:1327`）这类性能断言语义各异，不可机械按前缀合并。

### 4.2 该删 / 该合并 / 该留

| 类别 | 数量 | 收益 | 风险 |
|---|---|---|---|
| A. 参数化合并 | 79 函数 → ~16 参数化函数，净减 ~63 函数 / ~1500-1800 行 | 消除重复维护；case 数不变 | 低（须排除异质项） |
| B. 控制器重复 mock 交互测试 | ~8-12 函数 / ~200-300 行 | 降低实现耦合 | 中（可能丢状态栏文案契约，只合并重复项，不整文件删） |
| C. 替身自测 `test_in_mem_*` | 6 函数 / ~165 行 | parity 已覆盖 | 中 |
| **合计推荐** | **~77-81 函数 / ~1900-2300 行（测试总量约 5%）** | 显著提升可维护性 | — |

**明确禁止删除（守护测试）**：
- `test_naming_monitored_2006.py`（正名回潮 → 左栏覆盖 bug）
- `test_storage_migration_2006.py::test_legacy_subscribed_column_migrates_with_data`（用真 sqlite3 验证旧库升级数据逐条保留，不可替代）
- `test_no_decorative_emoji.py`（白名单精确到单个字面量）
- `test_ui_2007_hotfix.py::test_two_card_lists_top_aligned`（v2.0.7 两栏 11px 错位；已改为结构性断言，若 flaky 应改写而非删除）
- `test_architecture.py` / `test_app_service_surface.py` / `test_cli.py::test_cli_does_not_import_qt_or_qasync` / `test_qt_signal_64bit_ids_2006.py`
- 全部安全测试（CSV 公式注入、markdown 注入、zip-slip）
- 全部跨后端 parity 测试（双实现 parametrize + `integration/` 63 个）
- `test_telethon_client.py` 的 29 处 AsyncMock（是离线替身，不是脆弱交互断言）

---

## 5. 引入设计模式的建议（只列真实收益，务实清单）

> 原则：修好已引入模式的契约 > 引入新模式。以下 5 项按投入产出比排序。

| 项 | 模式 | 解决什么 | 预期收益 | 风险 |
|---|---|---|---|---|
| A | **Capability Interface**（替代 isinstance） | C3：新增 ObjectStore 后端静默不可用 | 真正实现"新增对象存储零改 core"；删 3 处类型 import + 6 处 isinstance | S3 语义（返回 `s3://` URI）需定义；依赖 `_path()` 的测试迁移 |
| B | **Typed Event + 契约守护** | C1：事件字段 `object` 占位 + 被吞异常 | 消除契约漂移；字段变化时 CI 暴露 | 修 C1 死代码后收益下降，可与清理合并做 |
| C | **Indirection（订阅者绑容器）** | C2：热重载写旧库 | 热重载正确性；`EventBus` 生命周期完整 | 补 `unsubscribe_all` 需防重复注册（按 name/token 注销） |
| D | **抽取原语（谨慎，不做完整 Template Method）** | M5：两处历史拉取重复 | 续拉规则单点定义 | 只抽 `iter_since(...)` async iterator；backfill 重连 kick 与 channel_sync 429 退避语义不同，**不可合并整个流程** |
| E | **Exporter 元数据驱动** | M2：注册表非真源 | 插件点名副其实；消除 `MEDIA_CSV` 漂移 | 给 ABC 加 `extension/needs_object_store/supports_thumbnails`，由 `EXPORTERS.available()` 驱动 GUI/CLI；`MEDIA_CSV` 用不同 DTO，保留显式分支 |

**明确不引入**：状态机框架（当前登录状态机虽在 1057 行文件里但语义清晰，引入外部库得不偿失）、ORM（项目有意选择手写 SQL 保证跨后端语义对齐）、Command 对象体系（argparse + callable 表更简单）、完整 Template Method 统一 backfill/channel_sync（见 D 的风险）。

---

## 6. 分阶段重构路线图

> 每个阶段独立成 PR、独立可验证、独立可回滚。顺序体现"先止血与去重，后拆分与门禁"。
> 遵循项目协作约定：低风险改动自主走完整链路（分支→全套校验→PR→CI→merge）；涉及生产行为/依赖/发版先确认。

### P0 — 契约止血（1 个 PR，低风险，生产行为影响可控）

| 项 | 动作 | 验收 |
|---|---|---|
| C2 | 活动持久化订阅者改绑 `app.storage` 惰性持有者；补 `EventBus.unsubscribe_all`；reconfigure 回归测试（切换后端后活动写新库） | 新增测试在旧代码上失败、新代码通过；`test_reconfigure.py` 全绿 |
| C1 | 清理死代码：删除 `SyncController.on_sync_progress/on_sync_done` + VM 的 sync signal 连线（若已无消费方），或对齐字段（二选一，评审定） | `ui/` 无对不存在字段的访问；相关测试全绿 |

**风险**：C2 改的是热重载路径，需覆盖 SQLite→PG 切换的场景（集成测试默认 deselected，需手动跑一次带 PG 的验证或补纯内存替身测试）。

### P1 — 去重与契约加固（2-3 个 PR，低风险）

| 项 | 动作 | 验收 |
|---|---|---|
| N2 | 提取 `_media_codec.py`，统一 `_media_status`/`_status_to_db`/`_MEDIA_SORT_COLUMN`；两端测试共用 | `test_storage_backends.py` + `test_thumbnail_repo_parity.py` 全绿；`'pending'` 兼容逻辑仍通过 |
| N3 | Local/Folder ObjectStore 提取共享基类 | `test_objectstore.py` 全绿 |
| N6 | 提取 `_disconnect_quietly()` | 相关 dialog/controller 测试全绿 |
| A/C3 | ObjectStore 增加 `local_path`/`uri` 能力方法，替换 3 处 isinstance | 新增"第 4 种假后端"测试证明零改 core 可用 |
| E/M2 | Exporter 元数据驱动 GUI 下拉与 CLI choices | 新增格式只需加一个文件的契约测试 |
| 文档 | 修正 ABC 数量（7→8）、导出格式数量、扩展点描述 | `docs/ARCHITECTURE.md` / `AGENTS.md` 与代码一致 |

### P2 — 结构拆分（每个文件 1 个 PR，中风险，逐一评审）

| 项 | 动作 | 验收 |
|---|---|---|
| N1 | `sqlite_repo.py` 按 8 个 ABC 边界拆 mixin（复用 PG 端 `_messages_pg.py` 等命名与结构） | 存储全量测试 + parity 全绿；分层架构测试通过 |
| N5a | `telethon_client.py` 抽 `_event_handlers.py`(537-691) | `test_telethon_client.py` 全绿 |
| N5b | `lightbox_dialog.py` 抽控制条 widget(734-954) | lightbox 两组测试 + DPI 守卫全绿 |
| M3/G | `ChannelService` 增 `monitored_ids() -> frozenset[int]`，UI 改走 `app.channels`；`MainWindowCtx.monitor` 过渡保留但停止业务调用 | `test_app_service_surface.py` allowlist 不破（方法加在域服务上） |
| M4 | MonitorService 组件改为接收最小依赖（storage/client/bus + 只读配置），去掉对宿主的反向引用 | monitor 相关测试全绿 |
| M6 | 解除 `message_item_delegate.py` ↔ `message_view.py` 循环依赖 | 导入顺序测试通过 |
| M7 | 用 `MediaExportRowDTO` 取代 `_media_idx` 动态属性 | exporter 测试全绿；删除两处 `type: ignore` |

**风险**：N1 是最大单笔改动，必须最后一个拆、且在每个子 PR 后跑全套；M4 双向引用解开涉及状态所有权，需逐组件验证。若 P2 任一 PR 出现回归，立即回滚该 PR，不影响 P0/P1 已落地成果。

### P3 — 测试精简（1 个 PR，低风险，但需评审清单）

> **复审结论（2026-11-25）：本节不执行。** 表 A 的 79 个候选经抽样审查，setup / 断言异质，机械参数化仅减定义行数、不影响 case 数与运行时间，反而损失可读性；表 B/C 涉及守护 / 安全 / parity 测试，须用户逐条确认，不批量删。故 P3 作为"已评估并决定不改动"关闭，测试基线完整保留。以下为原计划原文，留作依据。

- 按 4.2 表 A 做参数化合并（**明确 case 数不变、运行时间不降**，收益是可维护性）；逐文件评审，排除异质测试。
- 表 B/C 需逐条评审后再删，先列清单供用户确认，**不批量删控制器 mock 与守护测试**。
- 验收：1748 passed 基线不降（参数化合并后 case 数应完全相同）；skipped/deselected 不变。

### P4 — 门禁增强（可选，单独 PR，需用户拍板）

- ruff 渐进引入 `ANN`（先 `src/tgmonitor/core/`）、`C90`（阈值放宽，如 max-complexity=15）、`BLE`（配合 noqa 白名单）。
- mypy 逐步收紧：先去掉对**项目自有模块**的 `ignore_missing_imports`，第三方依赖保留。
- **不做**：一次性全量开启（会产生数百处告警，淹没真实问题）。

---

## 7. 明确不做的事（Non-Goals）

1. **不删"大部分"单元测试** —— 尤其守护测试、安全测试、parity 测试（见 4.2 禁止删除清单）。
2. **不引入 ORM / 状态机框架 / 外部 DI 容器** —— 与项目既有取舍冲突（见第 5 节末尾）。
3. **不把 backfill 与 channel_sync 合并成一个模板方法** —— 退避/重连语义不同，只抽底层原语。
4. **不升级任何依赖主版本、不改 pyproject 的 Python 约束** —— 本轮是重构不是升级。
5. **不动工作树中 4 个未跟踪的历史 PRD 文档**（`PRD-2.0.5/6/7/9.md`）。
6. **不建 Release、不推 tag** —— 2.1.3 本轮只出计划；真正发版按项目约定单独走轻量 tag。
7. **不一次性完成 P2 全部拆文件** —— 每个文件独立 PR、独立验证，避免大爆炸合并。

---

## 8. 验收标准与回滚策略

**每个 PR 的统一验收（缺一不可）**：
```bash
uv sync --all-extras --group dev --frozen          # 注意：裸 uv sync --frozen 会卸载 asyncpg/aioboto3
uv run pytest -m 'not integration' --basetemp=/tmp/tgmon-<新路径>   # 全套约 2m20s，后台跑
uv run mypy src/tgmonitor                           # 只跑 src，不跑 tests（546 个既有类型错误）
uv run ruff check src tests
uv tool run --from 'ruff>=0.5' ruff format --check src tests
```
- 基线：约 1748 passed / 11 skipped / 64 deselected；mypy 152 files；CI 8/8。
- 本地已知 flaky（不代表回归）：`test_async_helper.py::test_logs_exception_and_calls_on_error`（100ms 等待，复跑即过）、`test_two_card_lists_top_aligned`（DPI 脆弱，被手动 deselect）。
- 新增/修改的行为必须有回归测试，且**先在旧代码上失败**（缺陷修复的证明）。

**回滚**：
- 每个 PR  squash merge 前保留本地分支；回滚用 `git revert`，不改写历史。
- P0/P1 若引入回归，优先回滚单个 PR 而非连锁修复。
- 拆文件类 PR（P2）回滚粒度 = 单个文件，不影响其他已落地项。

**风险与待确认**：
- C2 的 SQLite→PG 热重载场景，集成测试默认 deselected，需在带 PG 的环境实跑一次或补内存替身等价测试。
- P4 门禁增强可能暴露存量告警，阈值与白名单需用户拍板后执行。
- 本计划中所有行号基于 `e847b58`；实施时若分支已前进，需重新定位。

---

## 附录 A：关键证据索引

| 结论 | 文件:行 |
|---|---|
| 事件字段漂移 | `src/tgmonitor/ui/controllers/sync_controller.py:41,50-52` vs `src/tgmonitor/core/events.py:382-386,407-410` |
| 事件异常吞噬 | `src/tgmonitor/core/events.py:483-492` |
| 活动订阅者闭包捕获 storage | `src/tgmonitor/core/runtime.py:126` |
| reconfigure 未重注册订阅者 | `src/tgmonitor/core/app_service/_reconfigure.py:199-258` |
| 无 unsubscribe_all | `src/tgmonitor/core/events.py:465`（只有单个 unsubscribe） |
| isinstance 击穿可插拔点 | `src/tgmonitor/core/media_service/_media_os_actions.py:179-220,240-259,312-324` |
| 8 个存储 ABC（文档写 7） | `src/tgmonitor/core/storage/repository.py:35,84,191,205,329,428,497,526` |
| SQLite/PG 序列化重复 | `src/tgmonitor/core/storage/sqlite_repo.py:309-327,273-277` vs `_media_pg.py:75-92,68-72` |
| ObjectStore 5 方法重复 | `local_store.py:93,101,107,114,127` vs `folder_store.py:100,108,114,121,135` |
| GUI 同步入口已删（C1 降级依据） | `src/tgmonitor/ui/` 全域 Grep `channel_sync` 仅命中注释：`sync_controller.py:12` |
| 事件被正确消费的对照 | `src/tgmonitor/core/activity.py:93-103` |
| mock 断言集中位置 | `test_app_state_controller.py`(34) / `test_message_stream_controller.py`(21) / `test_lifecycle_controller.py`(18) |
| 参数化候选族 | `test_message_view.py:940-1089,1327-1576`；`test_media_manager.py`；`test_storage_backends.py:734-899` |
