# PRD — tgmonitor 2.0.4「操作台整合与活动持久化」版本规划

> **历史文档**(点时间快照,2026-11-26 自 `docs/` 归档到 `docs/history/`)。描述的是当时的设计与
> 决策,其中的命令 / 路径 / 模块可能已随版本变更;现状以 `AGENTS.md`、`README.md`、
> `docs/ARCHITECTURE.md` 为准。索引见 `docs/README.md`。

## 一、文档信息

| 项 | 内容 |
|---|---|
| 版本 | v1.1（评审通过） |
| 目标版本 | tgmonitor 2.0.4 |
| 基线版本 | 2.0.3（`bbba60d`，tag `v2.0.3`） |
| 作者 | 司南（产品经理 Agent） |
| 需求方 | forcetone |
| 日期 | 2026-11-17 |
| 状态 | **已评审通过**（2026-11-17 第 1 轮，A1/A2/A3 已确认），可进入实施 |

**变更记录**

| 版本 | 日期 | 变更摘要 |
|---|---|---|
| v1.0 | 2026-11-17 | 初稿。基于代码事实诊断（三路只读排查，均带文件:行号证据），把 4 条诉求归并为 6 条 FR，选定核心决策点并展开完整 PRD。 |
| v1.1 | 2026-11-17 | 评审确认：A1 采纳 D1-β（实时流迁入大盘、撤销独立页）；A2 采纳 D2-α + D3-α（9 类运维事件、不含逐条消息）；A3 选择**不限量全留存**（否决推荐上限 500，取消裁剪逻辑）。全文同步收敛。 |

**评审记录**：第 1 轮通过（2026-11-17，用户逐项确认 A1/A2/A3）。A1 采纳推荐 D1-β，A2 采纳推荐 D2-α + D3-α，A3 选择「不限量全留存」（非推荐项，据此取消裁剪逻辑）。

> 诊断方式声明：本文所有「现状」结论均来自对 `bbba60d` 工作树的只读排查，未修改任何源码。文中「改动锚点」是实施落点建议，非已落地事实。

---

## 二、背景与目标

### 2.1 背景

2.0.2（实时流只读化）与 2.0.3（UI 减法 + core 门控）连续两版做减法后，应用形成 5 个导航页：`实时流 / 大盘 / 频道 / 媒体 / 设置`。实际使用暴露四类问题：

- 大盘「最近活动」时间线在全局 QSS（`app.py:215-219` 加载 `style.qss`）下，文字只显示中段、上下被裁——这是 bug，不是设计。
- 设置页「登录 / 登出」按钮被放在「账户凭证」分组**外面**（`settings_page.py:214` 把 `auth_row` 加到 `form_root`，是 groupbox 的兄弟节点），与用户的心智分组不符。
- 「实时流」与「大盘」都是只读看板，职责重叠：想看消息要到实时流页，想看统计要到大盘页，来回切换。用户提出把实时流搬进大盘、统计降为下方左右块——本质是**把大盘升级为唯一工作台**。
- 「最近活动」目前是纯内存 `QListWidget`（`dashboard_widget.py:221`，200 条上限、重启即丢），且展示的是开发视角的事件文本（如「设置已变更: xxx」），不承载运营语义；用户要求重新设计展示内容并**入库存储**。

### 2.2 要解决的问题

四条诉求表面是「2 个 bug + 2 个优化」，本质是两件结构改造：

1. **布局整合**：让「操作台」成为唯一工作台——实时流在上、统计在下，减少导航跳转；顺带修两个 UI 归属/渲染缺陷。
2. **活动可信化**：把易失、文本化的事件时间线，升级为持久化、字段化、可追溯的活动账本。

### 2.3 目标与成功指标（口径明确）

