# PRD — tgmonitor 2.0.7「顶部与设置页减法 + 频道管理图标化」版本规划

## 一、文档信息

| 项 | 内容 |
|---|---|
| 版本 | v1.0（初稿，基于只读代码诊断；决策点按推荐组合展开，待用户拍板） |
| 目标版本 | tgmonitor 2.0.7 |
| 基线版本 | 2.0.6（`main = 506fb77`，tag `v2.0.6` 已推送；工作树版本落点均为 2.0.6） |
| 作者 | 司南（产品经理 Agent） |
| 需求方 | forcetone |
| 日期 | 2026-11-19 |
| 状态 | **待评审**：7 项诉求已归入 11 条 FR；7 个决策点均给出推荐，本文按推荐组合撰写，用户确认后转「可实施」 |

**变更记录**

| 版本 | 日期 | 变更摘要 |
|---|---|---|
| v1.0 | 2026-11-19 | 初稿。对 7 项诉求逐条做只读代码诊断（大盘/详情/设置页/频道管理/凭证/保存入口六组，证据均带文件:行号），归并为 11 条 FR + 7 个决策点，按推荐组合（D1=α / D2=β / D3=β / D4=β / D5=α / D6=α / D7=α）展开完整 15 段 PRD。 |

**评审记录**：暂无（第 1 轮待进行）。

> 诊断方式声明：本文「现状」结论全部来自对 `506fb77` 工作树的只读排查，未改源码、未运行全套测试；「改动锚点」是实施落点建议，非已落地事实。尺寸、颜色值、间距为建议值，实施时按三平台真机视觉微调，但**语义与结构约束（一行、单滚动、图标按钮、无 ID、无双击）不可微调掉**。

---

## 二、背景与目标

### 2.1 背景

2.0.6 完成「撤频道页 + 监听管理迁入设置页 + 命名正名 + 全局装饰 emoji 清理」后，应用进入 3 页结构（`大盘 / 媒体 / 设置`）。本次用户基于实际使用提出 7 项减法型诉求：

- **大盘顶部信息仍偏重**：5 张卡片每张 2 行（标题行 + 数值行，`dashboard_widget.py:91-119,245-263`），其中「消息总数」卡与底部「频道统计」表同源同口径（`:557,565-572`），是重复信息；登录状态在 2.0.6 全局去 emoji 时把 🟢/🔴/🟡 替换成单色 `●/×/…`（`state_labels.py:33-46`，提交 `3a9b266`），状态色 QSS 成了零消费者死规则（`style.qss:552-563`），所以用户看到「只有黑色」。
- **消息详情出现双层滚动**：正文 `QPlainTextEdit` 被 `setMaximumHeight(220)` 钳死（`message_detail.py:238-244`），长文本在正文框内出一条纵向滚动条，外层 `MessageDetail(QScrollArea)` 还有一条（`:81,100`），与「文字本就应完整展示」的心智冲突。
- **设置页视觉未收敛**：页面标题 `#pageTitle` 在滚动区外（`settings_page.py:109-113`）；灰底来自 `#contentArea`（`style.qss:23-30`，`ui_assembler.py:111`），设置页滚动内容透出灰底；`QGroupBox::title` 骑在上边框外的 margin 区（`style.qss:408-425`），而大盘面板早已验证「标题在卡片内左上」的做法（`:426-454`）。分组内边距、行距、底栏双按钮也存在规整空间。
- **频道管理交互仍是旧范式**：两栏**上下**堆叠（`channel_widget.py:397,423,435`），底部有「双击一行 → …」提示（`:244-248,419,431`），行文本含 `· {ch.id}`（`:138-154`），加入/移出靠双击（`:232,344-347,422,434,587-607`）——与用户期望的「左右布局 + 行尾加减图标 + 不展示 ID + 无双击」不一致。
- **凭证与保存入口有冗余**：API ID 是 `QSpinBox` 默认带上下箭头（`form_row.py:128-136`）；底部「仅保存到 .env」按钮（`:153-155`）会把同一份设置写盘但不热重载（`settings_page.py:640-691` vs `:693-735`），用户判断其「无实际意义」。

### 2.2 要解决的问题

一句话：**在不碰 core/存储/导航的前提下，把大盘、消息详情、设置页的冗余信息层与多余交互层剥掉，让信息只出现一次、状态可辨、操作靠显式按钮。**

1. 顶部减负：卡片从 2 行压到 1 行、5 张减到 4 张；恢复登录状态红/绿等语义色（用 QSS，不用 emoji）。
2. 正文去内层滚动：长文本由外层详情面板统一承接，正文区域自撑高。
3. 设置页换底：去页面标题、去灰底、分组标题内移、间距统一。
4. 频道管理换交互：左右布局、行尾 `+`/`−` 图标、无 ID、无底部提示、无双击。
5. 去掉两处噪音：APPID 箭头、「仅保存到 .env」按钮。

### 2.3 目标与成功指标（口径明确）

| 目标 | 成功指标（可断言） | 口径与来源 |
|---|---|---|
| G1 顶部单行 + 减卡 | 顶部卡片数 `== 4`；每张卡内部只有 **1 个** `QHBoxLayout` 内容行（标题 + 数值同排）；`card_total_messages` 属性与「消息总数」文案在生产代码中命中数为 0 | UI 对象树断言 + grep；改造 `test_dashboard_stats_compact_2005.py:74-89`、`test_dashboard_cards_refine_2005.py:23-46` |
| G2 状态色恢复 | `state_labels.py` 有唯一颜色映射；大盘状态卡数值 label 与状态栏状态 label 按状态设置 `property("state", …)`；QSS 中 ready/error/pending/unset 四档规则存在；**彩色 emoji 命中数仍为 0** | QSS 文本断言 + widget property 断言 + `test_no_decorative_emoji.py` 保持绿 |
| G3 正文单滚动 | 正文中不再有 `QPlainTextEdit`；正文 `QLabel` 开启 `wordWrap` 且 `sizeHint().height()` 随 200 行长文本增大；正文区域内 `verticalScrollBar().isVisible() == False`，外层 `MessageDetail` 滚动条仍可见 | offscreen Qt 断言 + 改造 `test_message_detail_header_2005.py:28-30` |
| G4 设置页收敛 | 设置页无 `#pageTitle` 标题 label；页面背景为白（`#ffffff`）；分组卡片有专属 objectName 且 QSS 标题 `subcontrol-origin: padding`；`设置` 文案不再出现在 UI label | 对象树 + QSS 文本断言 + 改造 `test_settings_no_emoji_2006.py:51` |
| G5 频道管理换范式 | 两卡父级同一 `QHBoxLayout`；`bottom_hint` 参数与两条双击提示文案命中数为 0；行文本不含 `· {id}` 也不含 chat_id 数字；每行右侧有 `+`/`−` 按钮；`itemDoubleClicked` 连接数为 0 | UI 遍历 + grep + 改造 `test_channel_widget.py:171-219,305-332`、`test_main_window_channels.py:768-782` |
| G6 凭证与保存 | `api_id` 输入框 `buttonSymbols() == QSpinBox.ButtonSymbols.NoButtons`；`btn_save_env` / `_on_save_env` 在生产代码中命中数为 0；core `validate_backends` 保留 | `test_form_row.py:148-177` 附近新增断言 + grep + `test_app_service_surface.py:30` 保持绿 |
| G7 无回归 | 全套 pytest 绿（本版刻意改造的断言除外）；ruff/mypy 绿；CI 8/8；三平台 UI 肉眼无裁切/无过窄 | 本地全套 + CI + 上线清单 |

