# PRD — tgmonitor 2.0.6「频道管理归位与设置页收敛」版本规划

## 一、文档信息

| 项 | 内容 |
|---|---|
| 版本 | v1.4（全局 emoji 并入本期，评审通过，可进入实施） |
| 目标版本 | tgmonitor 2.0.6 |
| 基线版本 | 2.0.5（`e7dfb03`，工作树版本 `pyproject.toml:3` = 2.0.5；tag `v2.0.5` 截至起草时尚未打） |
| 作者 | 司南（产品经理 Agent） |
| 需求方 | forcetone |
| 日期 | 2026-11-18 |
| 状态 | **已评审通过**（T1 已确认；A1=β / A2=α / A3=β 采纳推荐；诉求 4「命名去歧义」扩为全改，D5 翻案；L1/L3/L4 并入本期；**D7=α 全局 emoji 并入本期，采用四层分类 + 静态守卫 + 分批 PR 保证 review/回归可控**）；可进入实施 |

**变更记录**

| 版本 | 日期 | 变更摘要 |
|---|---|---|
| v1.0 | 2026-11-18 | 初稿。基于 e7dfb03 只读代码诊断（导航/频道页/双字段模型/设置页/契约测试五组证据，均带文件:行号），把 3 条诉求归并为 6 条 FR，识别 3 个决策点 + 1 个术语确认项并给出推荐，展开完整 PRD。 |
| v1.1 | 2026-11-18 | 用户澄清：术语在 v2.0.3 已澄清——订阅=TG 侧已加入（`tg_subscribed`）、监听=monitor（白名单/物理列 `subscribed`）。T1 由「待确认」转为「已确认」；RK-01 术语歧义风险关闭；需求 2 数据源锁定 `list_joined_channels()`。 |
| v1.2 | 2026-11-18 | 用户确认 A1=β / A2=α / A3=β（采纳全部推荐），并新增诉求 4：把方法名/变量名中有歧义的命名改掉。新增 FR-07（命名去歧义，三档分级 + 决策点 D4/D5）、对应风险 RK-08/09、RTM 行与 PR 切分；范围声明与非目标同步放开「core 方法名可重命名」。 |
| v1.3 | 2026-11-18 | 用户决策「趁目前没有别人关注使用，把历史问题全部改掉」。基于第二轮只读诊断（存储层 DDL/迁移/默认值/测试 fixtures 全量核查）：**D5 翻案为全改**——物理列 `subscribed`→`monitored`、存储 ABC 两方法 + 形参同步正名；新增 FR-08（存储语义一致性 + 本机旧库一次性列改名迁移，因自愈补列会静默制造新旧列分裂且 SQLite/PG 默认值反向）、FR-09（历史债扫尾 L1 死代码桥接 + L3 过时注释）；L4 关闭并入本期；L2 全局 emoji 列为决策点 D7。RK-10 新增、RK-07 关闭口径、RTM 加 R-07/08/09、PR 切分 5→7。 |
| v1.4 | 2026-11-18 | 用户拍板 **D7=α：全局 emoji 并入本期**（「我相信你不会让 review 和回归失控」）。基于第三轮只读诊断（src 43 文件 254 行 + tests 27 文件 214 行 emoji 全量核查）：把「全局清理」拆为**四层**——A 零风险批量（注释/docstring/QSS 注释/文档约 380 行）、B 改了需同步测试（UI 装饰约 90 行）、C 改了改变产物需谨慎（导出模板 + 服务消息文本约 60 行）、**D 禁止清理（Telegram 用户真实 reaction emoji，属领域数据）**；新增静态守卫测试 `test_no_decorative_emoji.py` 白名单逐批收紧、PR-6 细分为 6a–6d 分批；FR-09 扩围、G9 重写、RK-13/14 新增、RTM R-09 细化。 |

**评审记录**：
- 第 1 轮（2026-11-18）：T1 术语口径用户已明确，A1/A2/A3 用户答复「可以」——全部采纳推荐方案（A1=β / A2=α / A3=β）。
- 第 2 轮（2026-11-18）：新增诉求 4「命名去歧义」，新增 FR-07 与 D4/D5 两个决策点并给出推荐。
- 第 3 轮（2026-11-18）：用户拍板「趁没人用把历史问题全部改掉」——D4 全档做、D5 翻案为全改、L1/L3/L4 纳入本期；D7（全局 emoji）为唯一保留的可裁剪项，已给推荐。
- 第 4 轮（2026-11-18）：用户拍板 **D7=α（全局 emoji 并入本期）**，并授权「按你的判断保证 review 与回归不失控」→ 采用四层分类（A 零风险 / B 同步测试 / C 产物谨慎 / D 领域数据禁止）+ 静态守卫白名单逐批收紧 + PR-6 拆 6a–6d，每批独立可回退。

> 诊断方式声明：本文所有「现状」结论均来自对 `e7dfb03` 工作树的只读排查，未修改任何源码、未运行测试。文中「改动锚点」是实施落点建议，非已落地事实；尺寸/顺序等为建议值，实施时按真机视觉微调。多处代码注释仍写「5 页 / 5 Tab / 8 个 FormSection」，与当前 4 项实现不符，本文一律以**代码实际实现**为准（证据见附录 A-1）。

---

## 二、背景与目标

### 2.1 背景

2.0.4 把实时流并入大盘（导航 5→4）、2.0.5 完成大盘视觉统一后，应用形成 4 个导航页：`大盘 / 频道 / 媒体 / 设置`（`nav_bar.py:35`）。实际使用暴露三处问题：

- **频道页职责与其他页重叠、入口过深**：频道页（`channel_widget.py:347`，605 行）同时承载「已加入频道」「已监听白名单」两张卡 + 搜索 + 刷新 + 全量同步（`channel_widget.py:382-422`），是唯一管理监听白名单的地方。用户每次调整监听都要从工作台切到独立页，路径长；而这套能力本质是「配置项」，与设置页心智更贴合。
- **「订阅 / 监听」双字段模型已在 v2.0.3 澄清，本期遵循既有口径**：**订阅** = TG 侧已加入的频道（`tg_subscribed`）；**监听** = 本应用收该频道消息 = monitor 白名单（物理列 `subscribed`），术语权威定义在 `channel_service.py:18-20`、`dto.py:21-39`。需注意方法名 `subscribe_channel` 做的是「开监听」而非「写 TG 订阅」（`channel_service.py:80-91`）——口径本身无歧义，只是 API 命名沿用历史。需求 2「从已订阅的频道中选择频道进行监听」据此明确为：**数据源取 TG 已加入频道（`list_joined_channels`），操作是开监听白名单**。
- **设置页视觉杂乱、emoji 泛滥**：6 个分组标题全部以 emoji 开头（`settings_page.py:160,252,283,320,391,431`），测试连接结果弹窗另有 4 处（`settings_page.py:723,725,727,739`）。分组按「凭证→代理→存储→对象存储→策略→同步」排列（`:117-123`），高频的「登录 / 监听」不突出，便携度不足。
- **代码命名残留历史歧义**：术语口径虽已澄清，但大量方法名/变量名仍用 subscribe 词根表达「监听」语义——`ChannelService.subscribe_channel`（实为开监听，`channel_service.py:80`）、`MonitorService.subscribed_ids`（实为监听白名单，`monitor/service.py:153`）、`message_service.include_unsubscribed`（实为 include_unmonitored，`message_service.py:54`）等；存储层更留有物理列 `subscribed`（`sqlite_repo.py:63`）、ABC 方法 `set_channel_subscribed`（`repository.py:135`）及默认值漂移（`_lifecycle_pg.py:53` DEFAULT FALSE vs `:60` DEFAULT TRUE）。读代码时需反复在脑中翻译，是新人与维护的持续认知负担。（诉求 4，v1.2 新增；v1.3 扩为含存储层全改）
- **清理时机已成熟（v1.3）**：用户 2026-11-18 明确「趁目前没有别人关注使用，将这些历史问题全部改掉」——此前 D5 因「存量用户数据迁移风险」而否决物理列改名，该前提现已不成立；技术债（L1 兼容桥接、L3 过时注释、L4 存储正名）一并纳入本期一次性清偿，避免长期背着「知道是错的但不敢动」的包袱。
- **全局 emoji 亦纳入（v1.4，D7=α）**：用户追加拍板把 L2 全局 emoji 并入本期，并授权按 agent 判断控制 review/回归。第三轮诊断确认 emoji 并非同类——**Telegram reaction emoji（🔥👍❤️）是用户真实表情数据，绝不可清理**；需按「装饰性 vs 领域数据」分层处理，详见 §5.8 与 FR-09。

### 2.2 要解决的问题

四条诉求本质是三件事：

1. **功能归位**：把监听频道管理从独立「频道」页搬进设置页，让设置页成为「所有配置」的唯一入口（符合 `nav_bar.py:9` 对设置页的定位声明）；同时取消频道页，导航做减法。
2. **风格收敛**：去掉设置页 emoji，重排分组顺序与展示层次，提升设置效率——且只动设置页，不牵连全局风格。
3. **命名正名**：把「频道域里 subscribe 词根实际指监听」的标识符按 v2.0.3 既定术语全量改为 monitored 词根，消除代码里的持续翻译成本；严格圈定范围，不误伤 EventBus 的「订阅事件」语义（v1.3 起含存储 ABC 方法与物理列，配套一次性列改名迁移）。
4. **历史债清偿（v1.3，v1.4 扩围）**：趁无外部用户，把此前因迁移风险搁置的存储层正名（L4）、死代码兼容桥接（L1）、过时注释（L3）、全局装饰性 emoji（L2，D7=α）一并清掉；**严格区分装饰性 emoji 与 Telegram reaction 领域数据**，后者不动。

### 2.3 目标与成功指标（口径明确）