| 目标 | 成功指标 | 口径与来源 |
|---|---|---|
| G1 文字完整可见 | 目标宽度下任意活动条目无上下裁切 | 自动化断言：给定 delegate 可用宽度 W（半宽块按 ≥360px 计），`sizeHint().height() >= 文本换行所需行数 × 行高 + 上下边距`；QSS 加载路径与 offscreen 路径各测一次 |
| G2 登出入口归位 | 登出按钮的祖先链中含「账户凭证」QGroupBox | UI 自动化：`btn_logout` 向上遍历 parent，命中 `title()` 含「账户凭证」的 QGroupBox |
| G3 单一实例 + 布局 | 实时流 widget 全应用唯一，且位于大盘；导航项数 4，首页为大盘 | `findChildren(MessageView)` 计数 == 1；`nav.count() == 4`；`stack.currentIndex() == 0` 对应 DashboardWidget |
| G4 活动持久化 | 写入 N 条活动 → 重建 DashboardWidget（模拟重启）→ 读到 N 条 | 存储契约测试：两后端（sqlite/inmemory）+ PG 集成各验一次；时间戳取自事件 `occurred_at` 而非渲染时刻 |
| G5 无功能回归 | 既有测试基线不下降（扣除本版刻意调整的断言） | 全套 pytest 通过；改动的测试逐条登记在 RTM（附录 B） |

**非目标**：本期不做历史活动检索页；不把逐条消息写入活动表（由实时流承载）；不改 core 领域服务公开 API（仅新增 `ActivityStore` 域接口）；不做版本 bump 与 CHANGELOG 发布条目（单独 PR）；不改登录/登出底层逻辑（`LoginDialog` / `app.auth.logout()` 原样复用）。

---

## 三、名词与术语定义

| 术语 | 定义 |
|---|---|
| 实时流模块 | 当前导航第 0 项「实时流」页的内容：`MessageView`（`live_view`，QListView）+ `MessageDetail`（QScrollArea），由 `ui_assembler.py:128-144` 内联装配进 QSplitter，**没有独立 widget 类** |
| 大盘 / 操作台 | 当前导航第 1 项 `DashboardWidget`（`ui_assembler.py:149-155`），含 5 张统计卡 + 最近活动 + 频道统计 |
| 最近活动 | `DashboardWidget.activity_list`（QListWidget），当前订阅 EventBus 6 类事件、以 `_format_*` 转文本、内存 200 条上限 |
| 活动记录 | 本期新增的 `ActivityDTO` + `activities` 表记录：时间/类型/标题/详情/级别/可选关联 id，落库后由 UI 读取渲染 |
| core 侧 formatter | 新增于 `core/` 的纯函数（禁止 import PySide6/qasync），把领域事件归一化为 `ActivityDTO`，替代当前 UI 内的 `_format_*` |

---

## 四、需求范围

### 4.1 包含（本期做）

- **FR-01** 修复最近活动文字截断：改为可测的行高计算（宽度感知），并适配半宽布局。
- **FR-02** 把「登录 / 登出」按钮移入「账户凭证」分组内部。
- **FR-03** 实时流单实例迁入大盘；「最近活动」与「频道统计」改为左右两块置于实时流下方；导航项 5→4，大盘成为首页。
- **FR-04** 重新设计最近活动展示内容：定义字段集、事件覆盖范围，formatter 下沉 core。
- **FR-05** 活动入库：`ActivityDTO` + `ActivityStore` 域接口 + SQLite/PG 双后端 + in-memory 测试替身 + 启动加载。
- **FR-06** 同步更新测试与契约（含索引硬编码、表集合断言），清理孤儿代码。

### 4.2 不包含（本期明确不做）

- 保留独立「实时流」导航页 + 在大盘内再嵌一套 widget（双实例方案，见 D1-α）。
- 逐条 `MessageReceived` / `MessageEdited` / `MediaDownloaded` 写入活动表（见 D3-β）。
- 活动记录的历史检索、按类型筛选 UI、导出（可留后续版本）。
- 改 `MessageView` / `MessageDetail` 内部实现（本期只搬位置，不改行为）。
- 存量活动数据回填（冷启动为空是预期，活动从本版上线起积累）。

---

## 五、方案对比与选型说明

### 5.1 决策点 D1：实时流搬家后的实例与导航策略（对应 FR-03）

| 方案 | 思路 | 成本 | 一致性 | 风险 | 评价 |
|---|---|---|---|---|---|
| α 复制实例 | LIVE 页保留原样，大盘内再建一套 MessageView/MessageDetail | 低（不动导航） | 差：双 model/controller、状态不同步、Esc/Ctrl+C 语义分裂 | 高（两处消息流各自刷新、选中态不一致） | 不推荐 |
| **β 单实例迁入 + 导航收敛（推荐）** | 唯一实例挂进大盘；撤销 LIVE 导航项；大盘成为 nav 0 / 首页 | 中 | 好：一份数据、一份选中态 | 中：4 处索引硬编码需同步（见 RK-01） | **推荐** |
| γ 导航别名 | 保留 LIVE 项但指向大盘同一 widget | 中 | 差：QStackedWidget 不能重复 add 同一 widget，需 reparent，两项指向同页语义混乱 | 中高 | 不推荐 |