**非目标（本期明确不做）**：不改 core/存储/导航结构、不改 DTO/事件/白名单算法；不删 `validate_backends`（core API 保留，`test_app_service_surface.py:30` 锁定）；不改原始 JSON 区域的 260px 滚动（`message_detail.py:270-280`，它是调试数据区，不是「文字正文」）；不恢复彩色 emoji（`test_no_decorative_emoji.py` 守卫）；不做版本 bump 与 Release（单独 PR，推 tag 才触发 Release）；不新增埋点与数据字段。

---

## 三、名词与术语定义

| 术语 | 定义 | 代码映射 / 证据 |
|---|---|---|
| 顶部统计卡 | 大盘首行的 `_StatCard(QFrame)`，objectName=`statCard` | `dashboard_widget.py:74-119`；QSS `style.qss:292-313` |
| 频道统计表 | 大盘底部 `QGroupBox("频道统计")` 内的 `QTableWidget`，列 = 频道/类型/消息数 | `dashboard_widget.py:319-343,567-572` |
| 登录状态 / 连接状态 | 登录态 12 值（`state_labels.py:33-62`）；连接态 5 值（`status_bar.py:53-59`）；状态栏 ready 时显示连接态、否则显示登录态（`status_bar.py:170-205`） | 本文「状态色」同时覆盖两者 |
| 正文 | 消息详情里的 `m.text` 区域，当前是 `QPlainTextEdit#detailTextEdit` | `message_detail.py:237-244` |
| FormSection | 设置页表单分组基类，`QGroupBox(title)` + `QVBoxLayout` + `QFormLayout` | `form_section.py:147-184` |
| 监听频道管理 | 2.0.6 从独立频道页迁入设置页的 `ChannelWidget` 单实例 | `settings_page.py:236-252`；`ui_assembler.py:146,158-163` |
| 订阅 / 监听 | 沿用 v2.0.3/v2.0.6 口径：订阅 = TG 已加入；监听 = 本应用白名单 | `channel_service.py:18-20`、`dto.py:36-39` |

---

## 四、需求范围

### 4.1 包含（本期做）

- FR-01：大盘顶部卡片单行化 + 删除「消息总数」卡（诉求 1 前半）。
- FR-02：恢复登录/连接状态语义色，颜色真值源唯一化（诉求 1 后半）。
- FR-03：消息详情正文去内层滚动条，长文本由外层面板统一承接（诉求 2）。
- FR-04：设置页去灰底 + 去页面标题（诉求 3 前半）。
- FR-05：分组标题移入卡片内左上方（诉求 3 后半）。
- FR-06：监听频道管理改左右布局 + 去底部提示（诉求 4 前半）。
- FR-07：行尾 `+`/`−` 图标加入/移出，去 ID 展示、去双击（诉求 4 后半）。
- FR-08：API ID 输入框去上下箭头（诉求 5）。
- FR-09：删除「仅保存到 .env」入口（诉求 6）。
- FR-10：设置页元素布局规整（诉求 7）。
- FR-11：测试与契约同步（跨全部 FR）。

### 4.2 不包含（本期明确不做）

- core 服务、存储层、物理列、迁移、EventBus 语义、导航项数量与索引。
- 删除 `validate_backends` / 改「保存并应用」的热重载链路（`settings_page.py:693-735` 不动）。
- 原始 JSON 区域滚动条、消息列表 delegate、灯箱/媒体预览布局。
- 彩色 emoji 回归、`statusDot` 死规则复活（应借本期清理，`style.qss:44-48,552-563`）。
- 新增监听数量上限、配额、埋点、版本号 bump 与 tag。

---

## 五、方案对比与选型说明

> 本文按 **推荐组合 D1=α / D2=β / D3=β / D4=β / D5=α / D6=α / D7=α** 展开后续章节；每个决策点独立，若用户改判某点，只替换该点对应 FR 章节与验收，其余不连带变更。

### 5.1 D1：顶部单行卡片的形态（诉求 1）

| 方案 | 做法 | 评价 |
|---|---|---|
| **α 保留图标，标题 + 数值同排（推荐）** | `_StatCard` 内由 2 个 `QHBoxLayout` 改为 1 个：图标 16px + 标题 + 弹簧 + 数值；卡高建议 44–48px；删「消息总数」卡 | **推荐**：保留频道/状态图标的扫读价值，仍满足「只保留一行」 |
| β 去图标，纯文字标题 + 数值 | 更极简，但失去类型区分 | 备选：若 α 视觉仍拥挤再降一级 |

「消息总数」删除依据：它与「频道统计」表第三列同源同口径——`_fetch_channel_rows()` 一次返回 `rows, total`（`dashboard_widget.py:557`），`_apply_stats_table()` 同时写顶卡 `:565` 与表格 `:567-572`，`total == sum(row[2] for row in rows)`。删顶卡不丢信息。

### 5.2 D2：状态色恢复的范围与实现（诉求 1）

| 方案 | 做法 | 评价 |
|---|---|---|
| α 只给大盘状态卡上色 | 改一处 QSS + 一处 property | 状态栏仍黑，用户「登录状态颜色缺失」只解一半 |
| **β 大盘状态卡 + 状态栏状态位同时上色（推荐）** | `state_labels.py` 新增唯一 `STATE_COLOR` 四档映射；两处 label 设置 `property("state", bucket)`；QSS 新增 `#statValue[state=…]` 与 `#statusStateLabel[state=…]` | **推荐**：状态语义一处定义、两处呈现；颜色恢复但不走 emoji |

颜色分档（建议值，实施可微调色号但档位不变）：ready / `TG 已连接` → 绿 `#16a34a`；error → 红 `#dc2626`；`*_required` / connecting / updating / 过渡态 → 琥珀 `#f59e0b`；closed / uninit / unknown → 灰 `#94a3b8`。**现状之所以「只有黑色」**：2.0.6 FR-09 把 🟢/🔴/🟡 换成单色 `●/×/…`（提交 `3a9b266`，`state_labels.py:33-46`），而 QSS 里的绿/红规则只命中已被删除的 `QFrame#statusDot`（`style.qss:552-563`），状态栏 label 从未设色（`status_bar.py:198-204`）。

### 5.3 D3：正文去内层滚动的控件选择（诉求 2）

| 方案 | 做法 | 评价 |
|---|---|---|
| α 保留 `QPlainTextEdit`，去 `setMaximumHeight(220)` 并按 `document().size().height()` 喂高度 | 改动小、测试少改 | `QPlainTextEdit.sizeHint()` 不随内容增长，需手动处理折行与宽度变化，脆弱 |
| **β 正文改 `QLabel` + `setWordWrap(True)`（推荐）** | `setTextFormat(Qt.TextPlain)` 防 `<`/`&` 被当 HTML；`setTextInteractionFlags(Qt.TextSelectableByMouse)` 保留复制；保留 `objectName="detailTextEdit"` | **推荐**：与详情内其它文本（媒体卡 `:250-252`、`_FieldRow :75-78`）同一范式，`sizeHint()` 天然随内容增长，内层滚动条自然消失；同步 2 个测试文件即可 |