| 目标 | 成功指标 | 口径与来源 |
|---|---|---|
| G1 频道页取消 | 导航项从 4 → 3，且 `len(win.nav._buttons)==3`、`win.stack.count()==3`；`_NAV_LABELS_SRC == ("大盘","媒体","设置")`；stack 中不再含任何 `ChannelWidget` 实例 | UI 自动化断言（改造既有 `test_dashboard_layout_2004.py:27-34,94-99`） |
| G2 监听管理可用 | 设置页内嵌控件可从 TG 已加入频道列表选择 → 开启监听（写白名单），可双击移出监听；操作后 `app.channels` 白名单与 core 存储一致 | UI + core 联动断言：`monitor_channel` / `unmonitor_channel`（原 `subscribe_channel` / `unsubscribe_channel`，本期改名 FR-07）被调用、`monitor.monitored_ids` 同步（复用 `test_subscription_monitoring_2003.py` 口径） |
| G3 能力不丢失 | 频道页原有 6 项能力（刷新已加入 / 搜索过滤 / 双击订阅 / 双击退订 / 全量同步 / 事件驱动标题头像更新 + 未登录禁用）在设置页逐项可用 | 逐条 GWT 验收（FR-02）+ 迁移前后能力清单对比（§9） |
| G4 设置页风格 | `settings_page.py` UI 文案中的 emoji 命中数为 0（6 标题 + 4 弹窗）；分组顺序调整落地；`objectName="pageTitle"` 与全局 QSS 规则保持 | 静态扫描 emoji 区间 + UI 文案断言 + 既有 `test_dashboard_header_panels_2005.py:47-51` 保持绿 |
| G5 索引顺延 | 媒体页 idx 2→1、设置页 idx 3→2；依赖导航索引的 debounce/测试断言同步 | `test_lifecycle_controller.py:130-162` 改造后断言 |
| G6 无回归 | 全套 pytest 通过（扣除本版刻意调整的契约断言数）；ruff + mypy 绿 | CI 8/8 + 本地全套 |
| G7 命名去歧义（v1.2，v1.3 扩为全档） | D4 全档 + D5 存储层的标识符全部改为 monitored 词根；`src/` 中「频道域 + subscribe 词根 + 语义=监听」的残留命中数为 0（白名单见 FR-07）；EventBus `subscribe*` 与 TG 订阅 API 零误伤；物理列 `subscribed`→`monitored`、存储 ABC 两方法与形参同步正名 | 静态扫描 + 全量测试绿；关键词白名单断言（附录 A-8/A-11） |
| G8 存储语义一致（v1.3 新增） | SQLite/PG 的 `monitored` 列默认值统一为 FALSE/0；DTO 映射默认值由 `True` 改为 `False`；本机旧库经一次性列改名迁移后监听状态保留，无「新旧列分裂」 | 双后端启动读写验证 + 旧库迁移测试（FR-08 GWT） |
| G9 历史债清偿（v1.3，v1.4 扩为全局 emoji） | ChannelWidget 兼容桥接属性清理；全局「5 页 / 5 Tab / 8 个 FormSection」过时注释归零；**全局装饰性 emoji 按四层策略清除（FR-09），Telegram reaction 领域数据 emoji 零误伤** | grep 过时注释归零 + 静态守卫 `test_no_decorative_emoji.py` 绿（白名单仅剩 D 层）+ 全套 pytest（FR-09 GWT） |

**非目标（本期明确不做）**：不改 DTO 字段名（已正名）、不改事件名（已正名 `ChannelMonitored`）、不改 EventBus 订阅语义与 `subscribe_updates`；不新增埋点或数据字段；不做版本 bump 与 CHANGELOG 发布条目（按 2.0.4/2.0.5 惯例单独 PR）；不改消息流/大盘/媒体页视觉。**存储 schema/物理列本期改为 `monitored`（v1.3 翻案，见 D5/FR-08）——但仅限频道域监听列，不引入通用 migration 框架。** **全局 emoji 清理的边界：仅装饰性用途；Telegram reaction emoji（用户真实表情，如 🔥👍❤️🎉 在 `ReactionDTO.emoji` / 贴纸 `emoji` / 服务消息 reaction 中的值）与消息正文里的用户 emoji 是领域数据，一律不动（D 层，见 FR-09）。**
> 版本演进：v1.0「不改 core 任何公开 API」→ v1.2 放开 core 频道域方法名 → v1.3 进一步放开存储 ABC 方法名 + 物理列（用户「历史问题全部改掉」），EventBus 与 TG 订阅语义始终不动。全局 emoji（L2）是否纳入本期由 D7 决定。

---

## 三、名词与术语定义

| 术语 | 定义 | 代码映射 / 证据 |
|---|---|---|
| 订阅（= TG 侧已加入） | 该 Telegram 账号在 TG 侧加入/订阅的频道。**用户口径已确认（2026-11-18）** | `is_tg_subscribed` / 列 `tg_subscribed` / `list_joined_channels()`（`dto.py:39`、`channel_service.py:64-72`） |
| 监听（= monitor / 白名单） | 本应用是否接收该频道消息 = 白名单 = 物理列（本期由 `subscribed` 改名为 `monitored`）。**用户口径已确认（2026-11-18）** | `is_monitored` / `set_channel_monitored()`（原 `set_channel_subscribed`，本期改名）/ `monitor_channel()`（原 `subscribe_channel`，本期改名）（`dto.py:36`、`channel_service.py:80-91`、`repository.py:135`） |
| 事件订阅（EventBus） | 订阅领域事件 / Telegram 更新流，与频道监听无关，**语义正确不改名** | `EventBus.subscribe*` / `subscribe_updates`（`events.py:443`、`telegram/client.py:141`） |
| ChannelWidget | 频道页的 605 行面板类，双栏 = 已加入 + 已监听，含搜索/刷新/同步/事件订阅 | `channel_widget.py:347` |
| ChannelListCard | ChannelWidget 内部双栏通用卡片组件（title / 搜索过滤 / 多选 / empty_hint / action 按钮） | `channel_widget.py:151` |
| FormSection | 设置页表单分组基类，字段走 `collect()` / `load()`，写入 `EditableSettings` → `.env` | `settings_page.py:517-598` |

> **术语口径（v2.0.3 已澄清，用户 2026-11-18 确认）**：**订阅 = TG 侧已加入**，**监听 = monitor 白名单**。方法 `subscribe_channel` / `unsubscribe_channel` 因历史命名沿用了 subscribe 词根，但自 v2.0.3 起其 docstring 与事件名（`ChannelMonitored` / `ChannelUnmonitored`）已明确语义为「开 / 关监听」，操作的是白名单而非 TG 订阅标志（`channel_service.py:80-107`）。本文档「订阅」一律指 TG 侧已加入，「监听」一律指 monitor 白名单，与代码术语定义一致。

---

## 四、需求范围

### 4.1 包含（本期做）

- FR-01：设置页新增「监听频道管理」区块，内嵌完整双栏能力（复用 ChannelWidget），写入 core 白名单。
- FR-02：从导航与页面栈移除「频道」页，导航 4→3，索引顺延。
- FR-03：迁移 ChannelWidget 实例所有权与信号接线到设置页，保证行为耦合不丢。
- FR-04：设置页去除全部 emoji（6 标题 + 4 弹窗），统一为纯文字文案。
- FR-05：设置页排版 / 展示 / 便携度优化（分组顺序、层次、间距、高频项前置）。
- FR-06：测试与契约同步（导航数量/索引/channel_panel 耦合/emoji 文案/新增设置页监听能力契约）。
- FR-07（v1.2，v1.3 扩为全档）：频道域「subscribe 词根实际指监听」的标识符全量去歧义，按 monitored 词根重命名，含 core/VM/UI 私有成员 + 存储 ABC 方法 + 物理列；严格不误伤 EventBus 事件订阅域与 TG 订阅 API。
- FR-08（v1.3 新增）：存储语义一致性——物理列 `subscribed`→`monitored` 的一次性列改名迁移（本机旧库）、SQLite/PG 默认值统一、DTO 映射默认值对齐。
- FR-09（v1.3，v1.4 扩围）：历史债扫尾——ChannelWidget 兼容桥接属性清理（L1）、全局过时注释归零（L3）、**全局装饰性 emoji 四层清理（L2，D7=α）**；Telegram 领域数据 emoji 不动。

### 4.2 不包含（本期明确不做）

- 删除 ChannelWidget / ChannelListCard 类本身（本期只是「搬家」与「撤页」，类保留复用）。
- 改 DTO 字段名 / 事件名 / 白名单算法（均已正名或无需改）。
- 改 EventBus `subscribe*` / `subscribe_updates` 语义与 TG 订阅 API（`tg_subscribed` / `set_channel_tg_subscribed` / `count_tg_subscribed`）——事件订阅域与 TG 侧订阅，语义正确。
- 引入通用数据库 migration 框架 / 版本表（本期只做一次性列改名，不建迁移体系）。
- 新增监听频道数量上限、配额或裁剪逻辑（沿用 A3=不限量历史决策）。
- 版本 bump / tag / Release（单独 PR）。
- 全局 emoji（L2）：**本期纳入（D7=α，v1.4 拍板）**，按 FR-09 四层策略分批清理，D 层 reaction 数据除外。

---

## 五、方案对比与选型说明

### 5.1 决策点 D1（=A1）：取消频道页的力度

| 方案 | 做法 | 成本 | 风险 | 评价 |
|---|---|---|---|---|
| α 仅导航隐藏 | 从 `_NAV_LABELS_SRC` 去「频道」，stack 保留第 1 页 | 低 | 页仍在、内存/事件双实例、`stack.count()` 契约需特判 | 不彻底，弃 |
| **β 撤导航 + 撤栈，类保留复用（推荐）** | 导航 4→3；stack 删频道页；ChannelWidget 类迁入设置页复用 | 中 | 装配顺序需调整、契约断言需改 | **推荐**：功能搬家不丢能力，改动可控 |
| γ 彻底删除类与测试 | 删 ChannelWidget / ChannelListCard 及其 300+ 行测试 | 高 | 6 项能力要在设置页全部重写 | 成本最高，列后续 |

**推荐 β**：诉求 2 要求设置页能管理监听频道，本质就是 ChannelWidget 现有能力；撤页 + 复用是「零后端改动 + 最小前端重写」的收敛路径。α 留隐性页造成双实例事件消费，γ 是重写。

### 5.2 决策点 D2（=A2）：设置页监听管理的形态

| 方案 | 做法 | 贴合度 | 成本 | 评价 |
|---|---|---|---|---|
| **α 内嵌完整双栏 ChannelWidget（推荐）** | 设置页新增一个自定义 section，塞入 ChannelWidget 实例（含已加入/已监听/搜索/刷新/全量同步） | 高 | 低（复用） | **推荐**：用户「从已订阅频道中选择监听」= 上栏选→开监听，语义完全对齐 |
| β 单栏勾选列表 | 只列 TG 已加入频道 + checkbox 切换监听 | 中 | 中（重写控件） | 丢弃搜索/全量同步/事件更新，能力倒退 |
| γ 跳转对话框 | 设置页放一个按钮，点击弹管理对话框 | 低 | 中 | 多一层交互，不符合「归位到设置页」 |

**推荐 α**：保留 ChannelWidget 完整能力，作为设置页里一个固定高度的自定义分组（不走 FormSection，因为白名单写 DB、不写 `.env`；见 §5.4）。

### 5.3 决策点 D3（=A3）：风格优化范围

| 方案 | 做法 | 评价 |
|---|---|---|
| α 仅去 emoji | 只删 10 处 UI emoji，不动排版 | 不满足「排版/展示/便携度」诉求 |
| **β 设置页去 emoji + 排版/展示优化（推荐）** | 去 10 处 emoji + 分组重排 + 层次统一；只改 settings_page.py | **推荐**：对齐诉求字面范围，牵连最小 |
| γ 全局去 emoji | 连状态栏/媒体页/消息详情等 40+ 文件一起改 | 远超本期范围，测试面爆炸，独立立项 |