**β 的关键有利事实**：入口索引 0 的既有判断（Esc 回首页、Ctrl+C 仅首页生效、`_focus_search`）在「大盘 = 首页」后**语义自然成立**，`lifecycle_controller.py:105/118` 与 `main_window.py:497-501` 基本无需改逻辑；真正要改的只有 `on_nav_changed` 的 `idx == 3`（媒体）→ `idx == 2`（`lifecycle_controller.py:88`）、导航标签与默认高亮（`nav_bar.py:34/48/209-211`）。

### 5.2 决策点 D2：活动数据模型与入库路径（对应 FR-05）

| 方案 | 思路 | 成本 | 覆盖度 | 风险 | 评价 |
|---|---|---|---|---|---|
| **α 专用 activities 表 + core 订阅落库（推荐）** | 新增 `ActivityDTO` / `ActivityStore` / 双后端表 / in-memory 替身；core 在 bootstrap 注册订阅写库；UI 启动时读库 | 中高 | 高：登录/设置/错误等非业务表事件也覆盖 | 中：跨后端 parity + schema 断言（RK-04） | **推荐**，且符合用户「将其入库存储」字面要求 |
| β 业务表 UNION 查询 | 从 channels/messages/meta 直接 UNION 出「活动」 | 低 | 低：登录态/设置变更/错误无业务表落点，丢失 | 低 | 不推荐 |
| γ 塞进 meta 表 JSON | 活动数组存 `meta.value` | 最低 | 中 | 中：不可查询、无索引、并发写覆盖、扩展差 | 不推荐 |

### 5.3 决策点 D3：最近活动的事件覆盖范围（对应 FR-04）

| 方案 | 覆盖事件 | 评价 |
|---|---|---|
| **α 运维事件集（推荐）** | 登录态、频道监听/取消、频道同步完成、批量完成、导出完成、媒体对账完成、设置变更、错误/认证错误 | **推荐**：半宽块信息密度合适，且与实时流不重复 |
| β 全事件集 | 在 α 基础上加逐条消息接收/编辑/媒体下载/各类 Progress | 不推荐：半宽块被高频事件刷屏，与实时流职责重复，DB 写入放大 |

### 5.4 推荐组合与理由

**已确认采用 D1-β + D2-α + D3-α**（2026-11-17 第 1 轮评审）：三条决策相互咬合——大盘成为唯一工作台（D1），工作台下方左块展示来自持久化活动账本的运维事件（D2+D3），右块保留频道统计；实时流独占上方大区，消息类信息不再进入活动块。风险集中在索引硬编码与跨后端 schema，均可用自动化测试闭环。A3 另定了「不限量全留存」，见 §10.3。

### 5.5 演进路径

- **本期**：6 条 FR 一次落地，分 3~4 个 PR（建议：FR-01+02 一个、FR-04+05 一个、FR-03 一个、FR-06 随各 PR）。
- **后续（触发条件：用户提出活动检索需求）**：在活动表加类型筛选 + 时间范围查询 UI，本期不预建。
- **本期决定（A3）**：活动记录不限量、全留存，不做写入裁剪。代价是 `activities` 表随时间单调增长（已向需求方明确，见 §10.3）。
- **后续（触发条件：用户改主意或表体量成为实际瓶颈）**：再评估加保留期清理任务；本期不预建。

---

## 六、用户角色与使用场景

**目标用户**：tgmonitor 桌面应用使用者（频道监听的运营/观察者），单机使用，熟练桌面 GUI。

| 场景 | 用户目标 | 当前痛点 | 本期后的流程 |
|---|---|---|---|
| S1 开机确认监听正常 | 一眼看到消息流与关键事件 | 实时流在大盘之外的另一页，来回切 | 打开即大盘：上方实时流、左下最近活动、右下频道统计 |
| S2 追溯「刚才发生了什么」 | 查登录/订阅/导出/错误等动作 | 事件内存态，重启丢失，文本还被裁 | 最近活动从库加载，文字完整，重启后仍在 |
| S3 修改 Telegram 凭据后登出重登 | 在同一分组内完成凭据编辑与登出 | 登出按钮漂浮在凭证块外 | 按钮归入「账户凭证」分组内 |

