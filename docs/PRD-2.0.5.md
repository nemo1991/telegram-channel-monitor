# PRD — tgmonitor 2.0.5「大盘空间释放与视觉统一」版本规划

## 一、文档信息

| 项 | 内容 |
|---|---|
| 版本 | v1.2（第 2 轮评审通过，档位 2 纳入本期） |
| 目标版本 | tgmonitor 2.0.5 |
| 基线版本 | 2.0.4（`fe2f1f4`，tag `v2.0.4`）；本版修复 PR #69 已合入 `27fcd96`（未 bump） |
| 作者 | 司南（产品经理 Agent） |
| 需求方 | forcetone |
| 日期 | 2026-11-17（v1.1）→ 2026-11-18（v1.2） |
| 状态 | **已评审通过**（第 1 轮 A1/A2/A3 + 第 2 轮 A4 档位 2 纳入本期），可进入实施 |

**变更记录**

| 版本 | 日期 | 变更摘要 |
|---|---|---|
| v1.0 | 2026-11-17 | 初稿。基于代码事实只读诊断（4 条代码路径，均带文件:行号），把 3 条诉求归并为 5 条 FR，识别 3 个决策点并给出推荐，展开完整 PRD。 |
| v1.1 | 2026-11-17 | 评审确认：A1 采纳 D1-α（保留 5 卡仅压尺寸）、A2 采纳 D2-γ（容器观感 + 排版语言都对齐，分两步）、A3 采纳 D3-α（局部 objectName 修复面板标题，设置页不动）。全文按此收敛，决策点固化。 |
| v1.2 | 2026-11-18 | 第 2 轮评审：A4 决定把「档位 2 — delegate 结构性重构」从后续技术债**提前纳入本期**，作为 FR-06。新增 G6 与 perf 基线验收，RK-01 由"护栏兜住"升级为"结构性消除"，PR 切分增加重构 PR，RTM 与风险登记册同步。 |

**评审记录**：第 1 轮通过（2026-11-17，A1/A2/A3 三项全部采纳推荐方案）；第 2 轮通过（2026-11-18，A4 决定档位 2 纳入本期实施）。无遗留待决项。

> 诊断方式声明：本文所有「现状」结论均来自对 `fe2f1f4` 工作树的只读排查，未修改任何源码。文中「改动锚点」是实施落点建议，非已落地事实；尺寸数值（如卡片 100→68px）为**建议值/估算**，实施时按真机视觉微调。

---

## 二、背景与目标

### 2.1 背景

2.0.4 把实时流迁入大盘、活动落库，大盘成为唯一工作台（首页）。实际使用暴露三处视觉与空间问题：

- **空间被非核心元素占用**：大盘顶部有一个"操作台"标题（`dashboard_widget.py:225-228`）和 5 张固定 100px 高的统计卡（`dashboard_widget.py:82`），加上 24px 页边距与 20px 行间距（`:222-232`），整页纵向空间被大量占用；而真正高频使用的消息流只与底部两块按 3:2 分配（`:268/:308`）。
- **消息流与最近活动视觉割裂**：同在大盘一页，上半消息流是 `QListView` + 等宽 `<pre>` 富文本、时间以 `⏱` 内嵌正文（`message_item_delegate.py:172-175`、`message_list_model_format.py:79`）；下半最近活动是 `QListWidget` + 灰字时间独立列 + 14px 图标 + 无衬线文本（`dashboard_widget.py:151-163`）。QSS 只定义了 `QListWidget`（`style.qss:427`）而消息流用的 `QListView` 无专属样式，导致消息流缺少白底圆角容器与交替行色观感。
- **面板标题视觉缺陷**：「最近活动」「频道统计」用 `QGroupBox`，其 QSS 把标题 `subcontrol-origin: margin` 放在边框外的 margin 区，且 `padding-top:20px`（`style.qss:408-424`），视觉上标题"在框框的外部"且整体偏高。

### 2.2 要解决的问题

三条诉求本质是两件事：

1. **空间释放**：去掉非必要的标题与冗余留白，把纵向空间让给消息流（用户核心信息区）。
2. **视觉统一**：让消息流与最近活动在同一页里观感一致，修正面板标题的位置与高度。

### 2.3 目标与成功指标（口径明确）

| 目标 | 成功指标 | 口径与来源 |
|---|---|---|
| G1 标题去除 | DashboardWidget 布局中不再存在 objectName=`pageTitle` 且文本="操作台" 的 QLabel；其他页面 pageTitle 数量不变 | UI 自动化：遍历 `win.dashboard.findChildren(QLabel)` 断言；全窗口 pageTitle 计数与其他页面分别断言 |
| G2 空间释放 | 相同窗口尺寸下消息流 splitter 高度 ≥ 改造前；卡片行总高下降；stretch 比例保持 3:2 | UI 几何断言：改造前后 `dashboardLiveSplitter.height()` 对比（用固定测试窗口尺寸）；根布局中卡片行 QHBoxLayout 高度 < 改造前基线 |
| G3 样式统一 | 消息流容器具备与最近活动一致的白底圆角/交替行色观感；按 A2 决策，字体与时间呈现达到约定一致维度 | QSS 断言（QListView objectName + 规则存在）+ 按 A2 结果的 delegate/几何断言 |
| G4 标题归位 | 两个面板标题位于框内（不再骑在框外 margin 区），面板顶部留白 ≤12px；title 文本仍为"最近活动"/"频道统计" | QSS 断言（`dashPanel` 专属规则、无 `subcontrol-origin: margin`）+ 既有 `test_dashboard_layout_2004` 标题文本断言仍绿 |
| G5 无回归 | 既有测试基线不下降（扣除本版刻意调整的断言） | 全套 pytest 通过；改动测试逐条登记在 RTM（附录 B） |
| G6 布局契约结构性化（A4） | `paint()` 与 `sizeHint()` 读同一份布局结果，不再各自 new QTextDocument；四个漏改点从代码结构上消除 | 静态/运行时断言：单一 `_layout()` 入口存在；paint/sizeHint 不再各自 `setHtml`；缓存 key 含 width/font；`+8` 魔数移除；perf 基线不退化（见 §8） |