**推荐 β**：用户明确限定「设置页面风格优化」。全局风格属独立质量债， documented，不混入本期。

### 5.4 术语口径 T1（已确认，不再是待决项）

用户 2026-11-18 明确，且与 v2.0.3 既有术语定义（`channel_service.py:18-20`）一致：

- **订阅 = TG 侧已加入的频道**（`tg_subscribed`）。
- **监听 = monitor 白名单**（物理列 `subscribed`）。

因此需求 2「从已订阅的频道中选择频道进行监听」的数据源与操作均已锁定：

| 项 | 结论 |
|---|---|
| 上栏数据源（"已订阅的频道"） | `list_joined_channels()` / `refresh_joined_channels()` 拉取的 TG 已加入列表 |
| 用户操作 | 从该列表选中 → `monitor_channel()`（原 `subscribe_channel()`，本期改名）开启监听白名单 |
| 一致性 | 与现有频道页「上栏已加入 → 双击订阅到监听」语义完全一致（`channel_widget.py:394-409,571-582`） |

> v1.0 曾把此列为「待拍板歧义 / RK-01 高风险」，系过度解读——术语在 v2.0.3 早已澄清，仅 API 方法名沿历史。v1.1 据此关闭该风险项。

### 5.5 已确认组合与理由

确认组合 **T1=订阅=TG 已加入、监听=monitor（已确认）+ A1=β + A2=α + A3=β（采纳推荐）+ 诉求 4 全档全改 + 历史债清偿（v1.3）**：把监听频道管理「整块搬进设置页」（不是重写、不是隐藏），撤掉独立频道页，顺手把设置页做干净；同时把频道域 subscribe 词根（含存储方法 + 物理列）一次正名为 monitored，并清偿 L1/L3/L4 技术债。整体是**结构性归位 + 视觉收敛 + 命名/存储正名**：前三条诉求 core 后端零改动；诉求 4 改名不改行为，但物理列改名配套一次性本机旧库迁移（FR-08）。风险集中在 UI 装配、契约测试、改名漏改与旧库迁移正确性。

### 5.6 决策点 D4（=诉求 4）：命名去歧义的力度（v1.2，v1.3 定稿为全档）

只读诊断发现 `subscrib` 词根分属**两个语义域**：频道域的「订阅/监听」（有歧义，本期对象）与 EventBus/Telegram 的「订阅事件/更新流」（语义正确，**绝不能误伤**）。因此改名范围严格限定频道域，但**用户拍板「历史问题全部改掉」后，频道域内的必改/建议改/原不改档全做**：

| 档 | 标识符（举例） | 半径 | v1.3 结论 |
|---|---|---|---|
| **必改** | `message_service.include_unsubscribed` → `include_unmonitored`；UI 私有成员 `_subscribed_ids` / `lst_subscribed` / `set_subscribed` / `"no_subscribed"` → `*_monitored` | 单文件 4+3 行；UI 约 20 行集中 4 文件 | 做 |
| **建议改** | `ChannelService.subscribe_channel`/`unsubscribe_channel` → `monitor_channel`/`unmonitor_channel`；`MonitorService`/`WhitelistManager.subscribed_ids` → `monitored_ids`；VM handler `_on_channel_subscribed` → `_on_channel_monitored` | 公开方法 ~11 行 + tests；`subscribed_ids` src 15/7 文件 + tests 7/4 文件 | 做（不受 allowlist 约束，见 A-9） |
| **原不改档 → v1.3 全改** | 存储 ABC `set_channel_subscribed` / `list_subscribed_channels` + 形参；物理列 `subscribed` | 存储两方法 src 11/21 + tests 43/36 行；物理列 22 处字面量 | **做**（D5 翻案，配套 FR-08 迁移） |
| **始终不改** | EventBus `subscribe*` / `subscribe_updates` / `ChannelMonitored` / DTO 双字段 / TG 订阅 API | — | 语义已正确，零误伤 |
| **v1.5 追加（2026-11-19）** | UI `MonitorViewModel.known_channels` → `monitored_channels`（含媒体侧 `_known_channels` / `set_known_channels`）| src 15 文件 / tests 3 文件，95 处 | **做**：语义=仅已监听，`known` 名实不符且诱发左栏覆盖 bug；v1.4「名字中性不改」判断被推翻 |

**D4 定稿：频道域全档做，事件域零改动。** `subscribe_channel` 等公开方法与存储方法虽半径大，但正是用户点名的"方法名有歧义"核心，一次清干净；`test_app_service_surface.py` allowlist 不锁 ChannelService 方法名（A-9），改名安全。

### 5.7 决策点 D5：存储层与物理列是否改（v1.2 提「不改」，v1.3 翻案为「全改」）

| 方案 | 做法 | 风险 | v1.3 结论 |
|---|---|---|---|
| α 不改（v1.2 推荐） | 全部保留，靠注释 + DTO 正名 | 无 | **已被前提推翻**：用户明确「没人用，全改」，迁移风险前提消失 |
| **β 全改（v1.3 采纳）** | 物理列 `subscribed`→`monitored` + 存储 ABC 两方法与形参正名 + 一次性列改名迁移 + 默认值统一 | 中（迁移逻辑需正确，见 FR-08） | **采纳**：技术债一次性清偿，无外部用户兜底压力 |

**翻案依据**：v1.2 否决 β 有三条理由，v1.3 逐条失效——
1. ~~自愈迁移会反噬存量用户~~ → 无外部存量用户；本机库用一次性 `RENAME COLUMN` 迁移解决（FR-08），且**必须显式处理**，否则自愈只会 ADD 空列造成新旧列分裂（SQLite 补 NULL→监听清零 / PG 静态补列 DEFAULT TRUE→监听全开，方向相反，`sqlite_repo.py:489-512`、`_lifecycle_pg.py:59-60`）。
2. ~~半径全仓最大~~ → 半径客观存在（src about 11/21 行 + tests 43/36 行 + 22 处列字面量），但纯机械重命名 + 守门测试同步，无逻辑复杂度，可单 PR 回滚。
3. ~~收益有限~~ → 用户价值judgment：无外部用户正是清偿的最佳窗口，留着只会持续积累认知负担。

### 5.8 决策点 D7：全局 emoji（L2）是否纳入本期（v1.3 提出，v1.4 拍板 α）

| 方案 | 做法 | 结论 |
|---|---|---|
| **α 纳入本期（v1.4 采纳）** | FR-09 里连全局 emoji 一起清，但按四层分类 + 静态守卫 + 分批 PR 控制风险 | **采纳**：用户拍板「并进去吧」并授权按 agent 判断保证 review/回归可控 |
| β 本期不做，独立专项（v1.3 推荐） | 本期只清设置页；L2 保留后续专项 | 已被 α 取代 |

**采纳 α 的前提是第三轮只读诊断把「全局 emoji」拆成可管控的四层**（src 43 文件 254 行 + tests 27 文件 214 行命中，A-12）：

| 层 | 内容 | 规模 | 处置 |
|---|---|---|---|
| **A 零风险批量** | 纯注释 / docstring / QSS 注释 / 仓库文档 emoji | src 98 行 + docs/QSS ~280 行 | 纯文本 diff，不动字符串字面量，第一批批量做 |
| **B 需同步测试** | UI 装饰字面量（设置页 10 处、状态栏、行渲染图标、频道徽标、空态/表单图标等） | src ~90 行 / 21 文件；对应 tests 2a 清单 | 同 PR 改断言，分批按模块推进 |
| **C 产物需谨慎** | 导出模板（HTML 6 + MD 7 处）+ Telegram 服务消息文本 `_ACTION_TEXT`（45 行）+ 阶段图标字典 | ~60 行 | 改变导出结果/历史消息呈现，走产品可见变更评审，最后做 + 全量回归 |
| **D 禁止清理** | `ReactionDTO.emoji` / 贴纸 `emoji` / 服务消息 reaction 中的 🔥👍❤️🎉 等 | tests 大量断言 + src 字段 | **用户真实表情，属领域数据，零误伤**；静态守卫白名单永久放行 |

**边界字形说明**：`▶ ◀ ▼ → ✓ ★ ⏸` 等符号形字符未计入 emoji 码点区间（多为控制/数学符号）。v1.4 口径**不追求符号全清**——只清 emoji 码点区间内的装饰字符；`✓ ★` 等若与文字语义绑定（如收藏标记）按 C 层评审决定，不机械替换。

### 5.9 演进路径

- 本期（2.0.6）：功能搬家 + 撤页 + 设置页风格收敛 + 频道域命名全档正名（D4+D5）+ 存储语义一致性 + 历史债清偿（L1/L3/L4/L2 四层装饰 emoji）。
- 后续（触发条件明确再立）：① 删除 ChannelWidget 死代码路径（本期已清兼容桥接 L1，剩余类在稳定一个版本后评估）；② 边界字形（▶/✓/★）风格统一（独立设计专项，非本期）；③ 监听频道批量勾选/导入。

---

## 六、用户角色与使用场景

**用户画像**（基于需求描述构建，为待确认假设）：桌面端 tgmonitor 使用者，以「登录账号 → 配置监听频道 → 查看消息/统计」为主流程，对导航页数量敏感，偏好在单一设置入口完成所有配置。

| 场景 | 用户目标 | 操作路径（改造后） | 触点 |
|---|---|---|---|
| S1 首次配置监听 | 从自己 TG 已加入的频道里挑几个开始监听 | 设置 → 监听频道管理 →（必要时先「刷新」拉取）→ 双击已加入频道 → 出现在已监听 | FR-01/02 |
| S2 调整监听 | 取消某个频道监听 / 重新开启 | 设置 → 在已监听栏双击移出；或搜索后重新双击加入 | FR-01 |
| S3 全量同步 | 对已监听频道补历史消息 | 设置 → 监听频道管理已监听栏 → 全量同步… | FR-01/03 |
| S4 改配置 | 改代理/存储/同步参数、登录登出 | 设置页内完成，无需跨页 | FR-04/05 |
| S5 未登录 | 账号未就绪时查看 | 设置页监听管理区按钮禁用，登录后自动恢复（沿用频道页门控） | FR-03 |

---

## 七、功能需求（编号 + 业务规则 + GWT 验收）

### FR-01 设置页新增「监听频道管理」区块（T1 口径、A2=α 均已确认）

在 `SettingsPage` 的 `form_root`（`settings_page.py:112-115`）新增一个自定义分组（建议置于「账户凭证」之后），内嵌 `ChannelWidget` 实例。**不走 FormSection**（白名单写入 core 存储，不进 `EditableSettings`/`.env`；`settings_page.py:518-556` 的 collect 循环不覆盖白名单）。