---

## 七、功能需求（编号 + 业务规则 + GWT 验收）

### FR-01 修复最近活动文字截断

**业务规则**
- 条目高度必须按**实际可用宽度**计算，禁止在宽度未定时用 `sizeHint()` 锁定（现 `dashboard_widget.py:332`）。
- 半宽块（导航收敛后「最近活动」与「频道统计」左右并排）宽度更窄，换行更多，行高适配必须在此布局下仍成立。
- 时间戳取事件 `occurred_at`，不得用渲染时刻（现 `_ActivityEntry` 内 `datetime.now()`，`dashboard_widget.py:119`）。

**验收标准**
- Given 一条 60 字的活动文本，When 在 ≥360px 宽的目标块中渲染，Then 文本完整可见且 `sizeHint().height()` 不小于换行所需高度（自动化几何断言）。
- Given QSS 已加载（真实 app 路径），When 添加同一条目，Then 无上下裁切。
- Given 定时器/事件追加，When 新增一条，Then 新条目时间等于事件发生时刻（非当前时刻）。

**改动锚点**：`dashboard_widget.py:110-132`（`_ActivityEntry`）、`:328-342`（`_add_activity`）；实现方案建议用 `QListView` + 自定义 delegate 的 rect 感知 `sizeHint`/`paint`（`setResizeMode(Adjust)` 随宽重排），或给条目显式参考宽度后再算 `sizeHint`。

### FR-02 登出入口移入账户凭证块

**业务规则**
- 「登录…」「登出」两个按钮必须是「账户凭证」QGroupBox 的内部子控件，不参与 `_collect()` / 保存 .env。
- 两按钮显隐仍由 `set_login_state()` 按 `READY_STATE` 控制，底层仍是 `LoginDialog` / `app.auth.logout()`。

**验收标准**
- Given 设置页已构造，When 遍历 `btn_logout` 的 parent 链，Then 命中标题含「账户凭证」的 QGroupBox。
- Given 未登录态，When `set_login_state("uninit")`，Then 登录可见、登出隐藏（行为不变）。

**改动锚点**：`settings_page.py:191` 保留 `gb` 引用，把 `:202-214` 的 `root.addLayout(auth_row)` 改为 `gb.layout().addLayout(auth_row)`（`FormSection.build()` 内部是 QVBoxLayout，见 `form_section.py:154`）。

### FR-03 实时流迁入大盘 + 左右分块 + 导航收敛

**业务规则**
- 全应用只有一对 `MessageView` / `MessageDetail` 实例；它们从 LIVE 页 reparent 进 DashboardWidget。
- DashboardWidget 内部顺序：标题 → 5 张统计卡 → 实时流区（QSplitter）→ 左右两块（左：最近活动；右：频道统计）。
- 导航 5→4：`大盘 / 频道 / 媒体 / 设置`；首页为大盘。
- `MainWindow.live_view` / `.message_detail` / `.dashboard` 属性名保持不变（控制器与测试依赖它们）。

**验收标准**
- Given 应用启动，When 统计 `findChildren(MessageView)`，Then 计数 == 1；`stack.currentIndex() == 0` 是 DashboardWidget。
- Given DashboardWidget，When 检查布局，Then 实时流区在统计卡之后，最近活动与频道统计位于同一 QHBoxLayout 且分别占左右。
- Given 切换到媒体页，When `on_nav_changed(idx)` 收到媒体页索引（现为 2），Then 触发媒体列表刷新。
- Given 在首页按 Esc / Ctrl+C，When 有选中消息，Then 复制行为与 2.0.3 一致（索引 0 语义保持）。

**改动锚点**：`ui_assembler.py:128-155`（装配与 reparent）、`dashboard_widget.py:180-249`（`_build()` 重排）、`nav_bar.py:34/48/201-211`（标签与默认高亮）、`lifecycle_controller.py:88`（媒体索引 3→2）、`main_window.py:433-435`（属性保持）。

### FR-04 重新设计最近活动展示内容

**业务规则**
- 展示字段集（建议）：时间（HH:MM:SS，来自 `occurred_at`）、类型徽标、标题、详情、级别（info/warn/error）。
- 覆盖范围采用 D3-α 运维事件集；`MessageReceived` 不进入活动块（v2.0.3 D4 决策保持）。
- 归一化 formatter 必须在 `core/`（禁止 import PySide6/qasync），供 UI 与落库共用单一口径。
- 修掉现有 `#{id(e)}`（`dashboard_widget.py:294`，取的是 Python 对象地址）这类错误兜底。

