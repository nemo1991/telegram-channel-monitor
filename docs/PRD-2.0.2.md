# PRD — tgmonitor 2.0.2「实时流只读化」版本规划

## 一、文档信息

| 项 | 内容 |
|---|---|
| 版本 | v1.0（初稿） |
| 目标版本 | tgmonitor 2.0.2 |
| 基线版本 | 2.0.1（`c65b098`，tag `v2.0.1`） |
| 作者 | 司南（产品经理 Agent） |
| 需求方 | forcetone |
| 日期 | 2026-11-16 |
| 状态 | 已评审通过（A1/A2/A3 已确认），**实施完成**，本地验证全绿待 PR |

**变更记录**

| 版本 | 日期 | 变更摘要 |
|---|---|---|
| v1.0 | 2026-11-16 | 初稿。基于代码事实诊断，选定核心决策点 β，展开三条需求的完整 PRD。 |
| v1.1 | 2026-11-16 | 评审确认：A1 保留状态与登录、A2 右键菜单 6 项一并删、A3 详情保留"复制文本"。三个假设全部转为已确认决策，进入实施。 |
| v1.2 | 2026-11-16 | 实施完成。FR-01~FR-06 全部落地；删除 6 个孤儿源文件 + 6 个专属测试文件，新增 `tests/test_live_readonly_2002.py`（5 个契约用例）。本地基线 `1378 passed / 38 skipped / 63 deselected`，ruff / format / mypy 全绿。 |

**评审记录**：第 1 轮通过（2026-11-16，用户逐项确认 A1/A2/A3，均采纳推荐方案）。

---

## 二、背景与目标

### 2.1 背景

2.0.1 完成了实时流装配修复（消息可点击、详情可弹出）。在实际使用中，用户发现当前 LIVE 页承载了过多的"操作型"功能，与页面的真实用途不匹配：

- 顶部 `HeaderBar` 中嵌了一个功能齐全的 `SearchBar`（文本 + 日期范围 + 订阅范围 + ★/🏷/📌 三个过滤器），占据紧凑头栏的显著位置，但用户判断其**没有实际用处，且位置规划不合理**。
- LIVE 页浮出一条 9 按钮的 `SelectionToolbar`（全选/反选/清除/标记已读/导出/删除/转发/钉选/表情回应），而用户对该页的定位是**"确认当前频道监听状态 + 查看最近落地的消息"**，不需要批量操作。
- 消息详情面板底部常驻"导出 / 删除 / 复制文本"三颗按钮，其中导出与删除与只读定位冲突。

### 2.2 要解决的问题

三条诉求表面是"删按钮"，本质是**页面职责重新收敛**：把 LIVE 页从"消息工作台"还原为"监听状态看板"。若只按字面删点名按钮，会留下功能割裂（工具条消失但右键菜单仍可删除、详情无导出但批量导出仍在），反而增加认知负担。

### 2.3 目标与成功指标（口径明确）

| 目标 | 成功指标 | 口径与来源 |
|---|---|---|
| G1 页面职责单一化 | LIVE 页可见的**写操作入口 = 0** | 枚举 LIVE 页（含详情面板、右键菜单）可见的导出/删除/标记/收藏/表情等按钮与菜单项，计数为 0；以 UI 自动化断言为准 |
| G2 消息展示口径明确 | 启动加载 ≤ 100 条；视图内常驻 ≤ 100 条 | `load_recent_messages` 的 `limit=100`（代码可查）；视图 `MAX_ITEMS=100`（超限删最旧） |
| G3 顶部回归紧凑 | `HeaderBar` 内无搜索控件 | `HeaderBar` 不再实例化 `SearchBar`，头栏仅保留标题 + 状态 + 操作按钮 |
| G4 无功能回归 | 已有测试基线不下降（扣除本版刻意移除的用例） | 全套 pytest 通过；移除的测试从分母中扣除，其余用例 0 失败 |