**非目标**：本期不改消息流的业务逻辑（过滤/选中/详情/只读语义不变）；不改活动的事件覆盖与落库口径；不改 core 任何公开 API；不做版本 bump 与 CHANGELOG 发布条目（按 2.0.4 惯例单独 PR）；不改设置页分组框样式（A3=α 时）；不新增数据字段与埋点。FR-06 重构**只改 `message_item_delegate.py` 内部实现，不改变其对外渲染结果（除 FR-03 步骤二约定的排版语言调整）与公开契约**（`clear_caches()` / `_DOC_CACHE_MAX` / `_plain_to_html` / `paint` / `sizeHint`）。

---

## 三、名词与术语定义

| 术语 | 定义 |
|---|---|
| 大盘 / 操作台 | 导航首页 `DashboardWidget`（`ui_assembler.py:130-138`），含统计卡行 + 实时流 splitter + 底部左右两块 |
| 顶部块 / 统计卡 | `_StatCard`（`dashboard_widget.py:73-115`），5 张：已加入频道 / 已监听 / 连接状态 / 存储后端 / 消息总数，各固定 100px 高 |
| 消息流 | `MessageView`（QListView + `MessageListModel` + `MessageItemDelegate`），从 v2.0.4 起单实例迁入大盘 splitter 左侧 |
| 最近活动 | `DashboardWidget.activity_list`（QListWidget），条目为 `_ActivityEntry`（时间 + 图标 + 文本） |
| 频道统计 | `DashboardWidget.stats_table`（QTableWidget），显示每频道消息数 |
| 面板标题 | 「最近活动」「频道统计」两个 `QGroupBox` 的 title（`dashboard_widget.py:275/287`） |

---

## 四、需求范围

### 4.1 包含（本期做）

- **FR-01** 删除大盘"操作台"标题 QLabel。
- **FR-02** 压缩顶部统计卡高度与相关间距，把释放的纵向空间让给消息流（D1-α：保留 5 卡仅压尺寸）。
- **FR-03** 让消息流样式与最近活动一致（D2-γ：容器观感 + 排版语言都对齐，分两步）。
- **FR-04** 修正「最近活动」「频道统计」面板标题的高度与位置（D3-α：局部修复，设置页不动）。
- **FR-05** 同步更新测试与契约；版本 bump / CHANGELOG 另立 PR。
- **FR-06** 重构 `MessageItemDelegate` 布局计算：抽出单一 `_layout()` 入口，`paint()` 与 `sizeHint()` 共享同一份 QTextDocument，从结构上消除隐式契约（A4=档位 2 纳入本期）。与 FR-03 步骤二合并实施——排版语言调整直接落在新结构上。

### 4.2 不包含（本期明确不做）

- 增删统计卡、改统计卡数据口径或取数逻辑（D1=β/γ 未选中时）。
- 改消息流的信息结构（频道/作者/媒体/收藏/标签等字段的取舍）——本期只改视觉呈现，不改 `MessageDTO` 字段与 `_format` 承载的信息集合（D2=β 仅调整呈现方式与样式，不删字段）。
- 改设置页 `FormSection` 的所有 QGroupBox 样式（D3=α 时）。
- 把 QGroupBox 换成 QFrame+QLabel 等自绘容器（破坏既有 title 语义与测试，见 D3-γ 不推荐）。
- 新增筛选/检索/导出等活动或消息功能；新增埋点或数据字段。

---

## 五、方案对比与选型说明

### 5.1 决策点 D1：顶部卡片如何"优化展示 + 省空间"（对应 FR-02）

| 方案 | 思路 | 成本 | 信息完整性 | 风险 | 评价 |
|---|---|---|---|---|---|
| **α 压缩保留（推荐）** | 保留 5 张卡，仅压缩高度（建议 100→约 68px）、值字号（24→约 20px）与间距（根 margin 24→约 16、spacing 20→约 12、卡间距 16→约 10）；stretch 保持 3:2 | 低 | 完好：不删卡、不改口径 | 低：纯尺寸调整，可逆 | **推荐** |
| β 精简卡片数 | 合并"连接状态/存储后端"等，5→3/4 张 | 中 | 下降：合并即丢独立展示与语义 | 中：动 UI 结构与数据口径 | 不推荐（除非需求方明确要减信息） |
| γ 扁平统计条 | 卡片改为单行紧凑指标条，取消图标/大数字 | 高 | 中 | 中高：改动最大 | 不推荐 |

**α 的有利事实**：省空间目标由"标题删除（FR-01）+ 卡片压缩 + 间距收紧"三者叠加即可达成，消息流 splitter 在根布局中 stretch=3、底部块=2（`dashboard_widget.py:268/308`），上方占用减少后消息流在固定窗口下自然变高，无需改比例逻辑，风险最低。

### 5.2 决策点 D2："样式一致"对齐到哪一层（对应 FR-03）

先明确现状"不一致"的三个维度：