两方案都依赖外层 `MessageDetail(QScrollArea)` + `widgetResizable=True`（`:81,100`）承接超高内容；**不改** QSplitter（`dashboard_widget.py:286-299`）。诊断已用 offscreen 探针确认：正文 220px 上限是内层滚动条的直接原因，外层滚动是第二层。

### 5.4 D4：设置页去灰底与标题（诉求 3）

| 方案 | 做法 | 评价 |
|---|---|---|
| α 只把滚动内容区刷白 | `scroll_content` 白底 | 底栏仍灰，视觉割裂 |
| **β 整页白底（推荐）** | 给 `SettingsPage` 设专属 objectName（如 `settingsPage`），QSS 令页面与滚动内容均 `#ffffff`；同时删除 `header_label`（`settings_page.py:109-113`） | **推荐**：一次到位；注意 `#pageTitle` 全局规则必须保留给其它页（`test_dashboard_header_panels_2005.py:47-50`） |

### 5.5 D5：分组标题内移与频道管理布局（诉求 3、4）

| 决策 | 方案 | 推荐 |
|---|---|---|
| 标题内移 | α：`form_section.py:153` 给 `gb` 加 `objectName="settingsSection"`，QSS 照搬 `QGroupBox#dashPanel`（`style.qss:426-454`）的 `subcontrol-origin: padding` + 左上定位 + 顶部留白；β：去掉 `QGroupBox(title)` 改 `QWidget` + 显式 `QLabel` | **α**：不动 Python 结构、不动 `gb.title()` 契约（`test_form_section.py:292-298`），全局 `QGroupBox` 规则仍保持 `margin-top:14px`（`test_dashboard_header_panels_2005.py:128-141`） |
| 左右布局 | α：外层保留 `QVBoxLayout`（搜索框独占一行），新增内层 `QHBoxLayout` 承载两卡，stretch 1:1；β：换 `QSplitter` 可拖拽 | **α**：改动集中在 `channel_widget.py:397-435`；`settings_page.py:251` 的 `setMinimumHeight(460)` 需复核下调，避免左右布局后纵向空洞 |

### 5.6 D6：频道行交互改造（诉求 4）

| 决策 | 方案 | 推荐 |
|---|---|---|
| 图标按钮 | α：`setItemWidget` 每行一个 `QWidget`（标题 Label + 右端 `QToolButton`），新增 `plus.svg`/`minus.svg`，复用 `tinted_action_icon()`（`icon.py:68-87`）；新信号 `item_action_clicked = Signal("qlonglong")`；β：`QStyledItemDelegate` 自绘 + 命中测试 | **α**：不破坏 `item.setData(Qt.UserRole, ch.id)` 与 64 位信号契约（`:180-183`）；需重点验证过滤隐藏、标题/头像增量刷新 |
| ID 展示 | α：行文本去 `· {id}`，保留 tooltip；β：行文本去 ID 且不加 tooltip（严格「不展示」） | **β**：用户明确「不展示频道ID」，tooltip 也算展示；ID 仍保留在 `Qt.UserRole` 供逻辑使用 |
| 双击 | α：保留 `item_double_clicked` 信号但断开连接（死 API）；β：删除信号与槽 | **β**：彻底去掉旧范式；同步 `test_main_window_channels.py:768-782` |

### 5.7 D7：凭证与保存入口（诉求 5、6、7）

| 决策 | 方案 | 推荐 |
|---|---|---|
| APPID 箭头 | α：在 `form_row.py:128` 后 `spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)`，仅对 INT 字段生效（当前只有 `api_id` 是 INT，见 `settings_page.py:175-181`），min/max/校验/收集/加载全不变；β：`api_id` 改 `QLineEdit` + `QIntValidator` | **α**：零逻辑风险，现有测试不破 |
| 「仅保存到 .env」 | α：只删 UI 按钮 + 底栏 + `_on_save_env`（`settings_page.py:153-155,640-691`）与模块 docstring；保留 core `validate_backends`；β：连 core API 一起删 | **α**：客观说明——该按钮**不是空操作**（会校验并写盘，`:661`），但与「保存并应用」写同一份 `.env` 且不热重载，用户判断无意义；core API 被 `test_app_service_surface.py:30` 锁定，不越界 |
| 布局规整 | α：保持 7 个分组顺序不变（`settings_page.py:127-136`），统一卡片内边距 16px / 行距 12px / 分组间距 16px / 底栏单按钮右对齐；β：重排分组顺序 | **α**：诉求是「规整、好看」而非改信息架构；顺序重排会牵动多处契约，列入后续 |

### 5.8 演进路径

- 本期（2.0.7）：纯 UI/QSS 减法与交互替换；不碰数据层。
- 后续（触发条件明确再立）：① 清理 `style.qss` 中 `#headerState`/`QFrame#statusDot` 死规则（本期顺手做，若测试切片受影响则拆出）；② 设置页分组顺序重排（需用户给出目标顺序）；③ 频道行支持键盘操作（当前只有按钮点击，可补 `Return` 触发）。

---

## 六、用户角色与使用场景

**用户画像**（基于需求描述构建，为待确认假设）：桌面端 tgmonitor 使用者，日常主流程是「登录 → 查看大盘 → 点消息看详情 → 偶尔调整监听与配置」，偏好信息密度低、状态一眼可辨、操作靠显式入口而非隐藏手势。

| 场景 | 用户目标 | 操作路径（改造后） | 触点 |
|---|---|---|---|
| S1 看大盘 | 快速掌握频道/监听/连接状态 | 打开应用 → 首行 4 张单行卡扫读；连接状态为红/绿/琥珀/灰 | FR-01/02 |
| S2 看长消息 | 完整读一条长文本 | 消息流点开 → 详情正文完整展示，只在详情面板右侧滚动；可鼠标选中复制 | FR-03 |
| S3 调监听 | 加入/移出监听频道 | 设置 → 监听频道管理 → 左栏目标行点 `+` / 右栏目标行点 `−` | FR-06/07 |
| S4 改配置 | 改凭据/代理/存储等并立即生效 | 设置 → 填表 → 点「保存并应用」 | FR-04/05/08/09/10 |
| S5 未登录 | 知道当前该做什么 | 大盘状态卡显示灰色「未登录」；状态栏同色；设置页登录按钮可见；频道行按钮禁用 | FR-02/07 |

---

## 七、功能需求（编号 + 业务规则 + GWT 验收）

### FR-01 大盘顶部单行化与删除「消息总数」卡（D1=α）

- 业务规则：
  1. `_StatCard`（`dashboard_widget.py:74-119`）内部由「标题行 + 数值行」两行合并为一个 `QHBoxLayout`：图标 + 标题 + 弹簧 + 数值；删除 `sub_lbl` 残留（v2.0.5 已删）与双值逻辑中不必要的第二行。
  2. 删除 `self.card_total_messages = _StatCard("消息总数", ...)`（`:260-261`）及其两处写入（`:521`、`:565`）；`_apply_stats_table(rows, total)` 的 `total` 参数改为只用于表格内部或一并移除（若移除，需保证 `:551-552` 的聚合仍供表格使用）。
  3. 剩余 4 张卡顺序：已加入频道 / 已监听 / 连接状态 / 存储后端；QSS `#statCard` 高度下调（建议 44–48px），`#statValue` 字号可下调但保持 700 字重（`style.qss:292-313`）。