- 业务规则：
  1. 上栏数据源 = TG 已加入频道（`channel_service.list_joined_channels()`，best-effort，未登录返空 `channel_service.py:64-72`）；提供「刷新」按钮调 `refresh_joined_channels()`（`:109-173`，含空返回保护）。
  2. 双击上栏某频道 → `app.channels.monitor_channel(dto)`（原 `subscribe_channel`，FR-07 改名）→ 写白名单 + 发 `ChannelMonitored`（`:80-91`）。
  3. 双击下栏（已监听）某频道 → `app.channels.unmonitor_channel(id)`（原 `unsubscribe_channel`）→ 移出白名单 + 发 `ChannelUnmonitored`（`:93-107`）。
  4. 保留搜索框过滤、empty_hint 引导文案（当前指向「频道页」的措辞需改为「设置页」，见 `channel_widget.py:403` 注释用法）。
- 验收标准（GWT）：
  - **Given** 已登录且有 TG 已加入频道，**When** 打开设置页，**Then** 监听管理上栏展示这些频道，下栏展示当前白名单。
  - **Given** 上栏有频道 X，**When** 双击 X，**Then** X 出现在下栏，`monitor.monitored_ids`（原 `subscribed_ids`）含 X 的 id，`ChannelMonitored` 被发布。
  - **Given** 下栏有频道 Y，**When** 双击 Y，**Then** Y 从上栏移除/下栏消失，白名单剔除 Y。
  - **Given** 搜索框输入关键字，**When** 文本变化，**Then** 上下两栏按标题/username 过滤（复用 `ChannelListCard.apply_filter`）。

### FR-02 从导航与页面栈移除「频道」页（A1=β 已确认）

- 业务规则：
  1. `_NAV_LABELS_SRC` 改为 `("大盘", "媒体", "设置")`，`_NAV_ICONS` 同步去掉 `nav_channels`（`nav_bar.py:35,49`）。
  2. `_nav_items()` 的 `range(4)` 改为 `range(3)`（`nav_bar.py:52-54`）。
  3. `ui_assembler.py` 删除 channels_page 容器与其 `stack.addWidget`（`:140-150`）；stack 索引顺延：大盘 0、媒体 1、设置 2。
  4. 删除/迁移 `channel_panel` 属性赋值（`main_window.py:436`、`controllers/_base.py:89`），改为指向设置页内嵌的那个 ChannelWidget 实例（见 FR-03）。
- 验收标准（GWT）：
  - **Given** 主窗口构建完成，**When** 读取导航，**Then** `len(nav._buttons)==3`、`stack.count()==3`、`_NAV_LABELS_SRC==("大盘","媒体","设置")`。
  - **Given** 主窗口，**When** 遍历 `findChildren(ChannelWidget)`，**Then** 恰好 1 个，且其父链在设置页内。
  - **Given** 导航点击，**When** 点索引 1/2，**Then** 分别显示媒体页/设置页（原 2/3）。

### FR-03 迁移信号接线与行为耦合（配套 FR-02，防能力丢失）

- 业务规则：
  1. `ChannelWidget.sync_requested` → `MainWindow._on_sync_requested` 的接线（`ui_assembler.py:291-293`）改接设置页内嵌实例，保证 FR-01 的「全量同步…」可用。
  2. `AppStateController` 的 `ctx.channel_panel.set_operations_enabled(state==READY_STATE)`（`app_state_controller.py:55`）保持有效：`MainWindowCtx.channel_panel` 重定向到设置页实例（`controllers/_base.py:89`），使未登录禁用/登录恢复不丢。
  3. `set_joined` / `set_subscribed` 由 `_refresh_state` 调用（`main_window.py:617-619`）改指向设置页实例。
  4. EventBus 四类事件订阅（`channel_widget.py:482-502`：ChannelMonitored/Unmonitored/TitleChanged/PhotoChanged）随实例迁移，且因频道页撤销，全局只剩单实例消费，不产生重复更新。
- 验收标准（GWT）：
  - **Given** 未登录态，**When** 触发登录态刷新，**Then** 设置页内监听管理的所有操作按钮禁用；登录 READY 后恢复（复用 `test_app_state_controller.py:321-344` 断言口径）。
  - **Given** 已监听频道非空，**When** 在设置页点「全量同步…」，**Then** 触发 `MainWindow._on_sync_requested` 与同步流程（原频道页行为不丢）。
  - **Given** 收到 `ChannelTitleChanged` / `ChannelPhotoChanged`，**When** 事件到达，**Then** 设置页内嵌控件对应行文本/图标更新一次（非两次）。

### FR-04 设置页去 emoji（A3=β 已确认）

- 业务规则：
  1. 6 个分组标题去掉前缀 emoji，改纯文字：`账户凭证` / `网络代理 (Proxy)` / `消息存储 (Database)` / `对象存储 (ObjectStore)` / `策略` / `同步参数`（`settings_page.py:160,252,283,320,391,431`）。
  2. 4 处测试连接结果弹窗文案去 emoji：`可达: …` / `超时: …` / `失败: …` / `异常: …`（`settings_page.py:723,725,727,739`）。
  3. 相关 docstring 中的 emoji 一并去除，保持源码风格一致（`:7,158,247,277,318,389,429`）。
  4. **保留** `objectName="pageTitle"` 与全局 `#pageTitle` QSS 规则（有契约测试 `test_dashboard_header_panels_2005.py:47-51` 守护），只改 `tr()` 文案，不改结构。
- 验收标准（GWT）：
  - **Given** settings_page.py，**When** 扫描 U+1F300–U+1FAFF / U+2600–U+27BF / FE0F，**Then** 命中数为 0（UI 文案与 docstring 均清零）。
  - **Given** 设置页，**When** 查看各分组标题，**Then** 文本为纯文字（无符号前缀），标题仍为 `pageTitle`/分组标题 objectName。

### FR-05 设置页排版 / 展示 / 便携度优化（A3=β 已确认）

- 业务规则（均为建议值，实施按真机微调；保留 `form_root` 现有 16px 间距与底部固定按钮栏 `settings_page.py:114,137-151`）：
  1. 分组顺序调整为：① 账户凭证（登录/登出入口）→ ② 监听频道管理（FR-01 新增）→ ③ 网络代理 → ④ 消息存储 → ⑤ 对象存储 → ⑥ 策略 → ⑦ 同步参数。高频项（登录、监听）前置。
  2. 标题层次统一为纯文字 + QSS 现有 FormSection 标题样式；不新增 QSS 规则去覆盖全局 `#pageTitle`。
  3. 登录/登出入口仍在「账户凭证」分组内部（`settings_page.py:194-221` 的 2.0.4 FR-02 结论不变），不回退到块外。
  4. 底部「仅保存到 .env」/「保存并应用」按钮常驻可见、文案保持纯文字。
- 验收标准（GWT）：
  - **Given** 设置页分组列表，**When** 读取顺序，**Then** 账户凭证为第 1 组、监听频道管理紧随其后为第 2 组。
  - **Given** 设置页任意滚动位置，**When** 查看底部，**Then** 两个固定按钮仍可见可点（沿用现有固定栏结构）。
  - **Given** 登录态切换，**When** 登录/登出按钮显隐，**Then** 仍归属账户凭证分组（不出现兄弟节点漂浮）。

### FR-06 测试与契约同步

- 业务规则：
  1. 改造 `test_dashboard_layout_2004.py`：nav/stack 数量 4→3、`_NAV_LABELS_SRC` 三元组、媒体 idx 1、频道页不存在断言。
  2. 改造 `test_lifecycle_controller.py:130-162`：`switch_tab` 与 `on_nav_changed` 索引（媒体 2→1、设置 3→2），debounce 触发索引随之改。
  3. 保留并重定向 `channel_panel` 相关 mock/断言：`test_app_state_controller.py:321-344`、`test_main_window_channels.py:482-483`（`win.channel_panel` 改指向设置页实例）、其余塞 `channel_panel=MagicMock` 的文件原则上不改。
  4. `test_channel_widget.py`（325 行）随类保留保持绿；`test_main_window_channels.py`（922 行）里的装配/索引断言按新布局调整，能力断言尽量保留。
  5. 新增契约测试文件：`test_settings_channel_management_2006.py`，守 G1（3 项导航）、G2（设置页双栏订阅/退订→白名单）、G4（emoji 清零）、G5（索引顺延）、G7（命名白名单扫描）、FR-03 的同步/门控接线。
  6. 新增/更新 emoji 静态扫描断言（可放进新契约文件）。
  7. 命名去歧义涉及的测试同步（FR-07）：`test_channel_service.py`、`test_message_service.py`、`test_app_service.py`、`test_operation_gate_2003.py`、`test_reconfigure.py` 等按新名更新；`test_app_service_surface.py` allowlist 保持不变。
  8. 存储层改名与迁移测试同步（FR-08）：`test_sqlite_repo.py`（DDL 守门 + 硬编码旧库 SQL）、`test_storage_backends.py`、`tests/integration/test_pg_repo_parity.py`、`tests/fixtures/_in_memory_repository.py` 按 `monitored` 新名更新，并新增旧库迁移测试。
- 验收标准（GWT）：
  - **Given** 全套 pytest，**When** 执行，**Then** 全绿（新增测试通过，改造断言与代码一致）。
  - **Given** 新契约测试，**When** 单独跑，**Then** G1/G2/G4/G5/G8/G9 各自可独立失败定位。

### FR-07 频道域命名全档去歧义（v1.2，v1.3 扩为全档：D4 全做 + D5 翻案全改）

严格限定在「频道域 + subscribe 词根 + 语义=监听」，EventBus 事件订阅域、已正名的事件/DTO 字段、TG 订阅 API 一律不动；但频道域内的 core 服务、VM、UI 私有成员、存储 ABC 方法与形参、物理列**全部改为 monitored 词根**（物理列改名与迁移细节见 FR-08）。