**非目标**：本期不新增功能、不改数据模型、不改 core 层公开 API、不调整导航结构、不做版本 bump（bump 单独走 PR）。

---

## 三、名词与术语定义

| 术语 | 定义 |
|---|---|
| LIVE 页 / 实时流 | 导航第一项（`QStackedWidget` page 0），承载 `MessageView` + `MessageDetail` |
| 已落地消息 | 已由 `MonitorService` 调 `storage.save_message()` 入库的消息；`MessageReceived` 事件在入库**之后**发布（`core/monitor/service.py:324→332`） |
| 选择工具条 | `SelectionToolbar`，LIVE 页顶部浮层，默认隐藏，选中数 >0 时显示 |
| 详情面板 | `MessageDetail`，嵌在 LIVE 页右侧的 `QScrollArea`，点击消息后填充 |
| 写操作 | 任何会修改消息状态或落库内容的操作（导出、删除、标记已读、收藏/标签/备注、表情回应、转发、钉选） |

---

## 四、需求范围

### 4.1 包含（本期做）

- **FR-01** 移除顶部搜索区（`SearchBar` 及其 6 个信号链、debounce、`FocusSearch` 快捷键、VM 搜索结果回填）。
- **FR-02** 实时流消息口径改为"已订阅频道最近 100 条已落地消息"。
- **FR-03** 移除 LIVE 页选择工具条（`SelectionToolbar` 整条，含全部 9 按钮）。
- **FR-04** 移除右键菜单中的写操作（6 项，A2 已确认）。
- **FR-05** 移除详情面板的导出 / 删除按钮（保留复制文本、关闭详情）。
- **FR-06** 清理上述移除产生的孤儿 UI 代码与过时测试，保持代码库一致。

### 4.2 不包含（本期明确不做）

- 不删除 core 层服务方法（`MessageService.list_messages` / `BatchService.*` 等）——它们是公共 API，且被导出、媒体对账等复用；孤儿化只登记不清理。
- 不做历史消息检索的替代入口（见 §14 遗留 RK-01）。
- 不改 `MAX_ITEMS` 之外的消息模型、不新增埋点、不改设置项。
- 不改登录/登出、状态徽章、频道订阅、Dashboard、媒体管理器等其它页面。
- 不做版本号 bump 与 CHANGELOG 正式发布条目（单独 PR）。

---

## 五、方案对比与选型说明

### 5.1 候选方案

| 方案 | 思路 | 实现成本 | 一致性 | 风险 | 评价 |
|---|---|---|---|---|---|
| **α 字面减法是** | 只删用户点名的"全选 / 标记选中 / 表情回复 / 详情导出删除"，保留工具条其余按钮与右键菜单 | 低 | 差：留下"工具条还在但少了三颗"的半残状态，右键仍可删除/导出，与只读定位冲突 | 中（用户后续大概率再提一轮） | 不推荐 |
| **β 只读化重构（推荐）** | 按"LIVE 页 = 监听状态看板"定位，一次性移除全部写操作入口（工具条整条 + 右键菜单 + 详情导出删除），并收敛消息口径为 100 条 | 中 | 好：三条诉求由统一定位解释，页面边界清晰 | 低（core 不动，UI 层自洽） | **推荐** |

### 5.2 推荐理由

β 把三条诉求统一到一个可验证的产品定位（G1：写操作入口 = 0），避免 α 的"删一半"反复；且 core 层完全不动，风险集中在 UI 层，可用现有 UI 测试 + 新增契约测试闭环验证。

### 5.3 演进路径

- **本期**：UI 层只读化 + 100 条口径。
- **后续（触发条件：用户提出历史检索需求）**：评估独立"检索页"或在 Dashboard 增加检索能力，与 LIVE 页解耦；本期不预建。
- **后续（触发条件：core 孤儿方法确认无外部调用）**：单独 PR 清理孤儿 core 方法，不在本期混做。

---

## 六、用户角色与使用场景

**目标用户**：tgmonitor 桌面应用使用者（频道监听的运营/观察者），熟练使用桌面 GUI，单机使用。