- 验收标准（GWT）：
  - **Given** 主窗口构建完成，**When** 遍历大盘顶部卡片容器，**Then** 卡片数为 4、每张卡内容行数为 1、无 `card_total_messages` 属性。
  - **Given** 频道统计表有 N 行，**When** 查看顶部，**Then** 不再出现「消息总数」文案；底表「消息数」列仍逐行正确（`test_ui_simplification_2003.py:344-369` 的 `rows`/表格断言保持语义）。
  - **Given** 任一 1280×800 三平台窗口，**When** 查看首行，**Then** 卡片文字无裁切（改造 `test_dashboard_stats_compact_2005.py:93-106`）。

### FR-02 登录/连接状态语义色恢复（D2=β）

- 业务规则：
  1. `state_labels.py` 新增唯一 `STATE_COLOR: dict[str, str]`（12 登录状态 → 四档色）与 `state_color_bucket(state) -> str`；连接态 5 值（`status_bar.py:53-59`）复用同档位映射，避免第二套表。
  2. 大盘状态卡数值 label 设置 `setProperty("state", bucket)`（`dashboard_widget.py:451-453,386-398` 两处写入点都要设）；状态栏 `_UnifiedStateLabel._render()`（`status_bar.py:198-204`）同样设置 property。
  3. QSS 新增 `#statValue[state="ready"]{color:#16a34a}` 等四档规则 + `#statusStateLabel[state="…"]` 四档规则；颜色值沿用 `style.qss:8-13` 注释里已有的 Tailwind 色板，**不引入彩色 emoji**（守卫 `test_no_decorative_emoji.py:29-35,53-65,68-92`）。
  4. 顺手删除零消费者规则 `#headerState`（`style.qss:44-48`）与 `QFrame#statusDot[state=…]`（`:552-563`）及顶部过时注释；若 QSS 切片测试受影响则拆到独立 PR。
- 验收标准（GWT）：
  - **Given** 登录态 `ready`，**When** 读取大盘状态卡与状态栏状态 label，**Then** 两者 `property("state")=="ready"` 且 QSS 能解析到绿色规则；`error` 解析到红色。
  - **Given** 12 登录态 + 5 连接态全集，**When** 逐态调用 `state_color_bucket()`，**Then** 每态返回四档之一，无 KeyError、无未映射值。
  - **Given** 全仓扫描，**When** 检查 UI 文案装饰 emoji，**Then** 命中数仍为 0（守卫保持绿）。

### FR-03 消息详情正文去内层滚动（D3=β）

- 业务规则：
  1. `message_detail.py:237-244` 的 `QPlainTextEdit(m.text)` 改为 `QLabel(m.text)`：`setObjectName("detailTextEdit")`、`setWordWrap(True)`、`setTextFormat(Qt.TextPlain)`、`setTextInteractionFlags(Qt.TextSelectableByMouse)`；**不再设置** `setMaximumHeight(220)` 与 `QFrame.NoFrame`（Label 无 frame）。
  2. 外层 `MessageDetail(QScrollArea)` 的 `setWidgetResizable(True)`（`:100`）与 wrap 布局（`:181-184,288`）保持不变，由它承接完整高度；正文 Label 随宽度变化由布局自动重算折行。
  3. QSS `#detailTextEdit`（`style.qss:623-625`）保留背景透明语义，必要时补 `color` 与 `padding`，不设固定高度。
  4. 原始 JSON 的 `QPlainTextEdit#rawJsonEdit` 与 260px 上限（`:270-280`）本期不动。
- 验收标准（GWT）：
  - **Given** 一条 200 行长文本消息，**When** 打开详情，**Then** 正文 `QLabel` 完整渲染全文、无 `QPlainTextEdit` 后代、正文区域 `isVisible()` 的纵向滚动条为 False；详情面板自身可滚动到底部。
  - **Given** 正文含 `<b>` / `&` 等字符，**When** 渲染，**Then** 按纯文本原样显示，不被当 HTML 解析。
  - **Given** 用户选中正文，**When** 触发复制，**Then** 可复制原文（与 header 复制按钮共存）。

### FR-04 设置页去灰底与页面标题（D4=β）

- 业务规则：
  1. 删除 `header_label`（`settings_page.py:109-113`），`root` 首个子 widget 直接是 `QScrollArea`；保留全局 `#pageTitle` QSS 规则（其它页仍用）。
  2. 为 `SettingsPage` 设专属 `objectName="settingsPage"`，QSS 增加 `#settingsPage, #settingsPage QWidget#settingsScroll > QWidget { background:#ffffff; }`（具体选择器实施时按 Qt 实际层级校准），消除 `#contentArea` 灰底透出（`style.qss:23-30`、`ui_assembler.py:111`）。
  3. 底栏（`:150-164`）同页白底，不单独设灰。
- 验收标准（GWT）：
  - **Given** 打开设置页，**When** 遍历直接子控件，**Then** 不存在 `objectName=="pageTitle"` 的 label，也不存在文本为「设置」的 QLabel。
  - **Given** 设置页任意位置，**When** 取样背景，**Then** 为白色（QSS 断言 + 真机目视）；`#pageTitle {` 规则仍在 QSS 中（`test_dashboard_header_panels_2005.py:47-50`）。

### FR-05 分组标题移入卡片内左上方（D5=α）

- 业务规则：
  1. `form_section.py:153` 的 `gb` 设 `objectName="settingsSection"`；`settings_page.py:244-245` 的监听管理 `QGroupBox` 同样设该 objectName（或专用规则），统一视觉。
  2. QSS 新增 `QGroupBox#settingsSection`（`::title` 同步）：`margin-top:0`、`subcontrol-origin: padding`、`subcontrol-position: top left`、`top:10px; left:14px`、`padding:40px 16px 16px 16px`，照搬大盘 `QGroupBox#dashPanel` 已验证模式（`style.qss:426-454`）。
  3. **不改** `QGroupBox(title=...)` 构造、`gb.title()` 与 `retranslate()`（`form_section.py:210-231`），全局 `QGroupBox` 规则继续保持 `margin-top:14px` / `padding:20px 14px 14px 14px`（`test_dashboard_header_panels_2005.py:128-141`）。
- 验收标准（GWT）：
  - **Given** 设置页任一 FormSection，**When** 检查 QSS，**Then** `QGroupBox#settingsSection::title` 的 `subcontrol-origin` 为 `padding` 且坐标在卡片内左上；全局 `QGroupBox` 块不变。
  - **Given** 切换界面语言，**When** 分组标题重译，**Then** `gb.title()` 仍返回新文案（`test_form_section.py:292-298` 保持绿）。

### FR-06 监听频道管理左右布局 + 去底部提示（D5=α，诉求 4 前半）