**验收标准**
- Given 任一覆盖事件，When 经 core formatter，Then 产出含 5 字段的 `ActivityDTO`，字段无对象地址/异常对象等不可序列化内容。
- Given 未覆盖事件（如 `MediaDownloadProgress`），When 发布，Then 不产生活动记录。
- Given `ChannelMonitored` 且 `channel is None`，When 格式化，Then 详情为稳定的空值文案而非 `id(e)`。

**改动锚点**：新增 `core/activity.py`（formatter 表，仿 `dashboard_widget.py:312-326` 的 dispatch 写法）；`dashboard_widget.py:282-326` 的 `_format_*` 迁移/删除。

### FR-05 活动入库存储

**业务规则**
- 新增 `activities` 表；`ActivityStore` 作为 `StorageRepository` 的第 8 个域接口（现 7 个，`repository.py:498-511`）。
- core 在 bootstrap（`runtime.py:64-107`，storage 就绪后）注册订阅者，事件 → formatter → `ActivityStore.append()`；订阅者异常被吞，不影响主流程（沿用 EventBus 契约）。
- UI 启动时从库读取最近 N 条（建议 N=200），替换纯内存时间线；时间戳呈现本地时区。
- SQLite 与 PG 的 `SCHEMA_SQL` / `EXPECTED_SCHEMA` 两处必须同步（项目惯例）。

**验收标准**
- Given 新库，When `init_schema()`，Then `activities` 表存在且 `introspect_schema().ok is True`（两后端 + PG 集成）。
- Given 发布 3 条覆盖事件，When 重启并重建 DashboardWidget，Then 读到 3 条且字段与写入一致。
- Given 活动持续写入，When 数量超过任意阈值，Then 不做裁剪、全部保留（A3：不限量全留存）。
- Given core 模块，When 静态扫描，Then 无 `import PySide6` / `qasync`（架构守门 `tests/test_architecture.py`）。

**改动锚点**：`core/dto.py`（`ActivityDTO`）、`core/storage/repository.py:469-527`（新接口 + 组合 + `__all__`）、`sqlite_repo.py:51-242`（SCHEMA_SQL + EXPECTED_SCHEMA + 实现）、`_lifecycle_pg.py:34-301`（同）、`postgres_repo.py:55-115`（thin delegate）+ 新建 `_activity_pg.py`、`tests/fixtures/_in_memory_repository.py:31`、`core/runtime.py:64-107`、`dashboard_widget.py:378-440`（仿 `run_coro` 异步加载）。

### FR-06 测试与契约同步

**业务规则**
- 表集合硬编码断言必须更新：`tests/test_introspect_repair.py:16-22` 现有 `{"channels","messages","media","meta","thumbnails"}`。
- DDL ↔ EXPECTED_SCHEMA 对齐测试（`test_sqlite_repo.py:81-96`）需覆盖新表。
- 索引相关测试复核：`tests/test_lifecycle_controller.py:128-136/:191`、`test_status_bar_layout.py:21-34`、`test_ui_simplification_2003.py:243-273`、`test_live_readonly_2002.py:41-91`。
- 新增契约测试文件（建议 `tests/test_activities_2004.py`、`tests/test_dashboard_layout_2004.py`）守住 G1~G4。

**验收标准**：全套 pytest 通过；新增契约测试覆盖 G1~G4 每条至少 1 例。

---

## 八、非功能需求

| 类别 | 要求 |
|---|---|
| 性能 | 活动写入走 EventBus 订阅，不阻塞主循环；Dashboard 启动加载 N=200 条，目标 <200ms（本地 SQLite）；写库失败不得崩应用 |
| 兼容性 | SQLite / PG 双后端行为一致；存量库通过 `CREATE TABLE IF NOT EXISTS` + 现有 `init_schema`/`repair_schema` 链路升级，不要求用户手工迁移 |
| 可测试性 | formatter 为纯函数；活动读写有 in-memory 替身；几何断言可在 offscreen QPA 下运行 |
| 可访问性 | 活动文本对比度满足基础可读；级别用文字/图标双通道表达，不单靠颜色 |
| 架构 | `core/` 禁止 import PySide6/qasync；跨边界只传 DTO（既有守门测试保持绿） |