| 场景 | 用户目标 | 当前痛点 | 本期后的流程 |
|---|---|---|---|
| S1 开机确认监听正常 | 看一眼哪些频道有新消息流入 | 页面混有搜索框和批量按钮，干扰判断 | 打开应用 → LIVE 页 → 看到最近 100 条已落地消息，确认状态 |
| S2 查看某条消息内容 | 读正文、看媒体信息、复制文本 | 详情底部有导出/删除按钮，误触风险 | 点击消息 → 详情面板读内容 → 可复制文本 → 关闭 |
| S3 导出历史消息 | 批量导出归档 | —（本期移除该入口） | 走 Dashboard 的导出能力（已存在），见 RK-01 |

---

## 七、功能需求（编号 + 业务规则 + GWT 验收）

> 状态词约定：**必须**（shall）/ **应该**（should）/ **可以**（may）/ **禁止**（must not）。

### FR-01 移除顶部搜索功能区

**描述**：从 `HeaderBar` 移除 `SearchBar` 及全部关联信号链；头栏保留标题、状态徽章、登录/登出按钮。

**业务规则**
- FR-01-R1 `HeaderBar` 禁止实例化 `SearchBar`；头栏高度维持 44px 不变。
- FR-01-R2 必须移除：`main_window.py:295-309` 的 6 个搜索信号接线、`:228-230/271` 的 300ms debounce timer 构造与注入、`LifecycleController.focus_search()`（`lifecycle_controller.py:98-102`）及其 `MainWindow` 委托（`:596-600`）、`ui_assembler.py:371` 的 `message_search_results → set_messages` 接线。
- FR-01-R3 VM `search_messages()` 与 `message_search_results` 信号（`_messages.py:105,175-230`）应该一并清理，避免成为无人消费的孤儿；core 层 `MessageService.list_messages` 的 `search` 等参数保留。

**验收标准**
- **Given** 应用主窗口已启动，**When** 观察顶部头栏，**Then** 不存在搜索输入框、`📅`/`🌐`/★/🏷/📌 控件，且状态徽章与登录/登出按钮仍可见可用。
- **Given** 任意时刻，**When** 触发原搜索快捷键，**Then** 无任何搜索行为（快捷键已解绑）。
- **Given** 全套测试执行，**When** 统计结果，**Then** `test_search_bar.py`、`test_search_controller.py` 已移除，`test_lifecycle_controller.py` 中 `focus_search` 用例已移除，其余用例 0 失败。

### FR-02 实时流消息口径改为最近 100 条

**描述**：LIVE 页只展示"已订阅频道最近 100 条已落地消息"，作为监听状态确认依据。

**业务规则**
- FR-02-R1 启动加载：`load_recent_messages()`（`_messages.py:169`）的 `app.messages.list_messages(limit=...)` 必须传 `limit=100`（当前为 200）。
- FR-02-R2 视图容量：`MessageListModel.MAX_ITEMS`（`message_list_model.py:183`）必须改为 100（当前 10000）；超限时删除最旧（`message_list_model_mutation.py:64-65,128-129` 的截断语义不变）。
- FR-02-R3 数据源不变：`MessageReceived` 在 `storage.save_message()` 之后发布，Live 增量消息天然"已落地"，禁止另加未落地的乐观插入。
- FR-02-R4 订阅语义不变：`channel_ids=None` 时仍走已订阅频道（`message_service.py:70-77`）；无订阅时仍显示 `no_subscribed` 空态。
- FR-02-R5 搜索触发的 `set_messages()` 批量替换路径随 FR-01 移除；实时增量（`MessageReceived`/`MessageEdited`/`MessageDeleted`）保留。