- 业务规则：
  1. `channel_widget.py:397-435`：外层 `QVBoxLayout` 保留（搜索框独占顶部一行）；新增内层 `QHBoxLayout`，把 `joined_card` 与 `subs_card` 从 `root.addWidget(..., 3/2)` 改为 `row.addWidget(..., 1/1)`，再把 `row` 加入 `root`（stretch 1）。
  2. 删除 `ChannelListCard` 的 `bottom_hint` 参数与 label 创建（`:194,244-248`），以及两处实参（`:419,431`）；空态 `empty_hint_spec`（`:414-418`）保留。
  3. `settings_page.py:251` 的 `box.setMinimumHeight(460)` 复核：左右布局后改为较小值（建议 360）或移除，避免纵向空洞；`box_layout` 方向不变。
  4. 保留 `ChannelWidget` 类名与全部公开句柄（`joined_list` / `monitored_list` / `refresh_button` / 计数 label / `_empty_joined`，`:446-451`）、单实例 reparent 与门控接线（`ui_assembler.py:146,158-163,293-296`；`app_state_controller.py:48-55`）。
- 验收标准（GWT）：
  - **Given** 主窗口构建完成，**When** 检查两张卡的父布局，**Then** 二者同属一个 `QHBoxLayout` 且 `itemDoubleClicked` 连接数为 0（FR-07 配套）。
  - **Given** 任一卡片，**When** 遍历子控件，**Then** 无 `role=="hint"` 的底部 label；空态提示仍可在无数据时显示。
  - **Given** 三平台 1280×800，**When** 打开设置页，**Then** 两栏文本不换行为不可读、无水平滚动条。

### FR-07 行尾加减图标 + 去 ID + 去双击（D6=α/β/β，诉求 4 后半）

- 业务规则：
  1. `ChannelListCard` 新增 `item_action_clicked = Signal("qlonglong")`（**必须 64 位**，理由见 `:180-183` 的 32 位溢出事故）；删除 `item_double_clicked` 信号（`:183`）、`lst.itemDoubleClicked.connect`（`:232`）、`_on_lst_double_clicked`（`:344-347`）与两处卡级连接（`:422,434`）。
  2. 新增两个 16×16 SVG（`plus.svg` / `minus.svg`）到 `src/tgmonitor/resources/icons/`，用 `tinted_action_icon()`（`icon.py:68-87`）着色；每行 `setItemWidget` 一个 `QWidget`（水平布局：标题 Label 左 + `QToolButton` 右），`QToolButton.setObjectName("channelRowAction")`；按钮 `clicked` 用 `functools.partial` 或默认参数捕获 `int(cid)`（**禁止 late-binding lambda**，64 位 ID 如 `-1001375475051`）。
  3. `_channel_display_text`（`:138-154`）去掉 `· {ch.id}`，只保留 `title  @username` + verified/scam 徽标；**不加**含 chat_id 的 tooltip；`item.setData(Qt.UserRole, ch.id)` 保留供逻辑使用。
  4. 同步增量刷新：`_apply_title_changed`（`:529-533`）改为更新行内 Label；`_apply_photo_changed`（`:535-554`）仍设 item icon；`apply_filter`（`:313-331`）需验证 `item.setHidden()` 同步隐藏 `itemWidget`（不隐藏则显式同步）。
  5. 原双击槽 `_on_joined_double_click` / `_on_monitored_double_click`（`:587-607`）改由 `item_action_clicked` 驱动，core 调用 `monitor_channel` / `unmonitor_channel` 不变；已监听防重（`:592-593`）保留。
  6. 门控 `set_operations_enabled`（`:458-472`）保持 `lst.setEnabled(enabled)`（子按钮随之禁用），并显式遍历行按钮 `setEnabled(enabled)` 加固；core `require_ready` 不变。
- 验收标准（GWT）：
  - **Given** 左栏有频道 X（含 64 位负 id），**When** 点击 X 行 `+`，**Then** `app.channels.monitor_channel` 被调用一次、X 出现在右栏、事件 `ChannelMonitored` 发布；无双击触发。
  - **Given** 右栏有频道 Y，**When** 点击 Y 行 `−`，**Then** `unmonitor_channel` 被调用一次、Y 从左栏回到可加入态。
  - **Given** 任意行，**When** 读取行文本与 tooltip，**Then** 不含 `· ` + 数字 ID，也不含完整 chat_id；徽标（✓/[风险]）仍在。
  - **Given** 未登录态，**When** 触发状态刷新，**Then** 所有行按钮禁用，登录后恢复（`test_app_state_controller.py:302-344` 口径）。

### FR-08 API ID 输入框去上下箭头（D7=α）

- 业务规则：
  1. `form_row.py:128` 创建 `QSpinBox` 后设 `setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)`；该构造路径被所有 INT 字段共用，当前仓库仅 `api_id` 为 INT（`settings_page.py:175-181`），语义上等价于「只去 APPID 箭头」。
  2. `setRange(0, 2_000_000_000)`、校验（`settings_store.py:229-230`）、collect `int(...)`（`:568`）、load `setValue`（`:604`）均保持不变。
- 验收标准（GWT）：
  - **Given** 打开设置页，**When** 读取 `in_api_id.buttonSymbols()`，**Then** 为 `NoButtons`；输入框仍可键盘输入、范围钳制与校验仍生效。
  - **Given** 全套 pytest，**When** 执行，**Then** `test_form_row.py:148-177` / `test_form_section.py:48-59` 保持绿。

### FR-09 删除「仅保存到 .env」入口（D7=α）

- 业务规则：
  1. 删除 `settings_page.py:153-155` 的按钮创建/连接与底栏加入，以及 `_on_save_env`（`:640-691`）；底栏只留「保存并应用」右对齐按钮（`btn_apply` 与 `#primaryBtn` 不变）。
  2. 模块 docstring（`:5`）中「保存到 .env」措辞同步更新；core `validate_backends` 与 `_validate_and_apply`（`:693-735`）不动，`test_reconfigure.py:314-383` 与 `test_app_service_surface.py:30` 保持绿。
- 验收标准（GWT）：
  - **Given** 打开设置页，**When** 遍历按钮，**Then** 不存在文案为「仅保存到 .env」的按钮；源码 grep `btn_save_env|_on_save_env` 命中数为 0。
  - **Given** 修改任意配置，**When** 点「保存并应用」，**Then** 仍走校验 + `reconfigure` + 写盘 + 成功后提示热重载（链路不退化）。

### FR-10 设置页布局规整（D7=α）

- 业务规则（建议值，实施按真机微调）：
  1. 分组顺序保持：`账户凭证 → 监听频道管理 → 网络代理 → 消息存储 → 对象存储 → 策略 → 同步参数`（`settings_page.py:127-136`）。
  2. 统一尺度：卡片内边距 16px（`form_section.py:155` 当前 8px）、表单行距 12px（`:156` 当前未设）、`form_root` 分组间距 16px（`settings_page.py:124` 已是）、底栏 margins `24,12,24,16` 保持但只放一个按钮并 `addStretch` 靠左推右。
  3. 账户凭证卡内的登录/登出行（`:219-234`）保持卡片内部，不回到兄弟节点；`auth_row` 间距与表单行距对齐。
  4. 修正过时注释口径：`settings_page.py:12`（7 分组）、`:79/:90`（6 分组）、`:601`（8 section）统一为实际值。
- 验收标准（GWT）：
  - **Given** 设置页，**When** 读取各分组布局参数，**Then** 内边距/行距/分组间距为统一值；登录/登出按钮的父级仍是「账户凭证」QGroupBox（`test_ui_simplification_2003.py:149-171`）。
  - **Given** 任意滚动位置，**When** 查看底栏，**Then** 仅「保存并应用」一个按钮常驻可见。

### FR-11 测试与契约同步