- 业务规则（完整改名表，半径见 §5.6）：
  1. **core 域服务**：`ChannelService.subscribe_channel` → `monitor_channel`、`unsubscribe_channel` → `unmonitor_channel`、`list_subscribed_channels` → `list_monitored_channels`；VM 同名转发方法（`viewmodels/_channels.py:166-180`）与 UI 调用点（`channel_widget.py:580,589`）同步，连带 `error_label` 字符串一并改。
  2. **monitor 属性**：`MonitorService.subscribed_ids` / `WhitelistManager.subscribed_ids` → `monitored_ids`；所有只读调用点（`dashboard_widget.py`、`export_controller.py`、`sync_controller.py`、`app.py`、`main_window.py` 等）同步；docstring「订阅频道 id」改「监听频道 id」（`monitor/service.py:154`、`whitelist.py:54`）。
  3. **message_service**：`include_unsubscribed` → `include_unmonitored`（含 docstring `:59-65`、测试 `test_message_service.py`）。
  4. **UI 私有成员**：`_subscribed_ids` / `lst_subscribed` / `set_subscribed` / `_add_to_subscribed_list` / `_remove_from_subscribed_list` / `_on_subscribed_double_click`（`channel_widget.py`）、`card_subscribed`（`dashboard_widget.py:251`）、`"no_subscribed"`（`message_empty_overlay.py:44` 等）、`status_bar.set_stats(subscribed=...)` → 对应 `*_monitored`；局部 `subscribed` 变量一并改。
  5. **VM handler**：`_on_channel_subscribed` / `_on_channel_unsubscribed` → `_on_channel_monitored` / `_on_channel_unmonitored`，含事件名字符串映射（`_channels.py:83-84`）与 `_messages.py:29,35`、`_system.py:51` 的注释引用。
  6. **存储 ABC 方法与形参（v1.3 翻案新增）**：`repository.py` 的 `set_channel_subscribed` → `set_channel_monitored`（形参 `subscribed` → `monitored`，`:135`）、`list_subscribed_channels` → `list_monitored_channels`（`:159`）；同步 `postgres_repo.py:156-174` facade、`sqlite_repo.py:722,765`、`_channels_pg.py:197,254`、`_in_memory_repository.py:137,194` 四实现及全部 src/tests 调用点；契约注释 `:136-140` 的「命名历史遗留、方法名保留」改为已正名。**不改** `set_channel_tg_subscribed` / `count_tg_subscribed`（TG 订阅语义正确）。
  7. **始终不改清单**：`EventBus.subscribe*` / `subscribe_updates` / `ChannelMonitored` / `ChannelUnmonitored` / `is_monitored` / `is_tg_subscribed` / `set_channel_tg_subscribed` / `count_tg_subscribed`。
  8. **v1.5 追加（2026-11-19，用户现场反馈触发）**：UI `MonitorViewModel.known_channels` → `monitored_channels`（媒体侧 `_known_channels` / `set_known_channels` 同步）；v1.4 把它列入「始终不改（名字中性）」是误判——该字段唯一数据源是 `list_monitored_channels()`，语义=仅已监听，名实不符且直接诱发本次「双击加入后左栏塌缩」bug。src 15 文件 / tests 3 文件共 95 处；`test_naming_monitored_2006.py` 增守卫防回潮。
- 验收标准（GWT）：
  - **Given** 改名完成，**When** 全仓扫描「频道域 subscribe 词根」关键词白名单，**Then** 除 §7 始终不改清单外命中数为 0（静态扫描断言，含物理列字面量）。
  - **Given** 改名完成，**When** 跑全套 pytest，**Then** 全绿；`test_app_service_surface.py` allowlist 不变仍绿（确认 `channels` 仅实例属性名、未锁 `ChannelService` 方法名）。
  - **Given** 事件 `ChannelMonitored` 发布，**When** VM 收到，**Then** `_on_channel_monitored` 被正确调用（字符串映射同步，无漏接）。
  - **Given** 改名完成，**When** 检查存储层，**Then** 物理列为 `monitored`（FR-08 迁移后），无残留 `subscribed` 列引用；本条依赖 FR-08 先落地。

### FR-08 存储语义一致性与一次性列改名迁移（v1.3 新增，D5=β 配套）

物理列 `subscribed`（语义实为监听）改名为 `monitored`，并消除诊断发现的默认值漂移。**关键陷阱**：现有 `init_schema` 为 `CREATE TABLE IF NOT EXISTS` + 按 `EXPECTED_SCHEMA` 补列（`sqlite_repo.py:489-512`），repair「只补列、不删列」（`repository.py:64-67`），不会 RENAME——若只改字符串不迁移，旧库会出现「旧 `subscribed` 列有数据、新 `monitored` 列为空」的静默分裂，且 SQLite 补 NULL→监听清零、PG 静态补列 DEFAULT TRUE→监听全开（`_lifecycle_pg.py:59-60`），方向相反。

- 业务规则：
  1. **DDL 改列名**：SQLite `SCHEMA_SQL`（`sqlite_repo.py:63` `subscribed INTEGER NOT NULL DEFAULT 0` → `monitored ... DEFAULT 0`）、PG CREATE（`_lifecycle_pg.py:53` `BOOLEAN ... DEFAULT FALSE`）、两处 `EXPECTED_SCHEMA` 列键（`sqlite_repo.py:190`、`_lifecycle_pg.py:241`）、全部 SQL 字面量（SQLite 9 处 + PG 12 处，见 A-11）与 DTO 映射键（`sqlite_repo.py:333`、`_channels_pg.py:39` 的 `row.get("subscribed", ...)` → `"monitored"`）。DTO 字段本身不动。
  2. **一次性列改名迁移**：在 `init_schema` 之后、首次读写之前，检测「存在 `subscribed` 列且不存在 `monitored` 列」时执行 `ALTER TABLE channels RENAME COLUMN subscribed TO monitored`（SQLite 3.25+ / PG 均支持），保留数据。**因无外部用户，不写通用 migration 框架、不建版本表**；本机旧库即此迁移的唯一适用对象。迁移后 `introspect_schema` 不应再报 `monitored` 缺失。
  3. **默认值统一**：`_lifecycle_pg.py:60` 的静态补列 `DEFAULT TRUE` 改为 `DEFAULT FALSE`（与 `:53` 及 SQLite 一致）；两处映射默认值 `row.get("monitored", True)` → `False`（`sqlite_repo.py:333`、`_channels_pg.py:39`），与 DTO 默认 `is_monitored=False`（`dto.py:36`）对齐。
  4. **测试同步**：`tests/test_sqlite_repo.py:81-96` DDL/EXPECTED_SCHEMA 守门（双边同步改仍绿）；`:229-234` 硬编码旧库 SQL 造库列名改 `monitored`；新增「旧库含 `subscribed` 列 → 启动迁移 → 监听状态保留」测试；PG parity 集成测试列名同步。
- 验收标准（GWT）：
  - **Given** 一份含已监听频道、列名为 `subscribed` 的旧库，**When** 新版本启动并完成迁移，**Then** `monitored` 列存在且原监听状态逐条保留，`introspect_schema().ok` 为真、无新旧列分裂。
  - **Given** 全新 SQLite / PG 库，**When** `init_schema` 后读取某频道，**Then** 未设置监听时 `is_monitored == False`（两后端、DDL 与映射默认值一致）。
  - **Given** 内存仓储（InMemory）与两持久化后端，**When** 跑 `test_storage_backends` / `test_pg_repo_parity`，**Then** 三者监听读写行为一致。

### FR-09 历史债扫尾与全局装饰 emoji 清理（v1.3，v1.4 扩围；L1 + L3 + L2=α）

- 业务规则：
  1. **L1 兼容桥接清理**：撤页后 `ChannelWidget` 仅设置页一处实例，`channel_widget.py:424-434` 的旧属性桥接（`lst_joined` / `btn_sync` 等兼容别名）若无调用点即删除；确认 `test_main_window_channels.py` 等不再依赖后移除。
  2. **L3 过时注释归零**：全仓 grep `5 个 Tab` / `5 页` / `8 个 FormSection` 归零（主要文件 `nav_bar.py:2-9`、`main_window.py:3,93`、`ui_assembler.py:6`、`settings_page.py:154`）；模块 docstring 方法数漂移（`_channels_pg.py:3-7` 称 11 实为 13、`sqlite_repo.py:3` 称 42 方法）、`repository.py:85` ChannelStore docstring 的「订阅标志」改「监听/双标志」一并更正。
  3. **L2 全局装饰 emoji 四层清理（D7=α，v1.4）**——先按 §5.8 四层分类逐层处置，每层独立 PR、独立可回退：
     - **A 层（零风险批量）**：纯注释 / docstring（src 98 行）、QSS 注释（`style.qss:230,613,668,677`）、仓库文档（`.github/*` / `README.md` / `CHANGELOG.md` / `AGENTS.md` / `docs/*` 等 ~280 行）；纯文本 diff，不动任何字符串字面量，第一批做。
     - **B 层（同步测试）**：UI 装饰字面量——设置页 10 处（`settings_page.py:160,252,283,320,391,431,723,725,727,739`）、状态栏（`status_bar.py:105,212,277,289,293,316`）、行渲染（`message_list_model_format.py:38,40,43,45,79,81,95,97,99,101`）、频道徽标（`channel_widget.py:145,147,389,401,405,418`）、空态/表单（`message_empty_overlay.py:45,50,55,66`、`form_row.py:240`）、状态圆点表（`state_labels.py:34-45,85,104`）、媒体图标表（`_mm_base.py:26-33,56-59`、`_mm_render.py:328`）、消息详情（`message_detail.py:135,195,220,242,265,317,319,321`）、灯箱（`lightbox_dialog.py:644,778,781`）、同步对话框（`sync_dialog.py:187,219-224`）、各 status 提示文案等；**同 PR 必改对应测试断言**（`test_format_meta_icons_order.py`、`test_message_view.py`、`test_channel_widget.py`、`test_form_row.py:206,219`、`test_status_bar.py:77`、`test_app_state_controller.py:154,155` 等，完整清单 A-13）。
     - **C 层（产物谨慎）**：导出模板 HTML（`html_exporter.py:58,72,78-80,89`）/ Markdown（`markdown_exporter.py:80,113,115,117`）+ Telegram 服务消息文本 `_ACTION_TEXT`（`telethon_messages.py:203-274` 及 `:322`，45 行）+ `channel_sync/service.py:163` 进度文本；改变导出结果 / 历史消息呈现，**单独一批走产品可见变更评审**，同步 `test_exporters.py:518,990-992,1029-1033`，最后做 + 全量回归。
     - **D 层（禁止清理）**：`ReactionDTO.emoji`（`dto.py`）、贴纸 emoji 字段、reaction 格式化与测试中作为用户真实表情的 🔥👍❤️🎉🚀 等（`test_reaction_format.py`、`test_telethon_client.py`、`test_storage_backends.py:598-599`、`test_monitor_vm.py` 等）——**零误伤**，静态守卫永久白名单放行。
  4. **静态守卫测试**：新增 `tests/test_no_decorative_emoji.py`，遍历 `src/**/*.py` 用 emoji 码点正则断言装饰性 emoji 归零；白名单随 A→B→C 分批**逐步收紧**，最终只保留 D 层领域数据例外（reaction 字段/参数）；QSS 与文档 emoji 不进该扫描范围（另用 grep 守 A 层）。当前仓库无任何 emoji 守卫（A-14），本条是"回归不失控"的核心保障。