**验收标准**
- **Given** 库中某已订阅频道有 150 条消息，**When** 启动应用进入 LIVE 页，**Then** 视图最多显示 100 条，且为最近 100 条（最早 50 条不在列）。
- **Given** 视图已满 100 条，**When** 新落地 1 条消息，**Then** 视图仍为 100 条，最旧 1 条被移除。
- **Given** 无任何订阅频道，**When** 进入 LIVE 页，**Then** 显示 `no_subscribed` 空态，而非消息列表。
- **Given** 单测执行，**When** 运行 `test_message_view.py` 中 `MAX_ITEMS == 10000` 相关断言（`:813-815,818`）与 `tests/property/test_truncate_tail_invariant.py`，**Then** 断言已更新为 100 且通过。

### FR-03 移除 LIVE 页选择工具条

**描述**：整条移除 `SelectionToolbar`（9 颗按钮：全选/反选/清除/标记已读/导出选中/删除选中/转发/钉选/表情回应）。

**业务规则**
- FR-03-R1 删除 `widgets/selection_toolbar.py`，移除 `ui_assembler.py:134-136` 的装配与 `:330` 的 `clear_clicked` 接线。
- FR-03-R2 移除 `main_window.py:334-350` 的全部工具条信号接线（导出/删除/标记已读/转发/钉选/表情回应）。
- FR-03-R3 `MessageView` 的选择模式必须从 `ExtendedSelection`（`message_view.py:123`）改为 `SingleSelection`——单选仍可点开详情，但不支撑批量语义；`selection_count_changed`/`selection_messages_changed` 信号链随之清理。
- FR-03-R4 说明（代码事实）：**"全选"/"反选"本就是死信号**（无 connect，点击无业务效果），本期移除不产生行为损失。用户口述的"标记选中"对应代码中的"标记已读"，"表情回复"对应"表情回应…"。

**验收标准**
- **Given** 进入 LIVE 页，**When** 选中 1 条或多条消息，**Then** 页面顶部不出现任何浮动工具条。
- **Given** 用户尝试多选，**When** 按住 Ctrl/Shift 点选，**Then** 仅最后一条处于选中态（单选模式）。
- **Given** 全套测试执行，**When** 统计结果，**Then** `test_message_view_batch.py`（8 例）已移除，`test_live_selection_controller.py` 中批量用例已清理，其余 0 失败。

### FR-04 移除右键菜单写操作

**描述**：移除 LIVE 页消息列表右键菜单中的 6 项写操作（导出/删除/标记已读/收藏/标签/备注）。这是 β 方案的关键一致性动作，用户未逐项点名，按"只读看板"定位一并处理（见 §14 待确认 A2）。

**业务规则**
- FR-04-R1 删除 `controllers/live_actions_controller.py` 的 `show_context_menu()`（`live_actions_controller.py:119-180`）及其注入（`main_window.py:238-239`）与接线（`:319-332`）。
- FR-04-R2 右键点击消息可以仍弹出系统默认菜单（若 Qt 默认行为存在），但禁止出现 tgmonitor 自定义的 6 项操作。
- FR-04-R3 若 `LiveActionsController` 移除后无其它职责，应整体删除该文件。

**验收标准**
- **Given** LIVE 页有消息，**When** 右键点击任意消息，**Then** 不出现"导出…/删除/标记已读/★收藏/🏷标签/📝备注"菜单项。
- **Given** 全套测试执行，**When** 统计结果，**Then** 右键菜单相关用例已清理或更新，其余 0 失败。

### FR-05 移除详情面板导出 / 删除

**描述**：`MessageDetail` 底部按钮行只保留"复制文本"与"关闭详情"。

**业务规则**
- FR-05-R1 移除 `message_detail.py:252-262` 的 `_btn_export`/`_btn_delete` 及其 handler（`:322-338`）、两个信号 `export_current_requested`/`delete_current_requested`（`:89-90`）。
- FR-05-R2 移除 `main_window.py:352-357` 的对应接线；`LiveSelectionController.on_live_export_current`/`on_live_delete_current`（`:253-282`）随之清理。
- FR-05-R3 保留 `copy_text_requested` 信号与"复制文本"按钮（`:264-268,340-348`），其为空文本时置灰的行为不变。
- FR-05-R4 详情面板的消息内容区（正文/媒体/原始 JSON/媒体点击查看大图）完全不变。