- 业务规则（逐文件清单）：
  - 大盘：`test_dashboard_stats_compact_2005.py:20,40-42,74-89,93-106`、`test_dashboard_cards_refine_2005.py:18-46,72-92`、`test_ui_simplification_2003.py:344-369`、`test_refresh_joined_dashboard_2005.py:173-186`；新增状态色 QSS/property 断言（归入 `tests/test_dashboard_top_2007.py`）。
  - 详情：`test_message_detail_header_2005.py:3,28-30,33-42,54-76`、`test_live_message_detail_e2e.py:74-76,81-83`、`test_message_detail_fields.py:17-19,31,49,76-77,95-96`；新增「无内层滚动条 + 长文本全展示」断言。
  - 设置页：`test_form_section.py:292-298`、`test_form_row.py:148-177`、`test_settings_no_emoji_2006.py:44-51`、`test_dashboard_header_panels_2005.py:47-50,128-141`、`test_settings_page_storage.py`、`test_ui_polish_round2_2005.py:57-64`；新增白底/无标题/标题内移/无 env 按钮断言（`tests/test_settings_layout_2007.py`）。
  - 频道管理：`test_channel_widget.py:111-241,275-332`、`test_main_window_channels.py:629-782`（双击段 :768-782 必改）、`test_settings_channel_monitor_2006.py:76-120`、`test_app_state_controller.py:302-344`；新增左右布局/行按钮/无 ID/无双击断言（`tests/test_channel_row_actions_2007.py`）。
  - 守卫：`test_no_decorative_emoji.py` 必须保持绿（不回退彩色 emoji）；`test_app_service_surface.py:30` 保持绿。
- 验收标准（GWT）：
  - **Given** 全套 pytest，**When** 执行，**Then** 全绿；新增 3 个 2007 契约文件可独立失败定位（各对应 G1/G2、G3、G4/G5/G6）。
  - **Given** ruff + mypy，**When** 执行，**Then** 全绿（292 文件格式 / 143 文件类型检查基线不退化）。

---

## 八、非功能需求

| 类别 | 要求 | 口径 |
|---|---|---|
| 性能 | 设置页/大盘打开无新增阻塞；长文本详情滚动流畅 | 不新增同步 IO；沿用 `run_coro`（`async_bridge.py`）；QLabel 长文本按现有列表 delegate 同等体量评估 |
| 兼容 | 三平台（Windows `windows` QPA / macOS / Linux）；1280×800 最小窗口无裁切/无过窄；CLI 零影响 | CI 三平台 8/8；`style.qss` 单一真值源（`app.py:215-219`），`dist/` 过期副本不手工改，随打包重建 |
| 可访问性 | 状态不仅靠颜色（保留 `●/×/…/–` 字形 + 文本）；行按钮有 tooltip；键盘可选行 | 色觉障碍用户仍可读状态文本；`QToolButton.setToolTip("加入监听"/"移出监听")` |
| 可维护性 | 状态颜色一处定义；QSS 选择器有专属 objectName，不靠全局规则覆盖；死规则清理 | `STATE_COLOR` 唯一映射；`settingsSection` / `channelRowAction` / `settingsPage` 三个新 objectName 有 QSS 与测试守护 |

---

## 九、业务流程与状态流转

```
大盘：4 张单行卡（已加入 / 已监听 / 连接状态 / 存储后端）
  └─ 连接状态 ── state_color_bucket() ──> QSS 四档色（绿/红/琥珀/灰）

消息详情：点消息 ──> MessageDetail(QScrollArea)
  └─ 正文 QLabel(wordWrap) 自撑高 ──> 超高时外层面板滚动（唯一滚动条）

设置页：白底 + 无页标题
  ├─ 账户凭证 ── API ID（NoButtons） / 登录 / 登出
  ├─ 监听频道管理（左右两卡）
  │    ├─ 左「已加入频道」行 + ──> monitor_channel() ──> 右栏 + ChannelMonitored
  │    └─ 右「已监听」行 − ──> unmonitor_channel() ──> 左栏 + ChannelUnmonitored
  └─ 底栏「保存并应用」 ──> validate_backends + reconfigure + 写 .env
```

**状态机（沿用现状，不改动）**：未登录（按钮禁用）→ READY（可操作）→ 登出（禁用 + 白名单保留）。FR-02 只改呈现色，不改状态判定；FR-07 只改触发入口，不改 core 调用。

---

## 十、数据与埋点需求

本期**无新增数据字段、无埋点、无迁移**。涉及的既有链路：

- 大盘聚合：`aggregate_per_channel(monitored_ids)`（`repository.py:313-325`、`sqlite_repo.py:1085-1113`）继续供「频道统计」表使用；删顶卡不改取数。
- 监听白名单：`monitor_channel` / `unmonitor_channel`（`channel_service.py:80-107`）与 `set_channel_monitored` / `list_monitored_channels` 不变；物理列 `monitored` 不变。
- 设置写盘：`update_env_with_settings`（`settings_store.py:128-135`）只由「保存并应用」调用；core `validate_backends`（`_reconfigure.py:153-190`）保留但不新增 UI 入口。

---

## 十一、权限与安全要求

- 操作门禁沿用 core `require_ready`（`channel_service.py:88,104`）：UI 行按钮禁用只是体验，core 拒绝才是安全边界，本期不削弱。
- 频道 ID 从**展示层**移除，但 `Qt.UserRole` 中仍保留 64 位 id 供逻辑使用；不得因「不展示」而丢失 ID 绑定（避免同名频道误操作）。
- 正文 `QLabel` 使用 `Qt.TextPlain`，防止 Telegram 消息里的 `<script>` / `&` 被富文本渲染，属输出安全要求。
- 状态色不使用彩色 emoji，遵守装饰 emoji 静态守卫；颜色仅作辅助通道，文本与字形仍完整表达状态。

---

## 十二、异常处理清单

| 异常 | 处理 | 证据 / 责任 FR |
|---|---|---|
| 长文本导致正文 Label 过高 | 外层 `QScrollArea` 承接滚动，不设正文高度上限；验证 200/2000 字两档 | `message_detail.py:81,100` / FR-03 |
| 正文含 HTML 样字符 | `setTextFormat(Qt.TextPlain)` 原样显示 | `message_item_delegate.py:217-230` 转义先例 / FR-03 |
| 左右布局在窄窗口过挤 | 最小宽度 + 真机校准；必要时 6:5 而非 1:1 | `channel_widget.py:397-435` / FR-06 |
| `setItemWidget` 过滤后行控件未隐藏 | 验证并显式同步 `setHidden`； `takeItem` 时 itemWidget 随之销毁 | `channel_widget.py:295-303,313-331` / FR-07 |
| 行按钮捕获 64 位 ID 溢出 | `Signal("qlonglong")` + `partial`/默认参数捕获；禁止 32 位 `Signal(int)` | `channel_widget.py:180-183` / FR-07（历史事故） |
| 标题/头像事件更新不到行内 Label | `_apply_title_changed` 改内部 Label；`_apply_photo_changed` 保留 item icon | `channel_widget.py:520-554` / FR-07 |
| QSS 切片测试因新增规则位移而红 | 新增规则放在各块尾部；必要时拆独立 PR，不改测试字符串定位方式 | `test_dashboard_header_panels_2005.py:24-31`、`test_ui_polish_round2_2005.py:57-64` |
| 删「仅保存到 .env」后用户误以为不能保存 | 底栏保留醒目「保存并应用」；上线说明中写明此变更 | `settings_page.py:159-162` / FR-09 |
| 状态色色觉可达性 | 保留字形 + 文本，不只靠颜色； tooltip 补充 | `state_labels.py:33-62` / FR-02 |