| 维度 | 消息流（现状） | 最近活动（现状） |
|---|---|---|
| 容器 | `QListView` 无专属 QSS（`style.qss` 只定义 `QListWidget`），无白底圆角/交替行色观感 | `QListWidget`：白底圆角边框、交替行色、item 分隔线（`style.qss:427-447`） |
| 字体 | 等宽 `<pre>`（`message_item_delegate.py:172-175`） | 无衬线 QLabel（`dashboard_widget.py:161-163`） |
| 时间呈现 | `⏱ HH:MM:SS` 内嵌富文本 head（`message_list_model_format.py:79`） | 60px 灰色 hint 时间独立列 + 14px 图标（`dashboard_widget.py:151-159`） |

| 方案 | 对齐维度 | 成本 | 风险 | 评价 |
|---|---|---|---|---|
| α 仅容器观感 | 只给 QListView 加白底圆角/交替行色（QSS + objectName） | 低（纯 QSS） | 低 | 最小改动；视觉统一有限 |
| β 仅排版语言 | 去等宽 `<pre>`、时间改灰字独立列、字体族统一（动 delegate + format） | 中 | 中：动富文本渲染与行高缓存，需防消息截断回归（2.0.4 FR-01 曾修此 bug） | 单独做风险集中 |
| **γ 两者都对齐（推荐）** | α+β，分两步实施：先 QSS 容器、再 delegate 排版 | 中高 | 中：拆两步可控，任一步异常可独立回滚 | **推荐**：最贴合"样式一致"字面意图，且风险可分段消化 |

**γ 的护栏**：β 步骤涉及 `MessageItemDelegate._plain_to_html` 与 `sizeHint` 缓存（`message_item_delegate.py:123-175`）。2.0.4 FR-01 已修过"宽度未定时锁死行高导致文字裁切"，本次排版调整**必须保留**宽度感知的行高计算语义（`:144-158` 用容器宽 + `pre-wrap/break-word`，禁止回退到 `idealWidth()`），并复用现有行回归用例防复发。若需求方评审认为 β 风险不可接受，A2 可选 α（只做容器观感）。

### 5.3 决策点 D3：面板标题修复范围（对应 FR-04）

| 方案 | 思路 | 成本 | 波及面 | 评价 |
|---|---|---|---|---|
| **α 局部 objectName（推荐）** | 给大盘两个 QGroupBox 加专属 objectName（如 `dashPanel`），写专属 QSS 把标题放回框内、收紧 padding-top；全局 QGroupBox 规则不动 | 低 | 仅大盘两块，设置页不受影响 | **推荐** |
| β 全局改 QGroupBox | 直接改 `style.qss:408-424` 全局规则 | 低 | 波及设置页所有 FormSection 分组（`form_section.py:153`） | 不推荐（超出诉求范围） |
| γ 换容器 | QGroupBox→QFrame+QLabel 自绘标题 | 高 | 破坏 `title()` 语义与 `test_dashboard_layout_2004` 断言 | 不推荐 |

### 5.4 已确认组合与理由

**已确认采用 D1-α + D2-γ（分步） + D3-α + A4（档位 2 重构纳入本期）**（第 1 轮 2026-11-17 A1/A2/A3 采纳推荐；第 2 轮 2026-11-18 A4 决定提前实施 delegate 结构性重构）：前三者的共同点是"以最小改动达成视觉目标"；A4 则是对"最小改动"策略的主动修正——评审认为 D2-γ 步骤二若继续在旧的双路径结构上改富文本，等于把已知的隐式契约风险留在原地，不如先重构再改样式，一次根治。

- A1=D1-α：FR-02 按"保留 5 卡、压缩尺寸、stretch 3:2 不变"实施；不从简到 3/4 卡，不改数据口径。
- A2=D2-γ：FR-03 分两个可独立回滚的步骤——**步骤一** QSS 容器观感（objectName + 白底圆角/交替行色）；**步骤二** delegate 排版语言（去等宽 `<pre>`、字体族统一、时间呈现对齐）。步骤二与 FR-06 重构**合并实施**：排版语言调整直接落在新结构上（见 §7 FR-06）。
- A3=D3-α：FR-04 只给大盘两个 panel 加 `dashPanel` 专属 objectName，全局 QGroupBox 规则不动，设置页零影响。
- A4=档位 2：FR-06 把 `paint`/`sizeHint` 各自 new doc 的双路径结构改为单一 `_layout()` 共享，四个漏改点（缓存命中跳过字体、宽度 fallback 不一致、`+8` 单边 padding、无 DTO 第三路径）从结构上消除；代价是缓存粒度变细、需重跑 PR #9 perf 基线（见 §8）。

### 5.5 演进路径

- **本期**：FR-01~06 一次规划，切成 4 个实施 PR + 1 个版本 PR（见 §13.2）。FR-03 步骤二与 FR-06 合并为同一个 PR。
- **后续（触发条件：用户提出要减少信息密度）**：再评估 D1-β/γ（精简或重排卡片），本期不预建。
- **档位 3（去富文本，本期不做）**：`_plain_to_html` 的 HTML 转义 + `<br>` + `<pre>` 包装层，是否改用 `setPlainText` + `QTextCharFormat` 需等 FR-03 的灰字时间呈现方案定了再评估——若用 HTML span 则保留富文本，若用 QTextCursor 字符格式则可去 HTML 层。**本期 FR-06 不预设结论，保留 `_plain_to_html` 接口**。

---

## 六、用户角色与使用场景

**目标用户**：tgmonitor 桌面应用使用者（频道监听的运营/观察者），单机使用，熟练桌面 GUI。