**验收标准**
- **Given** 点击一条消息打开详情，**When** 观察底部按钮行，**Then** 仅有"📋 复制文本"与"关闭详情"，无导出/删除。
- **Given** 消息文本为空，**When** 打开详情，**Then** "复制文本"按钮置灰，点击无效果。
- **Given** `tests/test_live_message_detail_e2e.py` 执行，**When** 断言浮条可点击，**Then** 测试已更新为新的按钮集合并通过。

### FR-06 孤儿代码与测试清理

**业务规则**
- FR-06-R1 删除 `widgets/search_bar.py`、`controllers/search_controller.py`、`widgets/selection_toolbar.py`（若 FR-04 整体移除则含 `controllers/live_actions_controller.py`）。
- FR-06-R2 `LiveSelectionController` 若因 FR-03/04/05 清空全部入口，应整体删除；但 `on_live_copy_text` 若仍被复用则保留最小实现或内联到详情面板。
- FR-06-R3 移除 `EmojiPickerDialog` 的 LIVE 入口；若设置页表情配置仍使用它则保留 widget 本身（需确认，见 §14 待确认 A3）。
- FR-06-R4 更新 `AGENTS.md` 中涉及搜索/选择工具条/右键菜单的描述；`CHANGELOG.md` 记录 2.0.2 条目（与版本 bump PR 协同）。
- FR-06-R5 不删除 core 层任何公开方法；孤儿方法登记入 §14。

**验收标准**
- **Given** 全库 grep，**When** 搜索 `SelectionToolbar`/`SearchBar`/`focus_search`/`selection_count_changed` 等标识符，**Then** src 下无生产代码引用（测试中的历史提及除外）。
- **Given** `ruff` 与 `mypy` 执行，**When** 检查全库，**Then** 0 错误 0 警告。

---

## 八、非功能需求

| 类别 | 要求 |
|---|---|
| 性能 | 启动加载到视图填充 ≤ 现有基线（100 条应快于原 200 条）；滚动窗口截断保持 O(1) 均摊。 |
| 稳定性 | 不得引入新的 Qt 线程/事件循环问题；沿用 2.0.x 共享 loop 契约，全套测试无 SIGSEGV/hang。 |
| 兼容性 | 仅改 UI 层；不破坏 SQLite/Postgres 双后端、不破坏 CLI（`sync`/`monitor`）路径。 |
| 可维护性 | 移除后 src 下无死引用；每个删除的 widget 都有对应的测试清理，不留"测试孤儿"。 |
| 可访问性 | 头栏仍为纯文本 + 按钮，保留键盘可达；移除搜索不改变其余控件的 Tab 顺序合理性。 |

---

## 九、业务流程与状态流转

**LIVE 页状态流转（本期后）**

```
启动 → 读已订阅频道
        ├─ 无订阅 → no_subscribed 空态（提示去频道页订阅）
        └─ 有订阅 → list_messages(limit=100) → 视图填充(≤100 条)
                       ↑ EventBus 增量(injected after save)
                       │  MessageReceived → append(满 100 删最旧)
                       │  MessageEdited   → 局部刷新
                       │  MessageDeleted  → 删行
用户点击消息 → MessageDetail 展示(读操作: 复制文本 / 关闭)
```

**无写操作分支**：本期后 LIVE 页不存在"选中→批量操作"或"详情→导出/删除"的状态迁移。

---

## 十、数据与埋点需求

- **数据模型**：无变更。消息 DTO、存储 schema、事件定义均不变。
- **配置项**：无新增/删除。经核对 `settings_store.py:104-150` 与 `.env.example`，三块功能区无专属配置字段；`limit=100`/`MAX_ITEMS=100` 为代码常量，不暴露为设置项（与 2.0.1 的 `limit=200` 处理方式一致）。
- **埋点**：本项目无埋点体系，本期不引入。