- 验收标准（GWT）：
  - **Given** 撤页完成，**When** grep `lst_joined` / `btn_sync` 兼容别名，**Then** 生产代码中命中数为 0（或保留处均有明确调用点说明）。
  - **Given** 全仓，**When** grep 过时注释关键词，**Then** 命中数为 0；全套 pytest 绿。
  - **Given** A/B/C 三层完成，**When** 跑 `test_no_decorative_emoji.py`，**Then** src 装饰性 emoji 命中数为 0、D 层 reaction 字段/参数白名单放行、测试绿。
  - **Given** 任一批 emoji 清理，**When** 跑对应模块测试 + 全套 pytest，**Then** 全绿；B 层每批的断言修改与本批源码改动在同一 PR 内，不存在"改了 UI 未改断言"的红测窗口。
  - **Given** C 层导出模板清理完成，**When** 跑 `test_exporters.py`，**Then** 导出 HTML/MD 产物断言与新文案一致；服务消息 emoji 清理后历史消息呈现变化已在评审记录中确认。

---

## 八、非功能需求

| 类别 | 要求 | 口径 |
|---|---|---|
| 性能 | 设置页内嵌 ChannelWidget 后，打开设置页到列表可见无感知延迟；刷新/同步沿用现有异步桥接，不阻塞 UI | 不新增同步阻塞调用；沿用 `run_coro` 模式（`async_bridge.py`） |
| 兼容 | 桌面 UI 装配变化 + 频道域方法/属性/列名全量重命名（FR-07/08）；CLI 行为不变；**物理列一次性改名迁移（本机旧库），无外部存量用户压力**；跨三平台（Windows `windows` QPA / macOS / Linux） | CI 三平台 8/8 绿；旧库迁移测试 + 双后端默认值一致（FR-08 GWT） |
| 可访问性 | 去 emoji 后按钮/分组靠文字自解释；保留键盘可达与现有对比度 | 无新增纯图标按钮 |
| 可维护性 | 频道页撤销后全局只有 1 个 ChannelWidget 实例，事件单消费；注释中「5 页」等过时表述随本版同步更正；频道域命名与 v2.0.3 术语对齐 | grep「5 个 Tab / 5 页 / 8 个 FormSection」归零（`nav_bar.py:2-9`、`main_window.py:3,93`、`ui_assembler.py:6`、`settings_page.py:154`）+ 频道域 subscribe 词根白名单扫描（FR-07 GWT 第 1 条） |

---

## 九、业务流程与状态流转

**监听频道管理主流程（改造后）**：

```
设置页（idx 2）
 └─ 账户凭证（登录态 READY 才可操作）
     └─ 监听频道管理（ChannelWidget 单实例）
         ├─ 上栏「已加入频道」 ──双击──> monitor_channel()（原 subscribe_channel）──> 白名单 + ChannelMonitored
         │        └─「刷新」 ──> refresh_joined_channels() ──> 拉 TG 已加入 + 空返回保护
         └─ 下栏「已监听」 ──双击──> unmonitor_channel()（原 unsubscribe_channel）──> 移出白名单 + ChannelUnmonitored
                  └─「全量同步…」 ──> sync_requested ──> MainWindow._on_sync_requested
```

**状态机（沿用现状，不改动）**：未登录（按钮禁用）→ READY（可操作）→ 登出（禁用 + 白名单保留，借鉴 `unmonitor_channel`（原名 `unsubscribe_channel`）不动 TG 订阅的语义 `channel_service.py:96-97`）。

**能力迁移对照（G3 防丢失清单）**：

| 频道页能力（现状） | 改造后落点 | 证据 |
|---|---|---|
| 上栏已加入 + 刷新 | 设置页上栏 | `channel_widget.py:394-409,556-566` |
| 下栏已监听 + 多选 | 设置页下栏 | `channel_widget.py:411-422` |
| 搜索过滤 | 设置页搜索框 | `channel_widget.py:387-392,436-439` |
| 双击订阅/退订 | 设置页双击 | `channel_widget.py:571-591` |
| 全量同步… | 设置页下栏按钮，重接 FR-03 | `channel_widget.py:593-605`、`ui_assembler.py:291-293` |
| 事件驱动标题/头像更新 | 单实例消费 | `channel_widget.py:482-538` |
| 未登录禁用 | `channel_panel` 重定向 | `app_state_controller.py:55` |

---

## 十、数据与埋点需求

本期无新增数据字段、无埋点（v1.3 起物理列 `subscribed`→`monitored` 属重命名 + 一次性迁移，非模型扩展，见 FR-08）。监听状态读写沿用现有链路：

- 白名单：`set_channel_monitored` / `list_monitored_channels`（原 `*_subscribed_*`，本期改名；`repository.py:135`、channel_service 对应方法）。
- TG 已加入：`set_channel_tg_subscribed` / `count_tg_subscribed`（`repository.py:144,149`，语义正确不改）。
- 物理列：channels 表的监听列由 `subscribed` 改名为 `monitored`（FR-08），一次性迁移；无新增字段、无埋点、无通用 migration 框架。
- 大盘统计读口（`dashboard_widget.py:498,519,535`）不动——频道页撤销不影响大盘数据展示。

---

## 十一、权限与安全要求

- 操作权限门禁沿用 core 的 `require_ready`（`channel_service.py:88,104`）：未登录时 UI 禁用 + core 拒绝，双层防护不削弱。
- 设置页「登录 / 登出」入口归属账户凭证分组的安全语义不变（2.0.4 FR-02 结论）。
- 不涉及个人信息展示变更；频道标题/头像沿用现有脱敏与存储口径。

---

## 十二、异常处理清单

| 异常 | 处理 | 证据 |
|---|---|---|
| 未登录 / TG 未 ready 拉频道 | 上栏空 + empty_hint 引导「先登录」；`list_joined_channels` 返 `[]`，`refresh_joined_channels` 整轮跳过 | `channel_service.py:64-72,125-147`、`channel_widget.py:400-405` |
| 刷新返回空列表 | 沿用 RK-09 保护，不清空既有白名单、不级联停监听 | `channel_service.py:125-147`、`test_subscription_monitoring_2003.py` |
| 搜索无匹配 | 两栏展示空态，不报错 | `ChannelListCard.apply_filter`（`channel_widget.py:295`） |
| 设置页内嵌控件高度不足 | 给固定最小高度 + 滚动区兜底；真机验证 stretch 3:2 不被压扁 | 实施阶段视觉校准 |
| 事件重复消费 | 频道页撤销后单实例，天然消除；需在 G3 断言中验证「更新一次」 | FR-03 验收第 3 条 |
| 旧注释/旧文档残留「频道页」「5 页」 | 随本版统一更正（§8 可维护性 / FR-09） | 附录 A |
| 旧库 `subscribed` 列迁移失败 / 中途中断 | 迁移在 `init_schema` 后事务内执行 `RENAME COLUMN`；失败保持原状并记日志，下次启动重试（幂等）；绝不 DROP 列 | FR-08 业务规则 2 |
| 默认值为 NULL 的存量 `monitored` 列 | 映射 `bool(None)=False`，统一为未监听；迁移测试覆盖 NULL 行 | FR-08 业务规则 3 |

---

## 十三、上线计划、依赖与上线后验证

### 13.1 依赖就绪度评估

| 依赖项 | 就绪状态 | 分级 | 最晚就绪时限 | 影响/替代 |
|---|---|---|---|---|
| core 订阅/监听 API + 双字段模型 | 已就绪（v2.0.3） | 可绕过→已具备 | — | 无新增需求 |
| ChannelWidget / ChannelListCard 可复用 | 已就绪（605 行，功能完整） | 已具备 | — | 无 |
| ChannelWidget 迁入设置页装配 | 待实施 | 阻塞（本期核心） | PR-1 合入前 | 无替代（就是本期内容） |
| 契约测试改造清单（§7 FR-06/FR-07） | 已定位到具体文件/行号 | 并行 | 各对应 PR 前 | 无 |
| 命名去歧义不受 allowlist 约束 | 已核实 `test_app_service_surface.py` 只锁 AppService 自身方法 | 已具备 | — | `channels` 仅实例属性名，改 `ChannelService` 方法名安全 |
| 物理列改名迁移路径 | 已核实 SQLite/PG 均支持 `RENAME COLUMN`；现有 repair 只补列不删列 | 已定位（FR-08） | PR-5 迁移逻辑合入前 | 无外部用户，本机旧库用一次性 ALTER 迁移 |
| tag `v2.0.5` | 截至起草未打 | 可绕过 | 版本 bump PR 前确认 | 2.0.6 基于 2.0.5 行，tag 延后不影响规划 |

### 13.2 建议 PR 切分（每分支一主题，squash merge）

严格顺序：**先建新能力，再撤旧页，再正名，最后迁移存储与清债**——保证中途任一时刻功能不中断、可回退；命名重命名与存储迁移独立成 PR，避免与功能改动混淆 review。

1. **PR-1 `feat(settings)`**：FR-01 + FR-03 前半——设置页新增监听频道区块、内嵌 ChannelWidget、重定向 channel_panel 信号/门控接线（此时频道页仍在，双入口并存，便于 A/B 验证）。
2. **PR-2 `refactor(nav)`**：FR-02 + FR-03 后半 + FR-06 导航/索引部分——撤导航与 stack 页、索引顺延、契约改造；撤页后全局单实例。
3. **PR-3 `style(settings)`**：FR-04 + FR-05 + FR-06 emoji 部分——设置页去 emoji、排版重排、emoji 扫描契约。
4. **PR-4 `refactor(naming)`**：FR-07 D4 全档（core/VM/UI 私有成员 + 存储 ABC 方法名与形参，物理列字面量暂留到 PR-5）+ 白名单静态扫描契约；可细分 4a core/VM/UI、4b 存储方法名，按半径先后合入。
5. **PR-5 `refactor(storage)`**（v1.3 新增）：FR-08——物理列 `subscribed`→`monitored` DDL/EXPECTED_SCHEMA/SQL 字面量/映射键全改 + 一次性列改名迁移逻辑 + SQLite/PG 默认值统一 + 旧库迁移测试。**须在 PR-4 之后**（先有正名方法名再改列名）。
6. **PR-6a `chore(cleanup)`**（v1.4 细化）：FR-09 L1 兼容桥接清理 + L3 过时注释归零 + **A 层 emoji**（纯注释/docstring/QSS 注释/仓库文档，纯文本 diff）+ 新增 `test_no_decorative_emoji.py` 静态守卫（宽白名单起步）。
7. **PR-6b `style(emoji-ui)`**（v1.4 新增）：**B 层** UI 装饰字面量——设置页 10 处已在 PR-3 清过则跳过，其余按模块（status_bar → message_list_model_format → channel_widget / media_manager_widget / message_detail / 空态 / 灯箱 / 同步框）分批；**每个子批同 PR 带对应测试断言修改**；守卫白名单逐级收紧。
8. **PR-6c `style(emoji-export)`**（v1.4 新增）：**C 层** 导出模板（HTML/MD）+ 服务消息文本 `_ACTION_TEXT` + 进度文本；作为产品可见变更单独评审，同步 `test_exporters.py` 快照断言，跑全量回归；守卫白名单再次收紧。
9. **PR-6d `test(emoji-guard)`**（v1.4 新增）：**D 层确认**——把守卫白名单收紧到最终态（仅 reaction 领域数据字段/参数），跑全套 pytest + 三平台 CI，确认零误伤、全绿。
10. **PR-7 `chore(release)`**：版本 bump 2.0.5→2.0.6 + CHANGELOG（6 处版本落点，按既有清单），tag 与 Release 由用户确认后触发。