| 场景 | 用户目标 | 当前痛点 | 本期后的流程 |
|---|---|---|---|
| S1 打开即看消息 | 一眼看到尽可能多的消息 | 标题 + 5 张高卡片占掉整页上部，消息流可视区被压缩 | 去标题、卡片压缩后，消息流可视行更多 |
| S2 大盘内视觉连贯 | 消息流与活动看起来像一套 UI | 上半等宽 emoji 富文本、下半灰字图标列表，风格割裂 | 两者容器与观感统一（按 A2 决策） |
| S3 阅读面板标题 | 标题贴着面板、不悬空 | 标题骑在框外 margin、面板偏高 | 标题归位框内、顶部留白收紧 |

---

## 七、功能需求（编号 + 业务规则 + GWT 验收）

### FR-01 删除大盘"操作台"标题（无决策依赖，直接实施）

**业务规则**
- 删除 `DashboardWidget._build()` 中 objectName=`pageTitle`、文本="操作台" 的 QLabel 及其 `root.addWidget(title)`。
- 全局 QSS `#pageTitle` 规则保留（设置页/详情页/form_row/ui_assembler 仍依赖）。

**验收标准**
- Given 真实 MainWindow，When 遍历 `win.dashboard.findChildren(QLabel)`，Then 不存在文本="操作台" 且 objectName="pageTitle" 的节点。
- Given 设置页与消息详情已构造，When 统计各自 pageTitle 标签，Then 数量与改造前一致（全局样式未受影响）。

**改动锚点**：`dashboard_widget.py:225-228`。

### FR-02 压缩顶部统计卡并释放空间（A1=α 已确认）

**业务规则**
- 保留全部 5 张卡与取数口径（A1=α）；压缩 `_StatCard` 固定高度（建议 100→约 68px）、值字号（建议 24→约 20px）。
- 收紧根布局边距与间距（建议 root margin 24→约 16、root spacing 20→约 12、cards spacing 16→约 10）。
- `live_splitter` 与 `bottom_row` 的 stretch 保持 3:2 不变，让释放的空间通过布局自然分配给消息流。
- 不精简卡片数量、不改数据口径（A1 否决 β/γ）；具体数值为建议值，实施时按真机视觉微调，但必须满足 G2。

**验收标准**
- Given 固定测试窗口尺寸，When 对比改造前后，Then 卡片行 QHBoxLayout 高度下降、消息流 splitter 高度不减（几何断言）。
- Given 压缩后卡片，When 渲染数值与副文本，Then 文字无裁切（最小高度下 QFontMetrics 校验）。
- Given 压缩后 5 张卡，When `update_stats()` 刷新，Then 数据展示口径与 2.0.4 一致（卡数仍为 5）。

**改动锚点**：`dashboard_widget.py:73-115`（`_StatCard`）、`:220-249`（`_build` 根布局与卡片行）、`:268/:308`（stretch 保持）。

### FR-03 消息流样式与最近活动一致（A2=γ 已确认，分两步）

**业务规则**
- **步骤一（容器观感，QSS）**：给消息流 `QListView` 设专属 objectName（如 `streamList`），在 `style.qss` 增加与 `QListWidget` 同观感的白底圆角/边框/交替行色规则。
- **步骤二（排版语言，delegate）**：在步骤一基础上，把排版语言向活动对齐——去等宽 `<pre>`、字体族统一为无衬线、时间呈现改为灰色独立样式；**保留** `MessageDTO` 全部信息字段，只改呈现。**步骤二与 FR-06 重构合并实施**：排版调整直接落在 FR-06 抽出的单一 `_layout()` 新结构上，不在旧双路径结构上改。
- 回滚粒度：步骤一（PR 3）为纯 QSS，可独立回退；步骤二与 FR-06 合并为同一个 PR 4，**二者作为一个整体回退**（退回后 FR-03 退化为仅步骤一状态）。不存在"只单独回退步骤二而保留重构"的语义。
- 强制护栏：排版调整必须保留宽度感知的行高语义（按容器宽折行），禁止回退 `idealWidth()`；`pre-wrap/break-word` 换行语义保留，防止长消息撑出横向滚动条或文字裁切。FR-06 的新结构天然满足（单一 doc + 容器宽），见 FR-06 验收。
- 消息流的只读/选中/详情/过滤等交互行为一律不变。

**验收标准**
- Given 步骤一落地，When 检查 QSS 与 objectName，Then 消息流容器规则存在且观感与 `QListWidget` 一致（选择器与关键属性断言）。
- Given 步骤二 + FR-06 落地，When 渲染一条含长文本/URL/媒体的消息，Then 字体非等宽、时间按约定呈现、文本完整不裁切、无横向滚动条（复用 2.0.4/2.0.1 行高与折行回归用例）。
- Given 点击消息，When 触发 `message_selected`，Then 详情面板正常显示（交互无回归）。

**改动锚点**：`message_view.py:102-125`（view 初始化，设 objectName）、`message_item_delegate.py`（排版语言，与 FR-06 同一文件同一 PR）、`message_list_model_format.py:62-102`（时间呈现）、`style.qss`（新增 QListView 专属规则）。

### FR-06 重构 MessageItemDelegate 布局计算（A4=档位 2 已确认纳入本期）