---

## 十一、权限与安全要求

- 无新增权限模型、无 PII 处理变更。
- 安全收益：移除 LIVE 页"删除"入口降低了误删风险；删除操作仍可从其它入口触发（如有），需在生产数据中审慎（本期不改 core，故删除能力的可见性下降但能力本身仍在——见 RK-04）。

---

## 十二、异常处理清单

| 编号 | 异常场景 | 处理 |
|---|---|---|
| EC-01 | 视图满 100 条时新消息到达 | 删除最旧 1 条，追加新消息，视图恒 ≤100 |
| EC-02 | 无订阅频道 | 显示 `no_subscribed` 空态，不触发空查询刷屏 |
| EC-03 | 消息文本为空时点"复制文本" | 按钮置灰；handler 内二次早 return（保留现有双保险） |
| EC-04 | `MessageEdited`/`MessageDeleted` 事件在消息滚出窗口后到达 | 现有增量逻辑对"不在视图中的消息"应为 no-op；若存在异常需在本期回归中修复 |
| EC-05 | 旧配置/旧测试仍引用已删除的信号 | 由 `ruff`/`mypy`/全套 pytest 拦截，CI 门禁保证 |

---

## 十三、上线计划、依赖与验证

### 13.1 依赖评估

| 依赖 | 就绪状态 | 分级 | 最晚就绪时限 | 影响/替代 |
|---|---|---|---|---|
| 用户确认三个待确认假设（§14 A1-A3） | 待确认 | **阻塞**（阻塞定稿，不阻塞先动手改 UI 骨架） | 提测前 | 若假设被推翻，回滚对应子项即可，不影响其余 FR |
| 2.0.1 测试基线 | 已就绪（`1450 passed / 59 skipped / 63 deselected`） | 并行 | — | 作为回归对照 |
| CI 三平台门禁 | 已就绪 | 可绕过 | — | 本地先跑全套，CI 三平台复验 |

### 13.2 实施顺序（建议拆 2 个 PR）

- **PR-1 主体功能减法**：FR-01 ~ FR-05 的 UI 改动 + FR-02 的常量调整 + 对应测试更新。
- **PR-2 清理与文档**：FR-06 的死代码清理 + `AGENTS.md` 同步 + 版本 bump / CHANGELOG（可与 bump PR 合并）。

### 13.3 上线后验证

- 本地：全套 pytest + `ruff check` + `mypy`，记录新的通过数基线。
- CI：三平台（ubuntu/macOS/Windows）8/8 全绿。
- 人工冒烟：启动 → LIVE 页看 ≤100 条 → 点消息看详情无导出/删除 → 右键无写操作菜单 → 头栏无搜索框 → 退出无崩溃。

---

## 十四、风险与遗留问题

### 14.1 已确认决策（2026-11-16 用户逐项拍板）

| 编号 | 决策 | 依据 |
|---|---|---|
| **A1 ✅** | 顶部只移除 `SearchBar`，**保留**状态徽章与登录/登出 | 登录/登出是运行必需（未登录引导依赖 `btn_action`）；用户确认 |
| **A2 ✅** | 右键菜单 6 项写操作**一并移除** | β 定位"写操作入口 = 0"；用户确认 |
| **A3 ✅** | 详情**保留"复制文本"**，只删导出/删除 | 复制是只读操作，不违背定位；用户确认 |

三个假设均已确认，按上表实施；不再有"待确认"阻塞项。

### 14.2 风险登记册