---

## 九、业务流程与状态流转

**活动数据流（主流程）**

```
领域事件 (events.py)
   → EventBus.publish（core，按 MRO 匹配订阅）
   → core formatter：event → ActivityDTO | None（D3 白名单过滤）
   → ActivityStore.append(dto)（写 activities 表，不裁剪）
   → DashboardWidget 启动加载最近 N 条 → 渲染到左块
```

**活动记录生命周期**

| 状态 | 触发 | 允许操作 |
|---|---|---|
| 产生 | 覆盖事件发布 | 归一化、入库 |
| 已落库 | `append()` 成功 | 展示、计数 |
| 已归档 | 落库后随时间累积 | 只读；本期不删除、不裁剪（A3） |

**异常分支**：formatter 取不到事件字段 → 产出稳定空值文案，不丢整条；写库失败 → 订阅者吞异常 + 日志，UI 不受影响；存量库无表 → 启动 `init_schema` 建表。

---

## 十、数据与埋点需求

### 10.1 ActivityDTO 字段（建议）

| 字段 | 类型 | 含义 | 必填 |
|---|---|---|---|
| `id` | int | 自增主键 | 是（库生成） |
| `occurred_at` | datetime | 事件发生时刻（来自 `Event.occurred_at`，UTC 存储） | 是 |
| `event_type` | str | 稳定标识（如 `channel_monitored`） | 是 |
| `icon` | str | `action_icon` 名（或由 type 映射） | 是 |
| `title` | str | 短标题（如「已监听频道」） | 是 |
| `detail` | str | 详情文本（截断到合理长度，如 200 字） | 否 |
| `severity` | str | `info` / `warn` / `error` | 是 |
| `channel_id` | int \| None | 可选关联频道 | 否 |
| `telegram_msg_id` | int \| None | 可选关联消息 | 否 |

### 10.2 事件覆盖映射（D3-α，建议）

| 事件类（`events.py`） | title | severity | 备注 |
|---|---|---|---|
| `LoginStateChanged` | 登录状态切换 | info/warn（按 state） | 详情取 `detail[:N]` |
| `ChannelMonitored` | 已监听频道 | info | 取 `channel.title` |
| `ChannelUnmonitored` | 已取消监听 | info | 取 `channel_id` |
| `ChannelSyncDone` | 频道同步完成 | info | 结果摘要 |
| `BatchDone` | 批量操作完成 | info/error（按 `error`） | 成功/失败计数 |
| `ExportDone` | 导出完成 | info/error | 路径或错误 |
| `MediaReconcileFinished` | 媒体对账完成 | info | 扫描/清理计数 |
| `SettingsChanged` | 设置已变更 | info | 取 `what` |
| `ErrorOccurred`（含 `AuthErrorOccurred`） | 发生错误 | error | 只存 `str(message)[:N]`，不存异常对象 |

> 不覆盖：各类 `Progress` 事件、`MessageReceived` / `MessageEdited` / `MediaDownloaded` / `ChannelMetadataChanged` 等高频或消息类事件。

### 10.3 保留策略与口径

- **展示口径**：启动加载最近 200 条（与现内存上限一致，用户无感知变化）。
- **保留策略（A3 已确认）**：不限量、全留存，不实现写入裁剪。已知代价：`activities` 表单调增长，无任何自动清理；若未来成为瓶颈，另立需求评估清理任务（本期不做）。
- **时间口径**：`occurred_at` 存 UTC，UI 按本地时区显示 `HH:MM:SS`；不得用渲染时刻。

---

## 十一、权限与安全要求

- 单机桌面应用，无多用户/权限模型；本期不引入账号体系。
- 活动详情可能来自异常信息：`detail` 只保存 `str(message)` 且截断，**不保存异常对象、不保存凭据字段**；API Hash / 手机号 / Session 路径等凭据不得进入活动表。
- 活动数据全部本地存储，不联网上报；合规上属于本机运行日志，无跨境/个人信息外发场景。

---

## 十二、异常处理清单