**背景（代码事实）**：现 `paint()`（`:84-121`）与 `sizeHint()`（`:123-158`）各自 `new QTextDocument` 并各自 `setHtml(_plain_to_html(text))`，靠共享一个纯函数 + 手动清缓存维持"画的高度 = 报的行高"这一**隐式契约**。该契约横跨 5 个输入维度（HTML 结构 / 字体 / 宽度 / padding / 缓存 key），`_plain_to_html` 只锁住其中 1 维；四个漏改点已被逐条定位（字体缓存命中短路 `:105-106`、宽度 fallback 不一致 `:135` vs `:119`、`+8` 单边 padding `:148`、无 DTO 第三路径 `:151-158`）。本 FR 从结构上消除该契约，而非加护栏兜住。

**业务规则**
- 抽出单一布局入口 `_layout(text, font, width) -> QTextDocument`，内部负责：new doc → `setDefaultFont(font)` → `setHtml(_plain_to_html(text))` → `setDocumentMargin(4)` → `setTextWidth(width)`；缓存命中直接返回**同一个 doc 实例**。
- 统一缓存：删除 `_doc_cache` / `_size_hint_cache` 双缓存，改为单一 `_layout_cache`，key 含 `(cid, mid, width, font_key)`；保留 FIFO LRU 上限 200（`_DOC_CACHE_MAX` 语义不变）与 `clear_caches()` 公开接口。
- `paint()` 与 `sizeHint()` 都只调 `_layout()`：`sizeHint` 取 `doc.size().height()`（padding 已由 documentMargin 吸收，去掉 `+8` 魔数）；`paint` 复用同一 doc `drawContents`，不再二次 `setTextWidth`。
- 收编无 DTO 的第三路径：model 未给 DtoRole 时仍走 `_layout()`（用稳定 fallback key），不再复制一遍 new doc 逻辑。
- 字体一致性：font_key 纳入缓存 key（或 font 变化时统一失效），消除"缓存命中跳过 `setDefaultFont`"的短路。
- 保持公开契约不变：`clear_caches()`、`_DOC_CACHE_MAX`、`_plain_to_html` staticmethod、`paint` / `sizeHint` override 签名均保留（有外部引用与测试依赖）。
- 保留 FR-03 步骤二的排版语言调整目标（去等宽 / 无衬线 / 灰字时间），但落在本新结构上。

**验收标准**
- Given 同一 `(cid, mid, width, font)`，When 连续调用 `sizeHint()` 与 `paint()`，Then 两者拿到的是**同一个 QTextDocument 实例**（注入 spy 断言 `_layout` 只 new 一次），从结构上证明一致性。
- Given 字体/宽度变化，When 以不同 font 或 width 调用，Then 缓存 key 不同、各自重算，且不出现"画用旧字体、量用新字体"（构造 font 切换场景断言 doc 的 font 与入参一致）。
- Given 长文本 / URL / 无空格长串 / 带媒体 / 多行手工换行消息，When 渲染，Then 文本完整不裁切、无横向滚动条（复用 2.0.4 FR-01 与 2.0.1 折行回归用例，字号按 FR-03 步骤二调整后重验）。
- Given 无 DtoRole 的 model 直接给 FormattedRole，When 渲染，Then 走 `_layout()` 且不崩、行高正确（第三路径收编验证）。
- Given 数据全表失效（`clear_caches()`）与单元格局部更新（`dataChanged`），When 刷新，Then 缓存正确失效、无 stale 高度（复用既有缓存契约用例）。
- Given PR #9 perf 基线，When 跑行高/paint 缓存命中性能测试，Then 缓存命中仍 O(1)、单 doc 复用无数量级退化（对比重构前基线；缓存粒度变细导致的额外 miss 需在可接受范围并记录实测数值）。
- Given 全套 pytest，When 运行，Then 基线不下降（G5），ruff / mypy 全绿。

**改动锚点**：`message_item_delegate.py:41-175`（整体重构，类内保持不变的公开契约见上）、`tests/`（新增布局契约 + perf 基线用例）。

**风险与回滚**：FR-06 与 FR-03 步骤二同一 PR，回滚粒度为整个 PR（退回后 FR-03 退化为仅步骤一 D2-α 状态）。重构本身不依赖 FR-03 步骤二的结果，若样式调整先出问题可只回退 `message_list_model_format.py` 的时间呈现部分。perf 退化触发条件与处置登记在 RK-06。

### FR-04 修正面板标题高度与位置（A3=α 已确认）

**业务规则**
- 给「最近活动」「频道统计」两个 QGroupBox 设专属 objectName（如 `dashPanel`），写专属 QSS：标题放回框内（不使用 `subcontrol-origin: margin`），收紧 `padding-top`（目标 ≤12px），保持标题左对齐与强调色。
- 全局 `QGroupBox` 规则保持不变，设置页分组框样式不受影响（A3 否决 β）。
- 两个 panel 的 `title()` 文本保持"最近活动"/"频道统计"不变（不用 γ 换容器方案）。

**验收标准**
- Given 大盘已构造，When 检查两个 panel 的 objectName 与 QSS 规则，Then 命中 `dashPanel` 专属样式且不含 `subcontrol-origin: margin`。
- Given 大盘渲染，When 读 `g.title()`，Then 仍为 ["最近活动", "频道统计"]（`test_dashboard_layout_2004` 断言继续绿）。
- Given 设置页已构造，When 检查其 FormSection 分组框，Then 样式与 2.0.4 一致（无连带变化）。

**改动锚点**：`dashboard_widget.py:275/287`（设 objectName）、`style.qss:408-424` 之后（新增 `QGroupBox#dashPanel` 专属规则，不改全局规则）。

### FR-05 测试与契约同步