---

## 十三、上线计划、依赖与上线后验证

### 13.1 依赖就绪度评估

| 依赖项 | 就绪状态 | 分级 | 最晚就绪时限 | 影响/替代 |
|---|---|---|---|---|
| 只读诊断证据（本报告附录 A） | 已完成（6 组，含 64 位信号事故与双层滚动探针） | 已具备 | — | 无 |
| plus/minus SVG 图标 | 待制作（现有 11 个图标无 plus/minus，`resources/icons/`） | 阻塞（FR-07） | PR-4 开工前 | 无替代；也可用 `QToolButton` 文本 `+`/`−` 降级，但视觉弱 |
| QSS 单一真值源 | 已具备（`app.py:215-219`）；`dist/` 副本过期 | 可绕过 | 打包前 | 不手改 `dist/`，随构建刷新 |
| 单实例 ChannelWidget 与公开句柄 | 已具备（2.0.6 落地） | 已具备 | — | FR-06/07 不改类名/句柄/门控签名 |
| 状态色值 | 已有色板注释（`style.qss:8-13`） | 已具备 | — | 色号可微调，档位不变 |
| core `validate_backends` 保留 | 已被测试锁定 | 已具备 | — | FR-09 只删 UI 入口 |

### 13.2 建议 PR 切分（每分支一主题，squash merge）

顺序原则：先视觉减法、再交互替换、最后清理与发版；每个 PR 自带对应测试同步，不做「先改 UI 后补测试」的跨 PR 红窗。

1. **PR-1 `style(dashboard)`**：FR-01 + FR-02 + 大盘测试同步（含状态色新契约 `test_dashboard_top_2007.py`）。
2. **PR-2 `fix(message-detail)`**：FR-03 + 详情测试同步（含长文本无内层滚动断言）。
3. **PR-3 `style(settings)`**：FR-04 + FR-05 + FR-10 + 设置页测试同步（`test_settings_layout_2007.py`）。
4. **PR-4 `feat(channel-actions)`**：FR-06 + FR-07 + SVG 图标 + QSS + 频道测试同步（`test_channel_row_actions_2007.py`）。
5. **PR-5 `refactor(settings-credentials)`**：FR-08 + FR-09 + 凭证/保存测试同步。
6. **PR-6 `chore(qss-cleanup)`**：FR-02 第 4 条的死规则清理（若 PR-1 的 QSS 切片测试允许则并入 PR-1，不单独开）。
7. **PR-7 `chore(release)`**：版本 bump 2.0.6→2.0.7 + CHANGELOG（6 处版本落点：`pyproject.toml`、`__init__.py`、`uv.lock`、`tgmonitor.spec` 两字段、`AGENTS.md`、`CHANGELOG.md`），`uv lock --check --offline`；**tag 与 Release 由用户确认后再触发，不手工 `gh release create`**。

每个 PR：本地全套 pytest + ruff + mypy 全绿 → 开 PR → 跟踪 CI 8/8 → squash merge → 同步 main 并删分支。基线 `1566 passed / 38 skipped / 63 deselected`。

### 13.3 上线后验证清单

- 三平台打开大盘：首行 4 张单行卡、无「消息总数」；登录/登出/报错时状态卡与状态栏呈绿/红/琥珀/灰，文字仍在。
- 打开一条长文本消息：正文完整显示，只有详情面板一条滚动条；含 `<`/`&` 的消息按原样显示；可选中复制。
- 打开设置页：白底、无「设置」标题、分组标题在卡片内左上、API ID 无箭头、底栏只有「保存并应用」；改配置后热重载成功。
- 打开监听频道管理：左右两栏，无底部提示、行内无 ID；`+`/`−` 图标分别加入/移出（用一个真实 64 位负 id 频道验证）；双击不再触发任何动作；未登录时按钮禁用、登录后恢复。
- 全套 pytest + 三平台 CI 绿；带 reaction 的消息 emoji 仍原样显示（守卫零误伤）。

---

## 十四、风险与遗留问题

### 14.1 风险登记册

| 编号 | 风险描述 | 类别 | 概率 | 影响 | 等级 | 应对措施 | 状态 |
|---|---|---|---|---|---|---|---|
| RK-01 | 状态色映射漏掉 12+5 态中的某态，运行时 KeyError 或颜色不更新 | 技术 | 3 | 3 | 9 中 | `STATE_COLOR` 全集覆盖测试 + 两处 property 写入点断言（大盘两处刷新路径） | 跟踪中 |
| RK-02 | 新增 QSS 规则位移导致字符串切片测试红 | 技术 | 4 | 2 | 8 中 | 新规则放块尾；必要时独立 PR；不改测试定位方式 | 跟踪中 |
| RK-03 | `setItemWidget` 后过滤隐藏失效、增量刷新打到 item 而非内部 Label | 技术 | 4 | 4 | 16 高 | 先写 offscreen 探针验证隐藏/重建，再改代码；FR-07 四条 GWT 独立断言 | 跟踪中 |
| RK-04 | 行按钮捕获 64 位 ID 时 late-binding / 32 位溢出重演 | 技术 | 3 | 5 | 15 高 | `Signal("qlonglong")` + `partial` 捕获；真实负 id 频道纳入手工验收与单测 | 跟踪中 |
| RK-05 | 去 ID + 行控件化牵动 `test_channel_widget.py` 密集文本断言，改动面失控 | 流程 | 3 | 3 | 9 中 | PR-4 单独成支，先列断言清单再改；文本格式一次定稿（保留徽标） | 跟踪中 |
| RK-06 | `QLabel` 正文把 Telegram 消息中的 `<`/`&` 当 HTML 渲染 | 安全 | 2 | 4 | 8 中 | `Qt.TextPlain` 显式设置 + 特殊字符 GWT 断言 | 跟踪中 |
| RK-07 | 左右布局在窄窗口把两卡压到不可读 | 体验 | 3 | 3 | 9 中 | 最小宽度 + 三平台 1280×800 目视；必要时 6:5 | 跟踪中 |
| RK-08 | 删「仅保存到 .env」后用户想「只写盘不生效」无入口 | 产品 | 2 | 2 | 4 低 | 用户已明确要求删除；上线说明提及改走「保存并应用」；core API 保留可后续恢复入口 | 接受 |
| RK-09 | 顺手清理 `statusDot` 死 QSS 误伤别处 | 技术 | 2 | 2 | 4 低 | 先 grep 确认零消费者（已确认）；与 FR-02 分 PR 可回退 | 观察 |
| RK-10 | 白底 QSS 选择器写法跨平台层级不一致 | 技术 | 2 | 3 | 6 低 | 用专属 objectName + 三平台目视；必要时改 `setAutoFillBackground` + QPalette | 观察 |

### 14.2 遗留问题

- **L1** 设置页分组顺序是否重排（D7=α 暂不重排，需用户给出目标顺序后独立立项）。
- **L2** 频道行键盘可达（当前只点按钮，`Return` 触发留后续）。
- **L3** `dist/` 下 QSS 副本过期，随打包重建即可，不手改。