每个 PR：本地全套 pytest + ruff + mypy 全绿 → 开 PR → 跟踪 CI 8/8 → squash merge → 同步 main 并删分支。**PR-6a/b/c/d 各自独立可回退，任何一批 CI 红即停在该批，不叠加后续；这正是 D7=α 下「review 与回归不失控」的执行纪律。**

### 13.3 上线后验证清单

- 桌面端三平台各打开设置页，确认导航 3 项、监听管理可用、emoji 清零。
- 手工走 S1–S5 五场景，重点验证双击订阅/退订与全量同步。
- 观察大盘活动时间线：`ChannelMonitored` / `ChannelUnmonitored` 事件落库仍正常（频道页撤销不影响 core 活动流）。
- **旧库迁移验证（FR-08，替代原「存量库零迁移」条目）**：用一份列名为 `subscribed`、含已监听频道的本机旧 SQLite / PG 库启动新版本，确认自动迁移为 `monitored`、监听状态逐条保留、无新旧列分裂；全新库默认值为未监听。
- **全局 emoji 分层验证（FR-09，v1.4）**：三平台各界面肉眼扫一遍装饰 emoji 已清零（设置页/状态栏/消息行/媒体管理/导出对话框）；发一条带 🔥👍 reaction 的消息 + 一条贴纸消息，确认 reaction/贴纸 emoji **原样显示与落库**（D 层零误伤）；导出一次 HTML/MD 确认新文案一致。

---

## 十四、风险与遗留问题

### 14.1 风险登记册

| 编号 | 风险描述 | 类别 | 概率 | 影响 | 等级 | 应对措施 | 状态 |
|---|---|---|---|---|---|---|---|
| ~~RK-01~~ | ~~「已订阅频道」术语理解错~~ 术语已在 v2.0.3 澄清、用户 2026-11-18 确认 | 需求 | 1 | 2 | 2 低（已关闭） | 订阅=TG 已加入、监听=monitor 已定死；保留 `tg_subscribed`/`is_monitored` 字段断言作回归守护即可 | **已关闭** |
| RK-02 | ChannelWidget 塞进设置页滚动区，布局被压扁/交互退化 | 技术 | 3 | 4 | 12 中高 | 固定最小高度 + 真机三平台视觉验证；保留 stretch 3:2 | 跟踪中 |
| RK-03 | 导航数量/索引/channel_panel 契约断言密集，漏改致 CI 红 | 技术 | 4 | 3 | 12 中高 | §7 FR-06 逐文件清单 + 新契约测试独立定位 | 跟踪中 |
| RK-04 | 双入口并存期（PR-1 后 PR-2 前）事件双消费 | 技术 | 2 | 3 | 6 低 | PR-1/PR-2 连续合入，缩短窗口；PR-2 后单实例断言 | 计划缓解 |
| RK-05 | 全量同步接线在撤页时丢失 | 技术 | 2 | 4 | 8 中 | FR-03 明确重接 + S3 场景验收 | 跟踪中 |
| RK-06 | 去 emoji 后 i18n 已有翻译串与测试文案不一致 | 数据 | 3 | 2 | 6 低 | 文案断言随 FR-04 同步更新；`tr()` 调用保留 | 观察 |
| RK-07 | 2.0.5 tag 未打即规划 2.0.6，版本落点混淆 | 进度 | 2 | 2 | 4 低 | PRD 标注基线；bump PR 前统一确认版本链 | 观察 |
| RK-08（v1.2） | 命名重命名漏改某调用点，致运行时 AttributeError / 测试红 | 技术 | 3 | 3 | 9 中 | FR-07 完整改名表逐项核对 + 白名单静态扫描 + 全套 pytest；公开方法改动集中一个 PR 便于回滚 | 跟踪中 |
| RK-09（v1.2） | 误伤 EventBus `subscribe*` / TG 订阅 API，破坏事件订阅或 TG 语义 | 技术 | 2 | 5 | 10 中高 | §5.6/FR-07 明确「不改清单」；静态扫描限定频道域；`test_monitor_whitelist` / `test_events` 回归守 | 跟踪中 |
| ~~RK-10~~（v1.3） | ~~物理列改名致存量用户监听清零~~ → 无外部存量用户，前提消失 | 数据 | — | — | 关闭 | 翻案为 D5=β 全改；本机旧库用一次性 `RENAME COLUMN` 迁移解决（FR-08），并新增旧库迁移测试兜底 | **已关闭（转 FR-08 实施）** |
| RK-11（v1.3） | 只改列名不改迁移/默认值，导致旧库新旧列分裂（SQLite 补 NULL→清零 / PG 补 TRUE→全开） | 技术 | 3 | 4 | 12 中高 | FR-08 把 DDL + 迁移 + 默认值统一绑在同一 PR；旧库迁移测试 + 双后端 parity 守；禁止只改字符串 | 跟踪中 |
| RK-12（v1.3） | 存储两方法 + 形参 + tests 43/36 行机械漏改，致 InMemory 替身不满足 ABC / 测试红 | 技术 | 3 | 3 | 9 中 | FR-07 第 6 条四实现清单逐项核对；`test_storage_backends` / inmemory 替身契约守；全套 pytest | 跟踪中 |
| RK-13（v1.4） | 全局 emoji 清理误伤 Telegram 用户真实 reaction 表情（🔥👍❤️🎉），篡改领域数据 + 测试红 | 数据 | 3 | 5 | 15 中高 | FR-09 D 层「禁止清理」+ 静态守卫白名单永久放行 `ReactionDTO.emoji` / reaction 参数；A/B/C 各批只匹配装饰用途；`test_reaction_format` / `test_dto_favorites` / `test_monitor_vm` 回归 | 跟踪中 |
| RK-14（v1.4） | C 层导出模板/服务消息 emoji 清理改变产物与历史消息呈现，用户已导出文件不一致 | 产品 | 3 | 3 | 9 中 | C 层单独 PR-6c 走产品可见变更评审；同步 `test_exporters.py:518,990-992,1029-1033`；上线清单肉眼验收 | 跟踪中 |
| RK-15（v1.4） | 全局 emoji 分批清理致 review 面失控 / 中间态红测窗口 | 流程 | 3 | 4 | 12 中高 | PR-6a/b/c/d 每批独立可回退；B 层每批同 PR 改断言（无"改 UI 未改断言"窗口）；静态守卫白名单逐批收紧；任一批 CI 红即停 | 跟踪中 |

### 14.2 遗留问题

- ~~**L1**~~（v1.3 并入 FR-09 本期清理）：ChannelWidget 兼容旧属性桥接 `channel_widget.py:424-434`（`lst_joined` / `btn_sync` 等）。
- ~~**L2**~~（v1.4 并入 FR-09 本期，D7=α）：全局装饰性 emoji 按四层清理；**D 层 reaction 领域数据零误伤**（RK-13 守）。边界字形（▶/✓/★）非本期。
- ~~**L3**~~（v1.3 并入 FR-09 本期清理）：源码「5 页 / 5 Tab / 8 个 FormSection」等过时注释 + 模块 docstring 方法数漂移。
- ~~**L4**~~（v1.3 并入 FR-08 本期实施）：存储层 `set_channel_subscribed` / `list_subscribed_channels` 与物理列 `subscribed` 的正名——「没人用」前提使其从「需灰度数据迁移方案」降级为「一次性本机旧库列改名」，已纳入本期。

---

## 十五、附录

### 附录 A — 诊断证据索引（e7dfb03 只读排查）