**业务规则**
- 新增契约测试文件（建议 `tests/test_dashboard_layout_2005.py`）覆盖 G1~G5 每条至少 1 例；**G6 的布局契约与 perf 用例另立 `tests/test_message_delegate_layout_2005.py`（FR-06）**。
- 既有 `test_dashboard_layout_2004.py` 的标题文本、单实例、布局顺序断言必须继续绿；如因 D2=γ / FR-06 调整了消息渲染断言，逐条登记到 RTM。
- 全套 pytest 基线不下降；ruff / mypy 全绿。
- 版本 bump + CHANGELOG 另立 PR（不在本期实施）。

**验收标准**：全套 pytest 通过；新增契约测试覆盖 G1~G6；CI 三平台门禁全绿。

---

## 八、非功能需求

| 类别 | 要求 |
|---|---|
| 视觉一致性 | 消息流与最近活动在容器观感上一致；按 A2=γ 时字体与时间呈现也一致；尺寸数值按真机（Win/mac/Linux）微调，避免跨平台字体度量差异导致裁切 |
| 性能 | FR-06 重构后，**单一 `_layout()` 缓存命中仍为 O(1)**、单 doc 由 paint/sizeHint 复用不再重复 `setHtml`；缓存粒度变细（含 width/font）带来的额外 miss 必须有实测数据且不造成数量级退化。重跑 PR #9 行高/paint 基线（改造前命中 ~0.1ms、未命中 ~5-10ms 为参照），实测数值登记到 RK-06。FR-01~05 不得引入额外绘制开销。 |
| 兼容性 | 三平台 QSS 生效一致；Qt/PySide6 版本兼容沿用 2.0.4 已解禁的 6.12 结论，不引入新 Qt 特性 |
| 可测试性 | 几何/样式断言可在 offscreen QPA 下运行；依赖 `tests/fixtures/_real_window.py` 的真实窗口装配 |
| 架构 | 不改 core；`core/` 禁止 import PySide6/qasync 的守门测试保持绿；跨边界仍只传 DTO |

---

## 九、业务流程与状态流转

本期为纯 UI 视觉/布局调整，无新增业务流程与实体状态流转。大盘装配顺序（本期调整后）：

```
DashboardWidget._build()
  根布局(root, margin≈16, spacing≈12)
   ├─ [FR-01 删除] 原 "操作台" 标题
   ├─ 统计卡行(QHBoxLayout, spacing≈10)：5 张压缩卡（FR-02）
   ├─ live_splitter(stretch 3)：MessageView | MessageDetail（FR-03 样式统一）
   └─ bottom_row(stretch 2)：
        ├─ QGroupBox#dashPanel "最近活动"（FR-04 标题归位）
        └─ QGroupBox#dashPanel "频道统计"（FR-04 标题归位）
```

**异常分支**：样式/布局为静态配置，无运行时状态异常；唯一需防的是 delegate 排版调整引入的文字裁切/横向滚动回归（FR-03 护栏 + 回归用例覆盖）。FR-06 把这道防护从"护栏 + 回归用例"升级为"单一布局入口的结构性保证"——paint 与 sizeHint 读同一 doc，从源头消除"画的高度 ≠ 报的行高"。

---

## 十、数据与埋点需求

本期**不新增**数据字段、实体、表或埋点。所有改动为 UI 呈现层（QSS / widget 尺寸 / delegate 呈现），不触及存储、DTO、事件覆盖口径。

- 数据展示口径（5 张卡、活动、频道统计）与 2.0.4 完全一致。
- 无新增指标口径、无埋点事件、无上报字段。

---

## 十一、权限与安全要求

- 单机桌面应用，无多用户/权限模型；本期不引入账号体系。
- 不新增数据收集与上报；活动详情不新增展示来源；无跨境/个人信息外发场景。
- 无新增合规风险点。

---

## 十二、异常处理清单

| 场景 | 处理 |
|---|---|
| 卡片压缩后数值/副文本被裁 | 用 QFontMetrics 校验最小高度；建议值留出余量，真机微调 |
| 字体/行度量跨平台差异 | 尺寸为建议值，按三平台真机视觉微调；保留 delegate 宽度感知行高（FR-03 护栏） |
| 消息流排版调整引入文字裁切/横向滚动 | FR-06 单一 `_layout()` + 容器宽 `setTextWidth` + documentMargin 吸收 padding；禁 `idealWidth()`；复用既有回归用例 |
| FR-06 重构后 paint/sizeHint 高度再次不一致 | 结构上两处读同一 doc 实例，契约不再靠人维持；用 spy 断言 `_layout` 只 new 一次 |
| FR-06 缓存粒度变细致 miss 增多、绘制变慢 | 重跑 PR #9 perf 基线，实测登记 RK-06；doc 复用抵消二次 setHtml 开销，如显著退化则评估 font_key 归一化 |
| 改 QSS 波及设置页 | A3=α 局部 objectName，全局 QGroupBox 不动；用设置页样式断言守护 |
| `pageTitle` 被误删全局规则 | 只删大盘单个 QLabel 节点，`style.qss:284` 规则保留；用其他页面 pageTitle 计数断言守护 |
| 版本 bump 遗漏 | 按 2.0.4 惯例另立 PR，六处版本落点同步（pyproject / `__init__` / uv.lock / spec / AGENTS.md / CHANGELOG） |

---

## 十三、上线计划、依赖与上线后验证

### 13.1 依赖就绪度评估