---

## 十五、附录

### 附录 A — 诊断证据索引（506fb77 只读排查）

- **A-1 大盘顶部**：`dashboard_widget.py:74-89`（`_StatCard`，高 68 / 最小宽 160）、`:91-119`（标题行 + 数值行两行）、`:245-263`（5 卡顺序，`:260` 消息总数）、`:513-572`（`_fetch_channel_rows` → `_apply_stats_table`，`:557` 同源 `rows,total`、`:565` 顶卡、`:567-572` 底表）；QSS `style.qss:292-313`。测试 `test_dashboard_stats_compact_2005.py:20,40-42,74-89,93-106`、`test_dashboard_cards_refine_2005.py:18-46`、`test_ui_simplification_2003.py:344-369`。
- **A-2 状态色归因**：`state_labels.py:33-46`（单色字形，2.0.6 FR-09 提交 `3a9b266` 把 🟢/🔴/🟡 换为 `●/×/…`）、`:49-62,97-104`；`status_bar.py:53-67,170-205`（`:198-204` 只 setText，无颜色）；`style.qss:8-13,44-48,552-563`（死规则）；`CHANGELOG.md:4450-4451,4542-4543`（历史红/绿/橙与色板）；`test_no_decorative_emoji.py:29-35,53-65,68-92`（emoji 守卫）。
- **A-3 详情双层滚动**：`message_detail.py:81,100`（外层 QScrollArea）、`:237-244`（正文 `QPlainTextEdit` + `:241` 220px 上限）、`:270-280`（原始 JSON 260px）、`:75-78,250-252`（QLabel wordWrap 先例）、`:288`；QSS `style.qss:623-625,635-639`；`dashboard_widget.py:286-299`（QSplitter 2:1，非根因）。offscreen 探针实测：正文 vbar max=199 且外层 vbar max=113 → 双层滚动。测试 `test_message_detail_header_2005.py:28-30`、`test_live_message_detail_e2e.py:74-76,81-83`、`test_message_detail_fields.py:17-19`。
- **A-4 设置页结构**：`settings_page.py:104-164`（根布局 / `:109-113` 标题 / `:121-124` 滚动 / `:127-136` 7 分组 / `:150-164` 底栏双按钮）、`:170-234`（账户凭证 + api_id + auth_row）、`:236-252`（监听管理分组，`:251` minHeight 460）、`:568,604`（collect/load）、`:640-691`（`_on_save_env`）、`:693-735`（`_on_apply`）；`form_section.py:147-184,210-231,314-360,366-395`；`form_row.py:128-136`（QSpinBox 默认箭头）；QSS `style.qss:23-30,283-290,408-454,534-538,546-550`。core 侧 `settings_store.py:128-135,229-230`、`app_service/_reconfigure.py:57-147,153-190`。
- **A-5 频道管理**：`channel_widget.py:138-154`（行文本含 `· id`）、`:157-184`（Card 结构/信号，`:183` qlonglong）、`:186-255`（构造，`:232` 双击连接、`:244-248` 底部 hint）、`:259-331`（set/add/remove/filter）、`:344-347`（双击 emit）、`:397-435`（外层 QVBox + 3:2）、`:446-472`（句柄 + 门控）、`:520-607`（事件刷新 + 双击槽）；`settings_page.py:244-251`；`ui_assembler.py:146,158-163,293-296`；`app_state_controller.py:48-55`；`icon.py:68-87`；`resources/icons/` 仅 11 个 SVG、无 plus/minus。测试 `test_channel_widget.py:111-241,275-332`、`test_main_window_channels.py:629-782`、`test_settings_channel_monitor_2006.py:20-120`。
- **A-6 测试基线**：`1566 passed / 38 skipped / 63 deselected`；mypy 143 files、ruff/format 292 files；CI 8/8（pytest 三平台 + paint-probe + coverage + integration + mypy + ruff）。
- **A-7 版本落点**：`pyproject.toml:3`、`src/tgmonitor/__init__.py`、`uv.lock`、`tgmonitor.spec` 两字段、`AGENTS.md`、`CHANGELOG.md`；改完 `uv lock --check --offline`。

### 附录 B — 需求追踪矩阵（RTM）

| 需求编号 | 需求描述 | 来源 | PRD 章节 | 测试用例 | 状态 |
|---|---|---|---|---|---|
| R-01 | 卡片内容只保留一行 | 诉求 1 | FR-01 | `test_dashboard_cards_refine_2005`（改造）+ `test_dashboard_top_2007` | 待实施 |
| R-02 | 去除消息总数卡片 | 诉求 1 | FR-01 | `test_dashboard_stats_compact_2005`（改造） | 待实施 |
| R-03 | 登录状态红绿标识恢复 | 诉求 1 | FR-02 | `test_dashboard_top_2007` + emoji 守卫 | 待实施 |
| R-04 | 正文不出现纵向滚动条 | 诉求 2 | FR-03 | `test_message_detail_header_2005`（改造）+ 新断言 | 待实施 |
| R-05 | 设置页去灰底 + 去标题 | 诉求 3 | FR-04 | `test_settings_layout_2007` | 待实施 |
| R-06 | 分段标题在卡片内左上 | 诉求 3 | FR-05 | `test_form_section.py:292-298` + `test_dashboard_header_panels_2005:128-141` | 待实施 |
| R-07 | 频道管理左右布局 + 去底部提示 | 诉求 4 | FR-06 | `test_channel_row_actions_2007` | 待实施 |
| R-08 | 加号/减号图标 + 去 ID + 去双击 | 诉求 4 | FR-07 | `test_channel_widget.py` / `test_main_window_channels.py:768-782`（改造） | 待实施 |
| R-09 | APPID 去箭头 | 诉求 5 | FR-08 | `test_form_row.py:148-177` 附近新增 | 待实施 |
| R-10 | 去除「仅保存到 .env」 | 诉求 6 | FR-09 | 新增按钮缺席断言 + `test_app_service_surface.py` 保持 | 待实施 |
| R-11 | 设置页布局规整 | 诉求 7 | FR-10 | `test_settings_layout_2007` | 待实施 |
| R-12 | 无回归 | 质量基线 | FR-11、§8、§13 | 全套 pytest + ruff + mypy + CI 8/8 | 待实施 |

### 附录 C — 评审决策记录

| 编号 | 决策点 | 推荐 | 本文采用 | 用户决定（待填） |
|---|---|---|---|---|
| D1 | 单行卡片形态 | α 图标 + 标题 + 数值同排 | α | 待确认 |
| D2 | 状态色范围 | β 大盘 + 状态栏，QSS 唯一映射 | β | 待确认 |
| D3 | 正文去内层滚动控件 | β QLabel wordWrap | β | 待确认 |
| D4 | 设置页去灰底范围 | β 整页白底 + 去标题 | β | 待确认 |
| D5 | 标题内移 / 左右布局 | α QSS objectName / α VBox+HBox | α | 待确认 |
| D6 | 图标按钮 / ID / 双击 | α itemWidget / β 严格无 ID / β 删信号 | α+β+β | 待确认 |
| D7 | APPID / env 按钮 / 布局力度 | α / α / α | α | 待确认 |

### 附录 D — 合规声明

本方案为产品设计参考，具体实施请结合公司合规与法务要求确认。