- **A-1 导航与装配**：`nav_bar.py:35,49,52-54,202-212`（4 项标签/图标/按钮）；`ui_assembler.py:118-166`（stack 4 页：大盘 0 / 频道 1 / 媒体 2 / 设置 3）；`main_window.py:430-440`（属性映射）；过时「5 页」注释 `nav_bar.py:2-9`、`main_window.py:3,93`、`ui_assembler.py:6`。
- **A-2 频道页能力**：`channel_widget.py:347,382-422`（双栏 + 搜索 + 刷新 + 同步）、`:571-605`（订阅/退订/同步）、`:482-538`（事件驱动更新）、`:441-456`（未登录禁用）。
- **A-3 双字段模型**：`channel_service.py:18-20`（术语权威定义）、`:64-107`（list/subscribe/unsubscribe）、`:109-173`（refresh + RK-09 保护）；`dto.py:21-39`；存储列 `sqlite_repo.py:63-64`、`_lifecycle_pg.py:53-54`。
- **A-4 频道页引用面**：生产 `ui_assembler.py:56,141-150,206,291-293`、`main_window.py:71,229,436,517-519,608-619`、`controllers/_base.py:40,89`、`app_state_controller.py:55`；测试 `test_channel_widget.py`、`test_main_window_channels.py`、`test_app_state_controller.py:321-344` 等。
- **A-5 设置页**：`settings_page.py:95-151`（布局：标题 + QScrollArea + form_root + 底部按钮栏）、`:117-123`（6 个 FormSection 顺序）、`:518-598`（collect/load 通用循环，不含白名单）、emoji 命中 `:160,252,283,320,391,431,723,725,727,739` + docstring `:7,158,247,277,318,389,429`。
- **A-6 emoji 全局对比（v1.0，v1.4 结论已更新）**：40+ 文件使用 emoji（如 `state_labels.py:34-43`、`status_bar.py:105,212,277,289,310`、`media_manager_widget.py:322,362,373,385,403`、`message_detail.py:135,195,242,265`）——v1.0 据此把范围限定设置页；**v1.4 D7=α 已推翻该范围限制，改为全局装饰性 emoji 清理，见 A-12/A-13**。
- **A-7 契约测试**：`test_dashboard_layout_2004.py:27-34,94-99`（nav/stack 数量 + 媒体 idx 2）；`test_lifecycle_controller.py:130-162`（switch_tab/on_nav_changed 索引）；`test_dashboard_header_panels_2005.py:47-51`（全局 pageTitle QSS 保留）；`test_form_row.py:197-227`（emoji+pageTitle+hint 结构）。
- **A-8 命名歧义（v1.2，全仓 subscrib 词根诊断）**：频道域歧义——`channel_service.py:74,80,93`（subscribe/unsubscribe/list_subscribed）、`monitor/service.py:153`、`monitor/whitelist.py:53`、`message_service.py:54`、`channel_widget.py:374,429,467,540,549,584`、`viewmodels/_channels.py:83-84,137,166,174`、`dashboard_widget.py:251,442`、`status_bar.py:120`、`message_empty_overlay.py:44`。事件域正确（勿改）——`events.py:443,451-465`（EventBus.subscribe*）、`telegram/client.py:141`（subscribe_updates）、`events.py:58,65`（ChannelMonitored/Unmonitored）、`dto.py:36,39`（is_monitored/is_tg_subscribed）、`repository.py:144,149`（TG 订阅 API）。
- **A-9 allowlist 不锁 ChannelService（v1.2）**：`test_app_service_surface.py:19-51` 的 `ALLOWED_METHODS`/`PROPERTIES`/`INSTANCE_ATTRS` 用 `inspect.getmembers(AppService)` + `vars(app)`（`:65-69,79-83,94`），只锁 AppService 自身；`channels` 仅作实例属性名（`:46`），**不检查 ChannelService 方法名** → 改名 `app.channels.subscribe_channel` 等不触发 allowlist 失败。
- **A-10 物理列迁移风险（v1.2，v1.3 已由 FR-08 处置）**：`repository.py:49-71`（repair 只补列不删列）、`sqlite_repo.py:63`（列默认 0）、`_lifecycle_pg.py:59-60`（列默认 TRUE，跨后端不一致）、`sqlite_repo.py:489-512`（自愈 init_schema）——只改列名会致旧列变 extra、新列默认补入、新旧列分裂。v1.3 用一次性 `RENAME COLUMN` + 默认值统一解决。
- **A-11 存储层改名半径全量核查（v1.3，第二轮只读诊断）**：物理列 `subscribed` 字面量 22 处——SQLite `sqlite_repo.py:63,190,333,598,607,646,728,730,769,830`；PG `_lifecycle_pg.py:53,60,241`、`_channels_pg.py:39,68,77,117,206,208,246-249,259-262,272-275`、`_messages_pg.py:114-119`。存储 ABC 方法签名与实现：`repository.py:135,144,159`、`postgres_repo.py:156-174`、`sqlite_repo.py:722,735,751,765`、`_channels_pg.py:197,216,234,254`、`tests/fixtures/_in_memory_repository.py:137,163,188,194`。默认值漂移：`sqlite_repo.py:333`/`_channels_pg.py:39` 的 `row.get(..., True)`、`_lifecycle_pg.py:53`(FALSE) vs `:60`(TRUE)、`dto.py:36` 默认 False。测试硬编码：`test_sqlite_repo.py:229-234`（造旧库 SQL）、`:81-96`（DDL/EXPECTED_SCHEMA 守门）。后端仅 SQLite/PG 两套（`config.py:29-30`、`factory.py:18-26`），无 CHECK/视图/索引依赖列名。DTO 无 from_row/to_row（`dto.py`），列↔DTO 映射全在仓储层。
- **A-12 全局 emoji 全量核查（v1.4，第三轮只读诊断）**：src 43 文件 254 行 + tests 27 文件 214 行命中；src 按 AST/tokenize 拆为 23 行纯注释 / 75 行 docstring / 156 行字符串字面量或数据；71 种不同 emoji，高频 `⏸`(26)、`★`(21)、`🏷`(18)、`⚠`(15)、`📝`(15)、`📌`(13)、`⏳`(12)、`✓`(10)、`❌`(10)。无全局 emoji 常量模块，`ui/icon.py` 是 SVG 加载器与 emoji 无关；仅有 4 个局部半集中字典可杠杆覆盖：`_mm_base.py:25,56`、`state_labels.py:33`、`sync_dialog.py:219-224`、`telethon_messages.py:239`(45 处)。
- **A-13 B/C 层 emoji 与测试锁定清单（v1.4）**：B 层 UI 装饰——`settings_page.py:160,252,283,320,391,431,723,725,727,739`；`status_bar.py:105,212,277,289,293,316`；`state_labels.py:34-45,85,104`；`_mm_base.py:26-33,56-59`；`_mm_render.py:328`；`message_list_model_format.py:38,40,43,45,79,81,95,97,99,101`；`channel_widget.py:145,147,389,401,405,418`；`media_manager_widget.py:322,362,373,385,403`；`message_detail.py:135,195,220,242,265,317,319,321`；`message_empty_overlay.py:45,50,55,66`；`lightbox_dialog.py:644,778,781`；`sync_dialog.py:187,219-224`；`app_state_controller.py:91,93,127`；`export_controller.py:81`；`media_actions_controller.py:445`；`clear_channel_preview_dialog.py:56,64`；`export_dialog.py:133`。对应测试断言：2a 清单（`test_format_meta_icons_order.py:72-84,94,143-180,202-205`、`test_message_view.py:294,726,737,953-1018`、`test_channel_widget.py:172-234,321-322`、`test_form_row.py:206,219`、`test_status_bar.py:77`、`test_app_state_controller.py:154,155,254,263`、`test_main_window_channels.py:685`、`test_tray_icon_pause.py:87`、`test_message_delegate_layout_2005.py:165`）；**`test_dashboard_header_panels_2005.py` / `test_status_bar_layout.py` / `test_live_readonly_2002.py` 零 emoji 字面量，无需同步**。C 层产物——`html_exporter.py:58,72,78-80,89`、`markdown_exporter.py:80,113,115,117`、`telethon_messages.py:203-274,322`、`channel_sync/service.py:163`，断言 `test_exporters.py:518,990-992,1008-1033`。D 层领域数据——`dto.py` 的 `ReactionDTO.emoji` / 贴纸 emoji (`telethon_messages.py:383`)、tests 中 `ReactionDTO(emoji="🔥"/"👍"/"❤️"/"🎉"/"🚀")` 与 `add_reaction(...)` 参数（`test_reaction_format.py`、`test_telegram_batch_actions.py`、`test_telethon_client.py:703,709`、`test_storage_backends.py:598-599`、`test_monitor_vm.py`、`test_dto_favorites.py`、`test_pg_repo_parity.py:278,285` 等）——**禁止清理**。非 py 载体：`style.qss:230,613,668,677` 全是注释；`.env.example` 零 emoji；导出 HTML 模板内嵌在 `html_exporter.py` 的 `TEMPLATE` 字符串（非独立 .j2 文件）。
- **A-14 守卫空白（v1.4）**：仓库当前无任何 emoji 静态守卫——`.pre-commit-config.yaml`、`pyproject.toml`、`.github/workflows/`、`tests/conftest.py` 均无 emoji 规则；FR-09 第 4 条新增 `test_no_decorative_emoji.py` 为首个守卫。

### 附录 B — 需求追踪矩阵（RTM）

| 需求编号 | 需求描述 | 来源 | PRD 章节 | 测试用例 | 状态 |
|---|---|---|---|---|---|
| R-01 | 取消频道页面 | 用户诉求 1 | §4.1、FR-02 | `test_dashboard_layout_2004`（改造）+ 新契约 G1 | 待实施 |
| R-02 | 设置页增加监听频道管理，从已订阅频道选监听 | 用户诉求 2 | §4.1、FR-01、T1 | 新契约 `test_settings_channel_management_2006` G2 + `test_subscription_monitoring_2003` | 待实施 |
| R-03 | 设置页风格优化、去 emoji、提升便携度 | 用户诉求 3 | §4.1、FR-04/05 | emoji 静态扫描 + 分组顺序断言 + `test_dashboard_header_panels_2005` | 待实施 |
| R-04 | 撤页不丢能力（隐含约束） | 诊断推导 | §9、FR-03 | GWT FR-03 三条 + `test_app_state_controller` | 待实施 |
| R-05 | 无回归 | 质量基线 | §8、FR-06 | 全套 pytest + CI 8/8 | 待实施 |
| R-06 | 方法名/变量名去歧义 | 用户诉求 4（v1.2，v1.3 全档） | §4.1、FR-07、§5.6/5.7 | 白名单静态扫描 G7 + 全套 pytest | 待实施 |
| R-07 | 存储 ABC 方法 + 形参正名 | 用户「历史问题全改」（v1.3） | §4.1、FR-07 第 6 条、D5 | `test_storage_backends` / inmemory 替身契约 + 全套 pytest | 待实施 |
| R-08 | 物理列改名 + 一次性迁移 + 默认值统一 | D5 翻案（v1.3） | §4.1、FR-08、§5.7 | 旧库迁移测试 + 双后端 parity + `test_sqlite_repo` DDL 守门 | 待实施 |
| R-09 | 历史债清偿（L1/L3 + L2 四层，v1.4 D7=α） | 用户「历史问题全改」+ D7 拍板 | §4.1、FR-09、§5.8 | grep 过时注释归零 + 桥接属性清理断言 + 守卫（详见 R-10） | 待实施 |
| R-10（v1.4） | 全局装饰 emoji 清理（D7=α，四层） | 用户拍板并入本期 | §4.1、FR-09 第 3/4 条、§5.8 | `test_no_decorative_emoji.py` 守卫 + B 层同步测试 + C 层 `test_exporters.py` + D 层 reaction 回归 | 待实施 |

### 附录 C — 评审决策记录

| 编号 | 决策点 | 推荐 | 用户决定 | 理由/影响 |
|---|---|---|---|---|
| A1 | 取消频道页力度 | β 撤导航+撤栈、类保留复用 | **用户 2026-11-18 采纳推荐（β）** | 功能搬家不丢能力、core 零改动 |
| A2 | 设置页监听管理形态 | α 内嵌完整双栏 ChannelWidget | **用户 2026-11-18 采纳推荐（α）** | 语义对齐「从已订阅选监听」、零重写 |
| A3 | 风格优化范围 | β 仅设置页去 emoji + 排版优化 | **用户 2026-11-18 采纳推荐（β）** | 对齐诉求字面范围、牵连最小 |
| T1 | 「已订阅的频道」口径 | α = TG 侧已加入、监听=monitor | **用户 2026-11-18 已确认**（与 v2.0.3 代码术语一致） | 数据源锁定 `list_joined_channels()`，无需再拍板 |
| D4 | 命名去歧义力度（v1.2） | 必改+建议改全做 | **v1.3 扩为全档全做**（含原不改档） | 用户「历史问题全改」；公开方法改名不受 allowlist 约束 |
| D5 | 存储层/物理列是否改（v1.2） | α 不改 | **v1.3 翻案为 β 全改**（用户拍板） | 「没人用」前提使迁移风险消失；配套 FR-08 一次性列改名 + 默认值统一 |
| D7 | 全局 emoji（L2，v1.3） | β 独立专项（v1.3 推荐） | **v1.4 用户拍板 α 并入本期** | 用户「并进去吧，相信你不会让 review 和回归失控」→ 四层分类（A 零风险/B 同步测试/C 产物谨慎/D 领域数据禁清）+ 静态守卫逐批收紧 + PR-6a/b/c/d 分批可回退 |

### 附录 D — 合规声明

本方案为产品设计参考，具体实施请结合公司合规与法务要求确认。