| 依赖项 | 所属 | 就绪状态 | 分级 | 最晚就绪时限 | 影响与替代 |
|---|---|---|---|---|---|
| QSS 加载链路 | 本项目 UI | 已确认（`style.qss` 全局加载，objectName 选择器可用） | 可绕过 | — | 无需替代 |
| `_real_window` 真实窗口 fixture | 本项目测试 | 已确认可用（`tests/fixtures/_real_window.py`） | 可绕过 | — | offscreen 几何断言替代 |
| 2.0.4 行高/折行回归用例 | 本项目测试 | 已存在（消息折行、活动裁切相关） | 并行 | FR-03 β 步骤合并前 | 复用并按需补强，防排版回归 |
| PR #9 perf 基线用例 | 本项目测试/历史 | 需确认是否存在可复用的计时用例；若无则在 FR-06 PR 补一条采样脚本产出基线数值 | 并行 | FR-06 合并前 | 重构前先采基线，重构后对比；无基线则用改造前代码同机采样 |
| QTextDocument.documentMargin 行为 | Qt/PySide6 6.12 | 已确认可用（Qt 标准 API，替代手写 `+8`） | 可绕过 | — | 若三平台度量差异异常，回退为在 `_layout` 内统一加 padding 的确定性写法 |
| 三平台真机视觉微调 | 环境 | 需实施者在 CI 三平台产物 + 本地验证 | 并行 | 发布前 | 用固定尺寸几何断言兜底 |

> 本期无外部系统阻塞依赖。

### 13.2 建议 PR 切分（每分支一主题，squash merge）

1. `ui/dashboard-header-and-panels-2005` — FR-01 + FR-04（去标题 + 面板标题归位，纯 QSS/布局，低风险）
2. `ui/dashboard-stats-compact-2005` — FR-02（卡片压缩与空间释放）
3. `ui/message-stream-container-2005` — FR-03 步骤一（消息流 QSS 容器观感，纯 QSS，低风险）
4. `refactor/message-delegate-layout-2005` — FR-06 + FR-03 步骤二（delegate 结构性重构 + 排版语言调整，同一 PR；先采 perf 基线再改，含布局契约与 perf 用例）
5. 随各 PR 带上 FR-05 对应测试；最后单独 PR 做版本 bump + CHANGELOG。

> PR 4 是本版唯一的中高风险 PR，也是唯一动绘制核心的 PR；其余三个 PR 为纯 QSS/布局，风险互不耦合。若 PR 4 触发不可接受的回归，可整体回退至 PR 3 状态（消息流仅容器观感统一）。

### 13.3 上线后验证清单

- [ ] 本地全套 pytest + ruff + mypy 全绿；CI 三平台 8/8 全绿
- [ ] 真机启动：大盘无"操作台"标题，卡片更紧凑，消息流可视区更大
- [ ] 消息流与最近活动观感统一（按 A2 决策维度核验）
- [ ] 「最近活动」「频道统计」标题贴框内、不再悬空，面板高度正常
- [ ] 设置页分组框样式无连带变化
- [ ] 长消息/URL/媒体消息无裁切、无横向滚动；点击消息详情正常
- [ ] FR-06 布局契约：spy 断言 paint/sizeHint 读同一 doc 实例，四个漏改点的针对性用例均绿
- [ ] PR #9 perf 基线对比：缓存命中仍 O(1)，实测数值登记 RK-06 且无数量级退化

---

## 十四、风险与遗留问题

### 14.1 风险登记册

| 编号 | 风险描述 | 类别 | 概率 | 影响 | 等级 | 应对措施 | 责任人 | 状态 |
|---|---|---|---|---|---|---|---|---|
| RK-01 | ~~D2=γ 的 β 步骤动 delegate 富文本/行高，触发 2.0.4 已修的文字裁切回归~~ → **A4 后升级为结构性消除**：FR-06 单一 `_layout()` 让 paint/sizeHint 读同一 doc，隐式契约从源头消失；残余风险转为"重构过程本身引入新 bug"，由回归用例 + 同 PR 回滚兜住 | 技术 | 2 | 4 | 8 中→低 | FR-06 结构保证 + 复用行回归用例；PR 4 整体可回退至 PR 3 状态 | 实施方 | A4 后降级 |
| RK-06 | FR-06 缓存粒度变细（key 含 width/font）导致 miss 增多、绘制变慢；或 documentMargin 在三平台度量异常 | 性能/兼容 | 3 | 3 | 9 中 | 重构前先采 PR #9 perf 基线，重构后同机对比并记录实测；doc 复用抵消 setHtml 开销；三平台 CI + 真机视觉验证；异常时 font_key 归一化或回退确定性 padding 写法 | 实施方 | 待处理 |
| RK-02 | 改全局 QGroupBox 波及设置页 | 体验 | 3 | 3 | 9 中 | A3=α 局部 objectName，全局规则不动；设置页样式断言守护 | 实施方 | 待处理 |
| RK-03 | 压缩尺寸在三平台字体度量下出现裁切/过挤 | 体验 | 3 | 3 | 9 中 | 尺寸为建议值留余量，三平台真机微调；QFontMetrics 校验 | 实施方 | 待处理 |
| RK-04 | 误删全局 `#pageTitle` 规则影响其他页面 | 技术 | 2 | 3 | 6 低 | 只删大盘单个 QLabel；其他页面 pageTitle 计数断言守护 | 实施方 | 待处理 |
| RK-05 | 消息流 `QListView` 新增 QSS 规则与 QListWidget 规则重复/冲突 | 技术 | 2 | 2 | 4 低 | 用独立 objectName 选择器，避免通配；QSS 断言守护 | 实施方 | 待处理 |

### 14.2 遗留问题