| 编号 | 风险 | 类别 | 概率 | 影响 | 等级 | 应对 |
|---|---|---|---|---|---|---|
| RK-01 | 移除搜索后**历史消息检索无入口**，用户可能需要查旧消息 | 体验 | 4 | 3 | 12 中 | 提测前与用户确认；保留 Dashboard 导出能力作为兜底；记录后续独立检索页触发条件 |
| RK-02 | "最近 100 条"口径理解偏差（是"启动加载 100"还是"视图恒 100"） | 需求 | 3 | 3 | 9 中 | PRD 已按两者都做（FR-02-R1+R2）；若用户只要其一，删减对应子项 |
| RK-03 | 选择模式改 `SingleSelection` 影响已有批量测试假设 | 技术 | 3 | 2 | 6 低 | 测试随改；CI 拦截 |
| RK-04 | core 层删除能力仍在（孤儿方法），只是 UI 不可见 | 安全/技术 | 2 | 2 | 4 低 | 本期登记不清理；后续单独 PR 评估 |
| RK-05 | `MessageService.delete_message` 已是 UI 孤儿（详情实际走 `batch.delete_messages_batch`），移除后进一步孤儿化 | 技术 | 3 | 1 | 3 低 | 不清理，登记即可 |
| RK-06 | 删除大量 UI 代码触发 Qt 测试稳定性波动 | 技术 | 2 | 3 | 6 低 | 沿用共享 loop 契约；本地多跑几轮全套 + CI 三平台复验 |

### 14.3 遗留问题（明确不在本期）

1. 历史检索入口（RK-01）。
2. core 孤儿方法清理（RK-04/RK-05），包括 `MessageService.delete_message`、`BatchService.mark_messages_read`、`BatchService.add_reaction` 等。
3. 版本 bump 与 CHANGELOG 正式条目。
4. 生产侧历史 segfault 治理（承接 2.0.1 遗留，非本期范围）。

---

## 十五、附录

### 15.1 需求追踪矩阵（RTM）

| 原始诉求 | PRD 编号 | 功能需求 | 主要验收 |
|---|---|---|---|
| R-01 移除顶部搜索等功能区 | §七 FR-01 | FR-01-R1~R3 | `test_live_readonly_2002.py::test_header_has_no_search_bar` |
| R-02 实时流显示最近 100 条 + 移除全选/标记/表情 | §七 FR-02 / FR-03 / FR-04 | FR-02-R1~R5 / FR-03-R1~R4 / FR-04-R1~R3 | `test_max_items_is_100` / `test_live_view_is_single_selection_and_no_selection_toolbar` / `test_no_custom_context_menu_on_live_view` |
| R-03 移除详情导出/删除 | §七 FR-05 | FR-05-R1~R4 | `test_message_detail_is_readonly_except_copy` |

### 15.2 关键用户故事

- 作为**监听运营者**，我想打开应用就看最近 100 条已落地消息，以便快速确认各频道监听状态。
- 作为**监听运营者**，我想点开消息只读内容与复制文本，而不会被导出/删除按钮干扰或误触。
- 作为**监听运营者**，我不希望 LIVE 页上有用不上的搜索框和批量按钮，以便页面回归单一职责。

### 15.3 代码事实速查（诊断依据）

| 事实 | 位置 |
|---|---|
| `HeaderBar` 含标题/搜索/状态/登录登出 | `ui/widgets/header_bar.py:34-61` |
| `SearchBar` 6 信号 | `ui/widgets/search_bar.py:54-59` |
| 搜索接线 + 300ms debounce | `ui/main_window.py:228-230,295-309` |
| `SelectionToolbar` 9 按钮；全选/反选为死信号 | `ui/widgets/selection_toolbar.py:20-29,46-93` |
| 启动 `limit=200` | `ui/viewmodels/_messages.py:169` |
| `MAX_ITEMS=10000` | `ui/widgets/message_list_model.py:183` |
| `MessageReceived` 在落库后发布 | `core/monitor/service.py:324→332` |
| 右键菜单 6 项 | `ui/controllers/live_actions_controller.py:119-180` |
| 详情导出/删除按钮 | `ui/widgets/message_detail.py:252-262` |
| 详情复制文本 | `ui/widgets/message_detail.py:264-268,340-348` |
| 无专属配置字段 | `core/settings_store.py:104-150`、`.env.example` |

### 15.4 合规声明

本方案为产品设计参考，具体实施请结合公司合规与法务要求确认。