| 场景 | 处理 |
|---|---|
| 活动表不存在（存量库） | 启动 `init_schema()` 建表；`repair_schema` 不负责补表（既有约束） |
| formatter 取不到事件字段 / 字段为 None | 产出稳定空值文案，不丢整条记录 |
| 事件 payload 含不可序列化对象 | formatter 只提取 str/int/datetime；异常对象只取字符串摘要 |
| 写库失败（磁盘满 / 锁） | 订阅者吞异常 + `log.warning`，主流程与 UI 不受影响 |
| 活动量持续增长 | 按 A3 全留存、不裁剪；表体量问题留后续需求评估 |
| 半宽块窄到不足以单行 | delegate 按实际宽度换行并返回足够高度（FR-01） |
| PG 未就绪 / testcontainers 不可用 | PG 集成测试按既有 skip 策略处理，不阻塞 CI 门禁 |

---

## 十三、上线计划、依赖与上线后验证

### 13.1 依赖就绪度评估

| 依赖项 | 所属 | 就绪状态 | 分级 | 最晚就绪时限 | 影响与替代 |
|---|---|---|---|---|---|
| `MessageView`/`MessageDetail` 可 reparent | 本项目 UI | 已确认（`ui_assembler.py:128-144` 内联装配，无反向持有） | 可绕过 | — | 无需替代 |
| 未提交的 `sqlite_repo.py` 补列修复 | 本项目 core | 工作树存在未提交改动（`init_schema` 幂等补列 + 2 个测试） | **并行** | FR-05 动同文件前 | 先按仓库约定单独提交/开 PR，或被本版同分支一并纳入；避免同文件冲突 |
| 索引硬编码清单 | 本项目 UI | 已定位 4 处（`lifecycle_controller.py:88/105/118`、`main_window.py:497-501`） | 并行 | FR-03 合并前 | 已列入 FR-06 测试复核 |
| PG 集成环境 | CI | 现有 testcontainers 链路可用 | 可绕过 | — | 不可用时跳过集成，靠 SQLite + in-memory 契约 |

> 本期无外部系统阻塞依赖。

### 13.2 建议 PR 切分（每分支一主题，squash merge）

1. `fix/dashboard-activity-clipping-and-logout` — FR-01 + FR-02（2 个 bug，低风险）
2. `feat/activity-persistence-2004` — FR-04 + FR-05（core 与存储，含双后端 + 契约测试）
3. `refactor/dashboard-consolidation-2004` — FR-03（布局与导航，含索引修正）
4. 随各 PR 带上 FR-06 对应测试；最后单独 PR 做版本 bump + CHANGELOG。

### 13.3 上线后验证清单

- [ ] 本地全套 pytest + ruff + mypy 全绿；CI 三平台 8/8 全绿
- [ ] 真机启动：首页为大盘，实时流在上、两统计块在下，文字完整
- [ ] 设置页登出按钮在「账户凭证」框内；登录/登出流程正常
- [ ] 制造一条错误事件 → 重启应用 → 活动仍在
- [ ] 存量库（旧 schema）启动不报错，`introspect_schema().ok`

---

## 十四、风险与遗留问题

### 14.1 风险登记册

| 编号 | 风险描述 | 类别 | 概率 | 影响 | 等级 | 应对措施 | 责任人 | 状态 |
|---|---|---|---|---|---|---|---|---|
| RK-01 | 页索引硬编码遗漏（媒体 idx 3→2、首页 0 语义、测试断言） | 技术 | 4 | 4 | 16 高 | FR-06 列全清单 + 导航切换测试逐页覆盖 | 实施方 | 待处理 |
| RK-02 | 未提交 `sqlite_repo.py` 改动与 FR-05 同文件冲突 | 进度 | 3 | 3 | 9 中 | §13.1 并行依赖：先单独提交或纳入同分支 | 实施方 | 待处理 |
| RK-03 | 半宽块更窄导致文字截断加重 | 体验 | 4 | 3 | 12 中 | FR-01 用宽度感知 delegate，G1 几何断言在半宽下测 | 实施方 | 待处理 |
| RK-04 | 新增表跨后端 parity / schema 断言遗漏 | 数据 | 3 | 4 | 12 中 | 两后端 SCHEMA_SQL 与 EXPECTED_SCHEMA 同步；更新表集合断言；PG 集成验证 | 实施方 | 待处理 |
| RK-05 | 事件 payload 含不可序列化对象导致落库失败 | 技术 | 3 | 3 | 9 中 | core formatter 只提取基本类型，异常只取字符串 | 实施方 | 待处理 |
| RK-06 | 双实例诱惑导致需求被「简化」为复制方案 | 需求 | 3 | 4 | 12 中 | D1 已明确推荐 β；评审时复核 G3 唯一实例断言 | 产品 | 待处理 |
| RK-07 | A3 全留存致 `activities` 表随时间增长 | 数据 | 4 | 2 | 8 中 | 用户已明确接受；不设裁剪，定期观察体量，成为瓶颈时另立清理需求 | 实施方 | 已接受 |