- D1-β/γ（精简或重排卡片）不在本期，触发条件见 §5.5。
- D2=α 时字体/时间呈现不一致仍保留，仅容器观感统一（若需求方选 α，此为已知接受项）。
- 设置页分组框样式不随本期调整（A3=α），如需求方希望全局统一，另立需求。
- 档位 3（去富文本层）本期不做，触发条件见 §5.5；FR-06 保留 `_plain_to_html` 接口，为后续档位 3 留演进空间。
- 版本 bump / CHANGELOG / tag 不在本期，按 2.0.4 惯例另立 PR；当前 tag 仍为 v2.0.4，PR #69 的 bug 修复随下个版本一起发布。

---

## 十五、附录

### 附录 A — 诊断证据索引（只读排查结论）

| 结论 | 证据 |
|---|---|
| "操作台"标题为独立 QLabel，objectName=pageTitle | `dashboard_widget.py:225-228` |
| pageTitle 被多处复用，不能删全局规则 | `message_detail.py:145`、`settings_page.py:102`、`form_row.py:268`、`ui_assembler.py:146`、`style.qss:284` |
| 5 张卡片固定 100px 高、最小宽 160 | `dashboard_widget.py:73-115`（`:82` setFixedHeight(100)） |
| 根布局边距/间距与卡片间距 | `dashboard_widget.py:220-232`（margin 24 / spacing 20 / cards spacing 16） |
| 消息流与底部块 stretch 3:2 | `dashboard_widget.py:268/308` |
| 消息流为 QListView + 等宽 `<pre>` delegate | `message_view.py:80/102-125`、`message_item_delegate.py:123-175`（`:172-175` pre-wrap） |
| 消息流时间内嵌富文本 head | `message_list_model_format.py:62-102`（`:79` `⏱ {dt}`） |
| QSS 只定义 QListWidget，无 QListView 规则 | `style.qss:427-447` |
| 最近活动为灰字时间独立列 + 图标 + 无衬线文本 | `dashboard_widget.py:151-163`、`style.qss:509-512`（role=hint） |
| 面板标题骑在框外 margin 且 padding-top 20 | `style.qss:408-424` |
| QGroupBox 被设置页 FormSection 复用 | `form_section.py:153` |
| 既有布局测试用 title() 断言 | `tests/test_dashboard_layout_2004.py:68-76` |

### 附录 B — 需求追踪矩阵（RTM）

| 需求 | 来源 | PRD 章节 | 改动锚点 | 验收用例 | 状态 |
|---|---|---|---|---|---|
| R-01 去操作台标题 | 用户诉求 1 | FR-01 §7 | `dashboard_widget.py:225-228` | `test_dashboard_layout_2005`（G1） | 待实施 |
| R-02 卡片压缩/省空间 | 用户诉求 1 | FR-02 §7 | `dashboard_widget.py:73-115/220-249` | 几何断言（G2） | 待实施 |
| R-03 消息流样式统一 | 用户诉求 2 | FR-03 §7 | `message_view.py`/`message_item_delegate.py`/`style.qss` | QSS 断言 + 行回归（G3） | 待实施 |
| R-04 面板标题归位 | 用户诉求 3 | FR-04 §7 | `dashboard_widget.py:275/287`、`style.qss` | 专属 QSS 断言 + 2004 标题断言（G4） | 待实施 |
| R-05 测试同步 | 衍生 | FR-05 §7 | 新增 `test_dashboard_layout_2005.py`、`test_message_delegate_layout_2005.py` | 全套 pytest | 待实施 |
| R-06 delegate 布局重构 | 第 2 轮评审 A4 | FR-06 §7 | `message_item_delegate.py:41-175` | `_layout` 单实例 spy + 四漏改点用例 + perf 基线（G6） | 待实施 |

### 附录 C — 评审决策记录

**第 1 轮（2026-11-17，已确认）**

| 编号 | 决策点 | 推荐 | 需求方选择 | 落地影响 |
|---|---|---|---|---|
| **A1** | 顶部卡片优化方案（D1） | α 压缩保留 5 卡 | **α（采纳）** | FR-02 按压缩实施：高度 100→约 68、字号与间距收紧，5 卡全保留、口径不变、stretch 3:2 不动 |
| **A2** | 样式一致范围（D2） | γ 两者都对齐（分两步） | **γ（采纳）** | FR-03 分两步：QSS 容器 + delegate 排版；守宽度感知行高护栏，复用裁切/折行回归用例 |
| **A3** | 面板标题修复范围（D3） | α 局部 objectName | **α（采纳）** | FR-04 只改大盘两块的 `dashPanel` 专属 QSS，设置页零影响，全局 QGroupBox 不动 |

**第 2 轮（2026-11-18，已确认）**

| 编号 | 决策点 | 背景 | 需求方选择 | 落地影响 |
|---|---|---|---|---|
| **A4** | delegate 结构性重构是否纳入本期 | 第 1 轮把 D2-γ 步骤二留在旧双路径结构上，靠护栏兜隐式契约；第 2 轮讨论确认该结构本身脆弱（共享只锁 5 维中的 1 维、四漏改点），建议在旧结构上改样式等于把已知风险留在原地 | **纳入本期（采纳）** | 新增 FR-06 + G6 + RK-06；FR-03 步骤二并入 FR-06 同一 PR（PR 4）；RK-01 降级；perf 基线需实测；档位 3 仍留后续 |

### 附录 D — 合规声明

本方案为产品设计参考，具体实施请结合公司合规与法务要求确认。本文所述改造均为原创设计，未复制任何竞品的专有文案、视觉或代码。