### 14.2 遗留问题

- 不提供活动历史检索/筛选 UI（触发条件见 §5.5）。
- 逐条消息不进活动账本（维持 v2.0.3 D4 决策）。
- 存量活动无回填，历史活动从本版起积累。

---

## 十五、附录

### 附录 A — 诊断证据索引（只读排查结论）

| 结论 | 证据 |
|---|---|
| 实时流无独立 widget 类，是装配段 | `ui_assembler.py:128-144` |
| 页面顺序 5 项、大盘第 1 页 | `ui_assembler.py:120-183`；`nav_bar.py:34` |
| 页 0 硬编码 | `lifecycle_controller.py:105/118`；`main_window.py:497-501`；`nav_bar.py:209-211` |
| 媒体页索引 3 | `lifecycle_controller.py:88` |
| 登出按钮在凭证块外 | `settings_page.py:191/202-214`；`form_section.py:154` |
| 文字截断根因：宽度未定时 `sizeHint` 锁行高 | `dashboard_widget.py:110-132/328-342`；`style.qss:434-439`（`QListWidget::item` padding，非固定高） |
| 时间用渲染时刻 | `dashboard_widget.py:119` |
| `#{id(e)}` 错误兜底 | `dashboard_widget.py:294` |
| 事件枚举 31 类、基类有 `occurred_at` | `events.py:28-421` |
| EventBus 按 MRO 分发、`subscribe_all` | `events.py:444-475` |
| 存储 7 域 ABC + 组合门面 | `repository.py:83-527` |
| 无活动/审计表 | `sqlite_repo.py:51-159`；`_lifecycle_pg.py:34-208` |
| schema 断言硬编码表集合 | `test_introspect_repair.py:16-22` |
| 架构守门测试 | `tests/test_architecture.py:25-29/99-113` |

### 附录 B — 需求追踪矩阵（RTM）

| 需求 | 来源 | PRD 章节 | 改动锚点 | 验收用例 | 状态 |
|---|---|---|---|---|---|
| R-01 文字截断 | 用户诉求 1 | FR-01 §7 | `dashboard_widget.py:110-132/328-342` | `test_dashboard_layout_2004`（G1） | 待实施 |
| R-02 登出归位 | 用户诉求 2 | FR-02 §7 | `settings_page.py:191/202-214` | 祖先链断言（G2） | 待实施 |
| R-03 布局整合 | 用户诉求 3 | FR-03 §7 | `ui_assembler.py:128-155` 等 | `test_dashboard_layout_2004`（G3） | 待实施 |
| R-04 活动重设计 | 用户诉求 4 | FR-04 §7 | 新增 `core/activity.py` | formatter 契约（G4） | 待实施 |
| R-05 活动入库 | 用户诉求 4 | FR-05 §7 | dto/repository/两后端/runtime | 存储契约（G4） | 待实施 |
| R-06 测试同步 | 衍生 | FR-06 §7 | 测试文件见 §7 | 全套 pytest | 待实施 |

### 附录 C — 评审决策记录（第 1 轮，2026-11-17 已确认）

| 编号 | 决策点 | 用户选择 | 落地影响 |
|---|---|---|---|
| **A1** | 独立「实时流」导航项的存废 | **D1-β**：迁入大盘并撤销独立页，大盘成首页（4 项） | FR-03 按单实例方案实施；需同步 4 处页索引硬编码（RK-01） |
| **A2** | 活动覆盖范围与入库路径 | **D2-α + D3-α**：新建 `activities` 表，覆盖 9 类运维事件、不含逐条消息 | FR-04/FR-05 按此实施；formatter 下沉 core |
| **A3** | 活动保留上限 | **不限量、全留存**（否决推荐的 500 条上限） | 全文取消裁剪逻辑；`activities` 表单调增长的风险已如实记录于 §10.3 |

### 附录 D — 合规声明

本方案为产品设计参考，具体实施请结合公司合规与法务要求确认。本文所述改造均为原创设计，未复制任何竞品的专有文案、视觉或代码。
