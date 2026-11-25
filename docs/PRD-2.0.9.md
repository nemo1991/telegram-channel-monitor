# PRD — tgmonitor 2.0.9「UI 测试瘦身 + 媒体页行操作重构 + 媒体按需下载」版本规划

## 一、文档信息

| 项 | 内容 |
|---|---|
| 版本 | v1.2（用户裁决：D8=β，补齐默认档缩略图下载；缩略图归入元数据、只有原文件才是「媒体本身」，D4 映射已按此修正。8 个决策点均已拍板，按组合展开） |
| 目标版本 | tgmonitor 2.0.9 |
| 基线版本 | 2.0.8（`main = 0af4f15`，lightweight tag `v2.0.8` 已推；版本落点均为 2.0.8） |
| 作者 | 司南（产品经理 Agent） |
| 需求方 | forcetone |
| 日期 | 2026-11-22 |
| 状态 | **待评审**：3 项诉求已归入 9 条 FR（FR-09 系 D8=β 新增）；决策点 **D1=α / D2=β / D3=β / D4=β / D5=β / D6=α / D7=α / D8=β 全部已拍板**，本文按该组合展开，可转「可实施」 |

**变更记录**

| 版本 | 日期 | 变更摘要 |
|---|---|---|
| v1.0 | 2026-11-22 | 初稿。对 3 项诉求逐条做只读代码诊断（UI 测试 / 媒体页 UI / 媒体获取与设置三组，证据均带文件:行号），归并为 8 条 FR + 7 个决策点，按推荐组合展开完整 15 段 PRD。 |
| v1.1 | 2026-11-22 | 用户裁决语义边界：**缩略图与元数据同属元数据，只有原文件才是「媒体本身」**。据此修正 D4 映射（thumbnail 由「开」改归「关/元数据侧」、默认档仍是元数据侧）、新增 D8「默认档是否真下缩略图」，并同步 §2/§3/§5/§7/§9/§12/§14/附录 全部相关表述。 |
| v1.2 | 2026-11-22 | 用户拍板 **D8=β：补齐默认档缩略图下载**。新增 FR-09；核实代码事实：`download_thumb`（`downloader.py:192-264`）+ `_maybe_download_thumb`（`service.py:476-510`）+ `thumbnails` 表（`sqlite_repo.py:139-156`）均现成，只需把空 hook `_maybe_store_thumb`（`:518-523`）接线；识别两个实施陷阱（FULL 档重复下载、thumb 落库晚于消息落库的外键时序）。全文按 D8=β 同步。 |

**评审记录**：暂无（第 1 轮待进行）。

> 诊断方式声明：本文「现状」结论全部来自对 `0af4f15` 工作树的只读排查，未改源码、未运行全套测试；「改动锚点」是实施落点建议，非已落地事实。核心判断依据是一个反直觉事实——**当前默认配置下媒体本来就不下载**（见 §2.1），它决定了诉求 3 的重构方向是「显式化 + 补手动入口」而非「改默认行为」。

---

## 二、背景与目标

### 2.1 背景

2.0.8 完成「登录收敛为二维码 + UI 交互精修」并发版后，用户基于实际使用提出 3 项诉求。只读诊断得到三组关键事实：

**① UI 测试存在可清理的冗余，但删错了会丢真守护**

- `tests/test_media_manager_widget.py:24-38` 的 `pytestmark` 把 `win32 / linux / darwin` 三个平台全部 skip，27 个用例在 CI 三平台与本地**从未执行**——是"看起来有、实际零覆盖"的死测试。
- `tests/test_main_window_activity_label.py:27-113` 的 10 个 `_ActivityLabel` 用例与 `tests/test_status_bar.py:103-170` 近乎逐一重复。
- `tests/test_main_window_statusbar.py:71-87` 的连接态映射与 `test_status_bar.py:202`、`test_ui_simplification_2003.py:92` 三处重复。
- 反面：`test_ui_2007_hotfix.py:82-93` 的 `test_two_card_lists_top_aligned` 用 `mapToGlobal().y()` 比较两栏顶部，本地 DPI 变化下稳定失败（`joined=440, subs=428`），干净 main 上可复现、与代码改动无关，CI 正常——这是唯一的"易碎但守护真实修复"两难项。
- 硬边界：`windows-paint-probe` job 唯一执行路径是 `tests/test_status_bar.py`（`ci.yml:113`），删它 probe 立即 `exit 4`。

**② 媒体管理页行操作按钮已挤爆横向预算**

- 每行是 `QListWidgetItem` + `setItemWidget` 自绘 `QWidget#mediaRow`（`media_manager_widget.py:200-205`、`_mm_render.py:310-319`），内部 `QHBoxLayout` 固定放：缩略图 40px + 频道 140px + 消息/类型 160px + 大小 80px + 状态 80px，再加 5 个**文字** QPushButton（Open/Reveal/Copy/Retry/Delete，`_mm_render.py:417-483`）。
- 固定占用约 750px + 9 段 spacing×8px + margins 16px ≈ **838px，已超 `QSize(800,48)` 的 sizeHint**（`_mm_render.py:296-300`）；唯一 `Expanding` 的文件名列（`:370-375`）被压缩，窗口变窄时首先截断。
- 5 个按钮全部常驻占位（非 DONE 行也只是 `setEnabled(False)`），无 objectName、无图标、无菜单，测试只能靠 `btn.text()` 定位（`test_media_manager_widget.py:300-309`）。
- 可复用范式已在仓库里：频道页 `channel_widget.py:373-392` 的 `QToolButton` + `setObjectName("channelRowAction")` + `tinted_action_icon()` + tooltip，配套 QSS 三态（`style.qss:592-603`）与 `findChild` 门控（`:335-344`）。

**③ 媒体逻辑：默认「只取元数据」已是现状，缺的是开关与手动下载入口**

- **语义边界（用户裁决 2026-11-22）**：**元数据 = 结构化字段 + 缩略图；媒体本身 = 原文件字节**。`METADATA` 与 `THUMBNAIL` 两态同属"元数据侧"（都不下载原文件），只有 `FULL` 是"媒体本身侧"。开关只控制"是否下载媒体本身（原文件）"，缩略图归元数据侧、不属于开关管辖范围。
- `MediaPolicy` 三态 `METADATA / THUMBNAIL / FULL`（`config.py:41-47`），默认 `THUMBNAIL`（`:108`）；但 `THUMBNAIL` 分支调的 `_maybe_store_thumb()` 是**空 hook**（`monitor/service.py:518-523`），真正的原文件下载只在 `FULL` 分支入队（`:298-319`）。**所以默认配置下：元数据落库、`object_key` 为空、`download_status=PENDING`，不下载原文件。缩略图虽按定义属元数据，但当前同样没落盘——D8=β 已拍板补齐这条链路（详见 FR-09）。**
- `MediaDTO`（`dto.py:110-151`）已完整区分元数据与字节：元数据字段 + `telegram_file_id`（实际是 `tl:{chat_id}:{msg_id}` 定位符）+ `object_key/object_backend` 字节引用 + `download_status` 四态（`:75-87`）。"仅元数据"记录形态天然存在，**无需新增 DB 列、无需迁移**。
- 缺口 A：设置页暴露的是三态 `media_policy` 下拉（`settings_page.py:512-550`），用户无法一眼理解哪档下原文件；没有"是否默认下载媒体本身"这一用户语言的开关。
- 缺口 B：`retry_media` 只接受 `FAILED`（`_media_os_actions.py:347-348`），行内 Retry 按钮也只在 FAILED 时 enable（`_mm_render.py:462`）。**大量 `PENDING`（仅元数据）媒体没有任何 UI 下载入口。**
- 陷阱：`diff_settings()`（`settings_store.py:363-390`）不比较 `media_policy` 等策略字段，`reconfigure()` 在 `changed=False` 时直接 return（`_reconfigure.py:83-85`），纯策略变更不会同步到 monitor。

### 2.2 要解决的问题

一句话：**清掉不产生覆盖的测试负担，把媒体页行操作从"文字按钮堆"收敛为可发现、省空间的操作区，并把"默认只取元数据（元数据 + 缩略图，不下原文件）"这一既有行为显式化为用户可控开关 + 给 PENDING 媒体补上手动下载/重试闭环。**

1. UI 测试：删死测试与重复覆盖，修易碎断言，保住活跃功能与 paint-probe 守护。
2. 媒体页：行操作按钮改为「高频图标常驻 + 低频操作进 ⋯ 菜单」，腾出文件名列空间；同步重建可真正执行的契约测试。
3. 媒体逻辑：设置页新增"同步时下载媒体本身"布尔开关（只管原文件，缩略图属元数据侧；默认关 = 现状），媒体页对 PENDING 媒体可下载、对 FAILED 可重试，不改存储与 schema。

### 2.3 目标与成功指标（口径明确）

| 目标 | 成功指标（可断言） | 口径与来源 |
|---|---|---|
| G1 测试瘦身 | `test_media_manager_widget.py` / `test_main_window_activity_label.py` 在源码中命中数为 0；`test_status_bar.py`、`test_qr_login_214.py`、`test_qt_signal_64bit_ids_2006.py`、`test_ui_208.py` 保持存在且绿；paint-probe job 目标文件未变 | grep + 全套 pytest + `ci.yml:113` |
| G2 易碎断言修复 | `test_two_card_lists_top_aligned` 不再出现 `mapToGlobal`；改用同一父容器内相对几何或布局索引断言；本地连续 3 次全量 pytest 不再因该用例失败 | 测试源码断言 + 本地复现 |
| G3 行操作布局 | 每行的常驻按钮数 ≤ 4 且均为 `QToolButton` + objectName；文件名列 `sizePolicy().horizontalPolicy() == Expanding` 且在 900px 窄窗口下仍可见；低频操作在 `QMenu` 中可触达 | offscreen 对象树断言 + 三平台目视 |
| G4 行操作信号 | 6 个既有 Signal（`open/retry/delete/reveal/copy/preview`）仍为 `"qlonglong", int, int`；新增 `download_requested` 同签名；`LIGHTBOX_PREVIEWABLE_TYPES` 反向导出仍在 | `test_qt_signal_64bit_ids_2006.py:85-109` 保持绿 + 新断言 |
| G5 下载开关 | 设置页有且仅有一个「同步时下载媒体本身」BOOL 字段（文案明确"仅指原文件，元数据与缩略图始终保留"），默认 False；`.env` 写出 `TG_MEDIA_POLICY=thumbnail`（关，元数据侧）/ `full`（开）；`EditableSettings` 往返一致 | `test_settings_store.py` / `test_settings_page_storage.py` 新增断言 |
| G6 手动下载 | PENDING 媒体行「下载」按钮 enable，点击后 `download_one(force=False)` 被调用一次；FAILED 媒体「重试」仍走 `force=True`；DONE 媒体两者均 disable | core service 单测 + VM 信号断言（"on_success 只 emit"） |
| G7 存量与兼容 | 打开开关不触发历史 PENDING 批量补下；`MediaDownloadStatus` 四态、schema、DTO 均无新增字段；`test_storage_migration_2006.py` 保持绿 | 策略矩阵测试 + grep |
| G8 无回归 | 全套 pytest 绿（刻意改造的断言除外）；ruff / mypy 绿；CI 8/8 | 本地全套 + CI |
| G9 默认档缩略图 | 默认档（关）收到带 thumb 的媒体后：`download_thumb` 被调一次、`thumbnails` 表有 DONE 行、`thumb/<sha>` 对象存在；FULL 档缩略图只下一次（不重复）；消息落库先于 thumb 落库（外键时序）；thumb 下载失败不影响消息落库 | `test_monitor_media.py` 新增断言 + 两后端 save_thumbnail 测试 |

**非目标（本期明确不做）**：不改存储层 / ObjectStore / DTO / schema / 迁移机制；不改二维码登录、64 位 Signal、消息管理页、状态栏；不引入按频道粒度的媒体策略；不做历史 PENDING 批量补下；不做版本 bump 与 Release（单独 PR，推 tag 触发）；不新增埋点。

---

## 三、名词与术语定义

| 术语 | 定义 | 代码映射 / 证据 |
|---|---|---|
| 元数据（用户定义） | **结构化字段 + 缩略图**，不含原文件字节；默认档获取的范围 | 用户裁决 2026-11-22 |
| 媒体本身（用户定义） | **仅指原文件字节**（`object_key` 指向的实体）；唯一受下载开关管辖的对象 | 用户裁决 2026-11-22 |
| 仅元数据 | `telegram_file_id` 已填、`object_key` 为空、`download_status=PENDING` 的媒体记录 | `dto.py:134-148`、`config.py:44` |
| MediaPolicy 三态 | `METADATA` / `THUMBNAIL` 两态同属**元数据侧**（都不下原文件，默认 `THUMBNAIL`；`THUMBNAIL` 的缩略图 hook 当前为空，见 D8）/ `FULL` 是**媒体本身侧**（下原文件） | `config.py:41-47`、`monitor/service.py:285-303` |
| PENDING / DOWNLOADING / DONE / FAILED | 媒体下载状态机四态 | `dto.py:75-87` |
| 行操作按钮 | 媒体行尾的 Open/Reveal/Copy/Retry/Delete，本期收敛为图标 + ⋯ 菜单 | `_mm_render.py:401-483` |
| channelRowAction | 频道页行尾图标按钮范式（本期复用的对象名与 QSS 三态） | `channel_widget.py:383-392`、`style.qss:592-603` |
| paint-probe | Windows CI 上用真 `windows` QPA 跑的冒烟 job，唯一目标 `test_status_bar.py` | `ci.yml:82-113` |
| 热重载 / reconfigure | 设置页「保存并应用」后的配置下发链路；当前不比较策略字段 | `settings_store.py:332-390`、`_reconfigure.py:83-85` |

---

## 四、需求范围

### 4.1 包含（本期做）

- FR-01：删除纯重复覆盖的 UI 测试文件（诉求 1 主体）。
- FR-02：修复 `test_two_card_lists_top_aligned` 的跨窗口全局坐标断言（诉求 1 易碎项）。
- FR-03：媒体页行操作按钮重构为「高频图标常驻 + 低频 ⋯ 菜单」（诉求 2）。
- FR-04：媒体页测试重写为 offscreen 可执行的信号/对象树契约（诉求 2 配套、与 FR-01 联动）。
- FR-05：设置页新增「同步时下载媒体本身」布尔开关并落到配置/.env（诉求 3 开关，只管原文件）。
- FR-06：媒体页新增 PENDING 手动下载、FAILED 重试闭环（诉求 3 操作）。
- FR-07：文档 / `.env.example` / README 配置表同步（诉求 3 配套）。
- FR-08：测试与契约同步（跨全部 FR）。
- FR-09：补齐默认档缩略图下载（D8=β；把空 hook 接到现成的 `_maybe_download_thumb`，不改 media DTO / schema）。

### 4.2 不包含（本期明确不做）

- 存储层 / ObjectStore / DTO / schema / 迁移的结构性改动（D8=β 只**使用**既有 `thumbnails` 表与 `download_thumb`，不新增列、不改 DTO）；按频道粒度策略；历史 PENDING 批量补下。
- 删除 `test_qr_login_214.py`、`test_qt_signal_64bit_ids_2006.py`、`test_ui_208.py`、`test_status_bar.py`、`test_status_bar_layout.py`、`test_qt_runtime_hygiene.py` 等活跃功能与 B 类 bug 契约守卫。
- 改 `MediaDownloadStatus` 枚举、改 14 个既有 Signal 签名、改 `LIGHTBOX_PREVIEWABLE_TYPES` 导出。
- 移动端 / Web 端；埋点；版本号 bump 与 tag（单独发版 PR）。

---

## 五、方案对比与选型说明

> 本文按 **推荐组合 D1=α / D2=β / D3=β / D4=β / D5=β / D6=α / D7=α** 展开后续章节；每个决策点独立，若用户改判某点，只替换该点对应 FR 章节与验收，其余不连带变更。

### 5.1 D1：UI 测试删除范围（诉求 1）

| 方案 | 做法 | 评价 |
|---|---|---|
| **α 保守：只删"零覆盖 + 纯重复"（推荐）** | 删 `test_media_manager_widget.py`（全平台 skip）+ `test_main_window_activity_label.py`（与 status_bar 重复）；`test_main_window_statusbar.py` 只去掉重复的映射断言、保留 `_on_conn_state` thin delegate 独有路径；易碎断言单独修（FR-02） | **推荐**：净减约 37 个用例的维护负担，不动任何活跃功能守护；`test_media_manager_widget.py` 不是"白删"——由 FR-04 重写取代 |
| β 激进：连 `test_main_window_close.py` 3 个 paint-skip 用例一起删 | 再少 3 个死用例 | 改动小但收益边际；closeEvent 异常不阻断退出等契约失去唯一守护（后台 loop 路径），且该修复历史上出过"skip 掩盖真 bug"事故（`CHANGELOG:465-475`），不值得省这 3 例 |

**明确不能删**：`test_status_bar.py`（paint-probe 唯一目标）、二维码登录三处守卫、64 位 Signal、消息页 UI、B 类 bug 契约。这些守护的是已修复并上线过的真 bug，删除净收益仅是用例数与运行时间，风险远大于收益。

### 5.2 D2：媒体页行操作按钮形态（诉求 2）

| 方案 | 做法 | 评价 |
|---|---|---|
| α 纯图标常驻 | 5 个操作全改 `QToolButton` 图标（复用 `tinted_action_icon`），仍平铺 5 个 | 比现状省约 130px，改动中等；但诉求 3 还要新增「下载」，会变成 6 个按钮继续挤占，问题复发 |
| **β 高频图标常驻 + 低频 ⋯ 菜单（推荐）** | 常驻 3 个图标：下载（新）/ 打开 / 重试；Reveal / Copy / Delete 收进第 4 个 `QToolButton#mediaRowMore` 弹出的 `QMenu`；全部带 objectName + tooltip | **推荐**：每行常驻按钮从 5 个文字压到 4 个图标（合计 ~100px，省 150px+），文件名列恢复可读；给新增下载留了位置；高频操作仍一眼可点、低频不丢可发现性 |

菜单项的 enabled 规则与现状一致：下载仅 PENDING、重试仅 FAILED、打开/Reveal/Copy 仅 DONE、Delete 总可点——菜单弹出时按当前行状态设置 `QAction.setEnabled()`。菜单项与常驻图标**共用同一组 Signal**，不改变 VM/controller 接线。

### 5.3 D3：媒体页测试处置（诉求 2/3 联动）

| 方案 | 做法 | 评价 |
|---|---|---|
| α 直接删 skip 文件 | 配合 D1 删除，重构后无 widget 层回归保护 | 与"重构媒体页"直接冲突：本次恰是最需要守护布局契约的时候 |
| **β 重写为 offscreen 安全契约（推荐）** | 删除 `test_media_manager_widget.py` 原 27 例，新建 offscreen 可执行的契约测试：对象树（按钮类型/objectName/常驻数量）、Signal 参数与 64 位 id、菜单项 enabled 规则、`_RowKey` 定位协议；断言布局结构而非真实几何（参考 `test_ui_208.py:274-305`） | **推荐**：既满足 D1"删死测试"，又给重构装上真正的回归网；几何断言用相对关系，避开 DPI/Daylight 坑 |

历史教训引用：`test_main_window_delete_feedback.py:36-45` 记录了 2026-10-22 把该文件 `xfail/skipif` 全部拆掉、因为"真 bug 被标记盖住"——本期对全平台 skip 的处理沿用同一精神。

### 5.4 D4：下载开关形态（诉求 3）

| 方案 | 做法 | 评价 |
|---|---|---|
| α 独立布尔 `download_media_on_sync` 叠加三态策略 | 新字段默认 False，`monitor/service.py` 入队条件变成 `开关 or policy==FULL` | 两个旋钮语义重叠（策略已有 FULL/元数据侧），用户更难理解；`diff_settings` 同理不含新字段 |
| **β 布尔开关映射到「元数据侧 / 媒体本身侧」两态（推荐）** | 设置页把三态 `media_policy` 下拉换成单一 BOOL「同步时下载媒体本身」（默认关）；底层：关 → 元数据侧（`METADATA` / `THUMBNAIL` 均归此侧，默认 `THUMBNAIL`），开 → `MediaPolicy.FULL`；开关**只控制原文件**，缩略图归元数据侧、不受其管辖 | **推荐**：按用户语义边界划分（缩略图属元数据），一个用户能懂的开关；默认关 = 现状（不下原文件）；复用既有 `media_policy` 字段与 .env key，**无需新 DB/配置字段**；CLI `--media-policy` 三态仍可用 |

配置映射表（严格按"是否下载媒体本身=原文件"划分）：

| 用户开关 | 语义侧 | `.env` 写出 | 说明 |
|---|---|---|---|
| 关（默认） | 元数据侧 | `TG_MEDIA_POLICY=thumbnail` | 保留元数据 + 缩略图，不下原文件；`thumbnail` 是默认档，属"关"侧（**不是"开"**） |
| 开 | 媒体本身侧 | `TG_MEDIA_POLICY=full` | 元数据 + 原文件（`FULL` 分支现已含缩略图下载 worker） |
| （CLI 兼容） | 元数据侧子集 | `TG_MEDIA_POLICY=metadata` | 旧配置/高级用户显式"连缩略图逻辑都不走"；UI 不暴露，读到此值时开关显示为关 |

`EditableSettings.media_download_on_sync: bool` 仅作 UI 层镜像，`from_settings()` 由 `media_policy == MediaPolicy.FULL` 推导（**只有 FULL 才显示"开"**），`to_settings()` 反向：False → `THUMBNAIL`、True → `FULL`。旧 `.env` 的 `metadata` / `thumbnail` 都归"关"侧，UI 一致显示为未勾选——这纠正了 v1.0 把 `thumbnail` 误归"开"的映射。

### 5.5 D5：手动下载与重试入口（诉求 3）

| 方案 | 做法 | 评价 |
|---|---|---|
| α 复用 Retry 放宽状态守卫 | Retry 按钮对 PENDING/FAILED 都 enable，`retry_media` 去掉 FAILED 守卫 | 改动最小，但把"首次下载"和"失败重下"混成一个语义（`force=True` 会重下已存字节），用户无法区分 |
| **β 新增独立 Download 信号 + 按钮（推荐）** | 新增 `download_requested = Signal("qlonglong", int, int)`；UI 常驻「下载」图标（PENDING enable）；VM `download_media()` → core `download_media()` 走 `download_one(force=False)`；Retry 保持 `force=True` 仅 FAILED | **推荐**：语义清晰（下载=首次取字节省流量，重试=重下损坏），与 D2 的图标布局天然契合；core 侧复用 `download_one`，只新增一层薄 delegate |

core 落点：在 `MediaService` 新增 `download_media(channel_id, telegram_msg_id, media_idx)`，复用 `_media_os_actions.retry_media` 的主体但去掉 FAILED 守卫与"清旧 bytes"步骤，`force=False`（`downloader.download_one` 已有 skip-if-stored 快路径，`downloader.py:124-162`）。DONE 媒体点下载时由 downloader 的 skip 路径直接返回 DONE，不产生重复下载。

### 5.6 D6：开关生效时机（诉求 3）

| 方案 | 做法 | 评价 |
|---|---|---|
| **α 重启生效 + UI 明示（推荐）** | 不扩展 `diff_settings`；"保存并应用"写盘成功即提示「媒体下载设置将在重启后生效」 | **推荐**：monitor 的 settings 与 downloader 队列在启动期构造（`orchestrator.py:114-121`、`whitelist.py:76-86`），运行中换策略需要重建队列，风险高；现状 `media_policy` 本就是重启生效，保持诚实一致 |
| β 扩展 `SettingsDiff` 立即生效 | 把策略字段纳入 diff，reconfigure 时热切 monitor | 需重建下载队列与 worker，牵动并发路径，超出本期收益；列后续 |

### 5.7 D7：存量数据处理（诉求 3）

| 方案 | 做法 | 评价 |
|---|---|---|
| **α 不补下存量（推荐）** | 开关打开后仅影响新消息；历史 PENDING 保持元数据，用户按需要在媒体页逐条/批量点下载 | **推荐**：避免一次性触发大量下载（流量、对象存储、Telegram 风控），与"默认轻量"初衷一致；批量下载已有入口可按需使用 |
| β 打开开关时批量补下历史 PENDING | 遍历 PENDING 全部入队 | 突发负载与风控风险，且用户诉求未要求历史补齐 |

### 5.8 D8：补齐默认档缩略图下载（用户拍板 β）

用户裁决"缩略图属元数据"，且拍板 **D8=β**：默认档（元数据侧）应真正拿到缩略图。**核实后发现这是"接线"而非"造轮子"——整条下载/落库链路已存在，只差把空 hook 接上**。代码事实：

| 现成组件 | 位置 | 状态 |
|---|---|---|
| 缩略图定位符填充 | `telethon_messages.py:153`（照片总有 thumb）、`:190-192`（文档仅 `has_thumb` 时填） | ✅ 元数据侧就有 `thumbnail_telegram_file_id` |
| `download_thumb()` | `downloader.py:192-264` | ✅ 下载→`thumb/` ObjectStore→返 `ThumbnailDTO`，失败返 FAILED 不抛，无 thumb id 返 None |
| `_maybe_download_thumb(msg, idx, med)` | `service.py:476-510` | ✅ 调 `download_thumb`→补 `media_idx`→`storage.save_thumbnail`，失败仅 log 不崩主流程 |
| `thumbnails` 表 + `save_thumbnail()` | `sqlite_repo.py:139-156`、`_thumbnails_pg.py:58`、`repository.py:419` | ✅ 两后端都有，schema 无需新增列 |
| 空 hook `_maybe_store_thumb(med)` | `service.py:518-523` | ❌ `return None`，当前调用它等于什么都没做——**唯一要改的点** |

**实施方案（FR-09）**：把 `_handle` 里 `!= METADATA` 分支对 `_maybe_store_thumb(med)` 的调用（`service.py:289-291`）改为调用现成的 `_maybe_download_thumb(msg, idx, med)`；后者目前只在 `FULL` 的 `_download_worker` 里被调用（`:466`）。改后默认档与 FULL 档都走同一条缩略图链路。

**两个必须处理的实现陷阱**：

1. **FULL 档不能重复下载**：若不加区分地在 `:289-291` 调 `_maybe_download_thumb`，FULL 档会在 `_handle` 下一次、又在 `_download_worker:466` 再下一次。需在调用点按策略去重——推荐在 `_maybe_store_thumb` 内部判断 `media_policy != FULL` 才执行，或把 `:289-291` 的条件收紧为 `== THUMBNAIL`（仅默认档走同步路径），FULL 档继续只由 worker 下。
2. **缩略图落库晚于消息落库的外键时序**：`thumbnails` 表对 `(channel_id, telegram_msg_id)` 有 `FOREIGN KEY … REFERENCES messages ON DELETE CASCADE`（`sqlite_repo.py:154-155`）。而 `_handle` 在 `:324` 才 `save_message`。所以缩略图调用必须安排在 `save_message` **之后**，否则 `save_thumbnail` 触发外键约束失败（PG 同样有该约束）。`_download_worker` 的 `:466` 正是在 `:455 save_message` 之后调用，顺序天然正确——默认档同步路径须照此对齐。

**取舍说明**：补齐后默认档会为每个带 thumb 的媒体产生一次缩略图下载（通常 <10KB，`max_bytes` 200MB 上限几乎不触发）+ 一行 thumbnails 记录 + 一个 thumb/ 对象；失败仅 log 不影响消息落库，故"轻量"不会被失败拖垮。D4 开关映射（只管原文件）不受本决策影响：关=有元数据+缩略图、无原文件；开=再补原文件。

### 5.9 演进路径

- 本期（2.0.9）：测试瘦身 + 媒体页布局收敛 + 下载开关与手动入口 + **默认档缩略图补齐（D8=β / FR-09）**；不碰 media DTO / schema（缩略图走既有 thumbnails 表）。
- 后续（触发条件明确再立）：① 策略字段热重载（D6=β）；② 按频道粒度媒体策略；③ PENDING 批量下载一键入口与限速；④ 顺手清理 `test_main_window_close.py` 死用例与 `style.qss` 零消费者规则。
- 后续（触发条件明确再立）：① 策略字段热重载（需重建下载队列，D6=β）；② 按频道粒度媒体策略（需新增 schema 列 + repair 常量 + UI）；③ PENDING 批量下载的一键入口与限速（D7=β 的受控版本）；④ 顺手清理 `test_main_window_close.py` 死用例与 `style.qss` 中零消费者规则（若 D1=β）。

---

## 六、用户角色与使用场景

**用户画像**：桌面端 tgmonitor 使用者，关注流量/存储成本，日常主流程是「登录 → 大盘扫状态 → 消息页看详情 → 媒体页找文件 → 偶尔调整配置」。偏好信息密度可控、操作入口显式、不为不需要的功能付出下载成本。

| 场景 | 用户目标 | 操作路径（改造后） | 触点 |
|---|---|---|---|
| S1 默认轻量运行 | 记录媒体元数据 + 缩略图，不存原文件 | 什么都不用做：默认「同步时下载媒体本身」关，媒体以 PENDING 落库、缩略图落 thumbnails 表，媒体页/消息页可看缩略图 | FR-05 / FR-09 |
| S2 打开全量下载 | 新消息带媒体时自动存原文件 | 设置 → 策略 → 勾选「同步时下载媒体本身」→ 保存并应用 → 重启 | FR-05 / D6 |
| S3 按需下载单个 | 看到某条媒体想要原文件 | 媒体管理 → 找到 PENDING 行 → 点「下载」图标 → 状态转 DONE → 点「打开」 | FR-06 / FR-03 |
| S4 失败重试 | 下载失败后重下 | 媒体管理 → FAILED 行点「重试」图标（或 ⋯ 菜单项） | FR-06 |
| S5 批量处理 | 清理/导出/批量重试 | 选中多行 → 底部工具栏（全选/批量重试/删除/清空频道/导出）不变 | FR-03 |
| S6 窄窗口看文件名 | 窗口缩小仍能读文件名 | 行操作收敛后文件名列保留 Expanding，不再最先被压没 | FR-03 |

---

## 七、功能需求（编号 + 业务规则 + GWT 验收）

### FR-01 删除纯重复覆盖的 UI 测试（D1=α）

- 业务规则：
  1. 删除 `tests/test_media_manager_widget.py` 原文件（27 例全平台 skip，零覆盖）；其内容由 FR-04 的新契约测试取代，**不直接净删守护**。
  2. 删除 `tests/test_main_window_activity_label.py`（10 例与 `test_status_bar.py:103-170` 重复）；其 `:49 persistent_without_timeout`、`:103 expires_after_interval` 两个独有断言先合并进 `test_status_bar.py` 再删。
  3. `tests/test_main_window_statusbar.py` 删除 `test_conn_state_label_mapping`（与 `test_status_bar.py:202`、`test_ui_simplification_2003.py:92` 重复），保留 `MainWindow._on_conn_state` thin delegate 断言（`:82-86`）。
  4. 不动 `test_main_window_close.py`（D1=α）。
- 验收标准（GWT）：
  - **Given** 全套 pytest，**When** 收集用例，**Then** 上述三个文件/函数不再被收集，基线从 `1619 passed / 38 skipped / 63 deselected` 按预期下降（skip 数减少约 30）。
  - **Given** `ci.yml:113`，**When** 检查 paint-probe 目标，**Then** 仍为 `tests/test_status_bar.py`，job 不 `exit 4`。
  - **Given** `_ActivityLabel` 两个独有断言，**When** 迁移后执行，**Then** 仍覆盖"无 timeout 持久显示"与"按 interval 过期"。

### FR-02 修复易碎的跨窗口坐标断言（D1=α 配套）

- 业务规则：
  1. `tests/test_ui_2007_hotfix.py:82-93` 的 `test_two_card_lists_top_aligned` 移除 `mapToGlobal(...).y()` 比较；改为断言两列表在**同一父布局**中处于同一 `QHBoxLayout` 的相邻索引（参考 `test_ui_208.py:274-305` 的"未 show 时几何恒 0，故断言布局索引而非 y 坐标"范式），或比较两列表相对同一父 widget 的 `y()`。
  2. 保持该测试守护的语义——"左右两栏列表顶部对齐"这一 v2.0.7 真实修复——不得退化为恒真断言。
- 验收标准（GWT）：
  - **Given** 本地当前显示/DPI，**When** 连续 3 次跑全套 pytest，**Then** 该用例不再因 `joined/subs` 坐标差失败。
  - **Given** 人为把两栏放进不同布局嵌套，**When** 执行该用例，**Then** 仍能失败（证明守护有效，非恒真）。

### FR-03 媒体页行操作按钮重构（D2=β）

- 业务规则：
  1. 重写 `_mm_render.py:401-483` 的 `_build_action_buttons_in_row`：常驻 3 个 `QToolButton`（下载/打开/重试）+ 1 个 `QToolButton#mediaRowMore`（⋯ 菜单入口），全部 `setObjectName`、`setToolTip`、`setCursor(PointingHandCursor)`、图标走 `tinted_action_icon()`（`icon.py:68`，复用 `resources/icons/` 现有图标或新增 4 个 SVG）。
  2. ⋯ 菜单用 `QMenu`：`Reveal / Copy / Delete` 三项，按当前行 `download_status` 设置 `QAction.setEnabled()`；菜单项与常驻图标触发**同一组 Signal**。
  3. 保留 6 个既有 Signal 名与 `"qlonglong", int, int` 签名（`media_manager_widget.py:115-132`、`test_qt_signal_64bit_ids_2006.py:85-109`），新增 `download_requested` 同签名；`LIGHTBOX_PREVIEWABLE_TYPES` 反向导出（`media_manager_widget.py:57-72`）不得移除。
  4. 文件名列保持 `setSizePolicy(Expanding, Preferred)`（`_mm_render.py:370-375`），行 sizeHint 宽度可适当下调（建议 760），让窄窗口优先保文件名；缩略图点击预览（`_mm_render.py:275-294`）路径不动。
  5. 行 hover 样式（`_mm_render.py:485-490`）扩展到 ⋯ 按钮的 hover 高亮，复用 `style.qss:592-603` 的 `#channelRowAction` 三态写法并新增 `#mediaRowAction` / `#mediaRowMore` 规则。
  6. `_reset_list_for_render`（`:220-227`）在 clear 时对挂件调用 `deleteLater()`，对齐频道页 `_clear_rows`（`channel_widget.py:348-355`），消除 viewport 残留。
- 验收标准（GWT）：
  - **Given** 一行媒体，**When** 遍历行尾按钮，**Then** 常驻 `QToolButton` 数 ≤ 4 且每个都有非空 objectName；原 5 个文字 QPushButton 命中数为 0。
  - **Given** 900px 窄窗口，**When** 渲染长文件名媒体行，**Then** 文件名 label 可见（未被按钮挤出），行尾按钮不换行。
  - **Given** DONE / PENDING / FAILED 三种状态，**When** 点开 ⋯ 菜单，**Then** 三个菜单项的 enabled 与现状按钮规则一致；点任一菜单项 emit 对应的 `(channel_id, telegram_msg_id, media_idx)`。
  - **Given** 64 位负 `channel_id`，**When** 点击下载/打开/重试，**Then** Signal 正常 emit，无 OverflowError、无 SystemError。

### FR-04 媒体页测试重写（D3=β）

- 业务规则：
  1. 新建 `tests/test_media_actions_209.py`，覆盖：行尾常驻按钮类型/objectName/数量、⋯ 菜单三态 enabled、6+1 个 Signal 参数与 64 位 id、`_RowKey`/`_status_labels` 定位协议、`LIGHTBOX_PREVIEWABLE_TYPES` 可导入。
  2. 全部用 offscreen 安全的对象树/信号断言，**不使用 `mapToGlobal` 或真实 paint**；参考 `test_ui_208.py:274-305` 与 `tests/fixtures/_qt_runtime.py` 的共享 loop + `deleteLater()` 约定（widget per-test `deleteLater`、不调 `close`）。
  3. 不设平台 skip 标记（除非某用例确实需要真 paint，则单独标记而非整文件 skip）。
- 验收标准（GWT）：
  - **Given** 三平台 CI，**When** 执行 `test_media_actions_209.py`，**Then** 用例真实执行（skipped 数为 0），全部通过。
  - **Given** 故意把 `download_requested` 改成 32 位 `Signal(int)`，**When** 执行新测试，**Then** 有用例失败（证明守护有效）。

### FR-05 设置页新增「同步时下载媒体」开关（D4=β）

- 业务规则：
  1. `settings_page.py:512-550` 的 `_build_policy` 把 `media_policy` 三态下拉替换为 `FieldDef(kind=FieldKind.BOOL, key="media_download_on_sync", label="同步时下载媒体本身", default=False, tooltip="仅控制是否下载原文件；元数据与缩略图始终保留。关闭时不下原文件，开启后新消息中的媒体原文件将自动下载。修改后重启生效。")`（`form_section.py:351-359` 的 BOOL 构造）。文案须明确"媒体本身=原文件"，避免用户误以为关掉会丢缩略图。
  2. `_collect()`（`:642-679`）与 `_load_from_settings()`（`:681-720`）用 `EditableSettings.media_download_on_sync` 与 `media_policy` 双向映射（**开 ⟺ `media_policy == FULL`**；关 → `THUMBNAIL`，元数据侧；旧 `metadata` / `thumbnail` 均视为关）。严禁沿用 v1.0"非 METADATA 即开"的推导——那会把 `thumbnail` 误显示为开。
  3. `settings_store.py`：`EditableSettings` 加字段（`:171-208`），`from_settings()`（`:210-236`）与 `to_settings()`（`:265-292`）加映射；`settings_to_pairs()`（`:95-124`）仍写 `TG_MEDIA_POLICY`，不新增 .env key。
  4. `monitor/service.py:285-303`、`channel_sync/service.py:345-370` 消费 `media_policy` 的逻辑**不变**（开关已映射为两态）；不改 `diff_settings`（D6=α）。
  5. `.env.example:26-29` 与 `README.md:69` 配置表更新 `TG_MEDIA_POLICY` 说明：`thumbnail`（默认，元数据侧=元数据+缩略图、不下原文件）/ `full`（媒体本身侧=额外下载原文件）/ `metadata`（元数据侧子集，CLI 兼容保留）。
- 验收标准（GWT）：
  - **Given** 新安装（无 .env），**When** 打开设置页，**Then** 「同步时下载媒体本身」未勾选，`.env` 写出 `TG_MEDIA_POLICY=thumbnail`。
  - **Given** 勾选开关并保存，**When** 读 .env，**Then** 为 `TG_MEDIA_POLICY=full`；重启后新消息原文件自动下载。
  - **Given** 旧 .env 中 `TG_MEDIA_POLICY=thumbnail` 或 `metadata`，**When** 打开设置页，**Then** 开关均显示为**未勾选**（都属元数据侧）；点保存后 `thumbnail` 保持、`metadata` 可规范化为 `thumbnail`（均不变成 full）。
  - **Given** `EditableSettings` 往返，**When** `from_settings → to_settings`，**Then** 字段无漂移（`test_settings_store.py` 既有口径）。

### FR-06 媒体页新增 PENDING 下载与 FAILED 重试闭环（D5=β）

- 业务规则：
  1. UI：常驻「下载」图标在 `download_status == PENDING` 时 enable；「重试」在 FAILED 时 enable；「打开」在 DONE 时 enable（现状规则不变）。
  2. Signal：`download_requested = Signal("qlonglong", int, int)`（`media_manager_widget.py` 定义区 115-151），`main_window.py:254-266` 接线到 `self._vm.download_media(...)` 的 lambda。
  3. VM：`viewmodels/_media.py` 新增 `download_media()`，走 `run_coro(app.media.download_media(...), on_success=只 emit, on_error=...)`；**on_success 只 emit Qt signal，主线程槽里碰 widget**（`async_bridge.py:41-87`、`message_manager_widget.py:222-224` 的跨线程协议）。
  4. Core：`MediaService.download_media(channel_id, telegram_msg_id, media_idx)` 薄 delegate 到 `_media_os_actions` 新增的同名函数；主体复用 `retry_media`（`:332-406`）但**去掉 FAILED 守卫与清旧 bytes 步骤**，`download_one(force=False)`；成功/失败仍发 `MediaDownloaded` 与进度事件。
  5. `retry_media` 保持 `force=True` 与 FAILED 守卫不动；批量路径（`_mm_actions.py:79-82`、`media_actions_controller.py:83-99`）不动。
- 验收标准（GWT）：
  - **Given** PENDING 媒体，**When** 点「下载」，**Then** `download_one(force=False)` 被调用一次；成功后状态转 DONE、`object_key` 非空、列表刷新。
  - **Given** FAILED 媒体，**When** 点「重试」，**Then** 仍走 `force=True`，清旧 bytes 逻辑保留。
  - **Given** DONE 媒体，**When** 查看行按钮，**Then** 下载/重试均 disable，打开/Reveal/Copy enable。
  - **Given** 下载过程中，**When** 收到 `MediaDownloadProgress`，**Then** 状态 label 按 `_RowKey` 更新文字（`_mm_render.py:494-518`，进度协议不变）。

### FR-07 文档与配置示例同步（诉求 3 配套）

- 业务规则：
  1. `.env.example` 的 `TG_MEDIA_POLICY` 行改为 `thumbnail`（默认）并加注释说明 `thumbnail`=元数据侧（不下原文件）、`full`=额外下载原文件、`metadata`=兼容保留。
  2. `README.md` 配置表更新策略描述；CHANGELOG 在发版 PR 统一补 2.0.9 条目（本期不改 CHANGELOG 版本号）。
- 验收标准（GWT）：
  - **Given** 用户照 `.env.example` 配置，**When** 启动应用，**Then** 默认仅记录元数据，无媒体下载产生。

### FR-08 测试与契约同步

- 业务规则（逐文件清单）：
  - 测试删除：FR-01 三个文件/函数；新增 `tests/test_media_actions_209.py`（FR-04）。
  - 断言改造：`test_ui_2007_hotfix.py:82-93`（FR-02）、`test_status_bar.py`（承接 `_ActivityLabel` 两个独有断言）。
  - 配置测试：`tests/test_settings_store.py`、`tests/test_settings_page_storage.py`、`tests/test_config_tolerant.py`（BOOL 往返、旧 thumbnail 兼容、默认值）。
  - 策略矩阵：`tests/test_monitor_media.py`、`tests/test_channel_sync.py`、`tests/test_reconfigure.py` 补「默认不下原文件但下缩略图 / 开关开 FULL 下原文件 / PENDING 手动下载 / FAILED 重试」四档。
  - core 测试：`tests/test_media_manager.py`、`tests/test_media_downloader.py` 补 `download_media` 的 force=False 断言与 DONE skip 路径；补默认档缩略图下载与落库断言（FR-09）。
  - 静态守卫：`test_qt_signal_64bit_ids_2006.py`、`test_no_decorative_emoji.py`、`test_app_service_surface.py`、`test_storage_migration_2006.py` 必须保持绿。
- 验收标准（GWT）：
  - **Given** 全套 pytest，**When** 执行，**Then** 全绿（刻意改造的断言除外）；新契约测试可独立失败定位。
  - **Given** ruff + mypy，**When** 执行，**Then** 全绿（mypy 144 files 基线不退化）。

### FR-09 补齐默认档缩略图下载（D8=β）

- 业务规则：
  1. 复用现成链路，**不造轮子**：`MediaDownloader.download_thumb`（`downloader.py:192-264`）、`MonitorService._maybe_download_thumb`（`service.py:476-510`）、`thumbnails` 表 + `save_thumbnail`（`sqlite_repo.py:139-156`、`_thumbnails_pg.py:58`、`repository.py:419`）均已在位，schema / media DTO 不改。
  2. 把 `_handle` 里 `service.py:289-291` 对空 hook `_maybe_store_thumb(med)`（`:518-523`）的调用，改为对 `_maybe_download_thumb(msg, idx, med)` 的调用；删掉空 hook（或改其签名为转发）。
  3. **去重**：仅默认档（元数据侧）走这条同步缩略图路径；FULL 档继续只由 `_download_worker:466` 下缩略图，禁止两处重复下载同一媒体（策略判断放在 `_handle` 的分支条件，如把 `!= METADATA` 改为 `== THUMBNAIL`，或在辅助函数内加 `if policy == FULL: return`）。
  4. **外键时序**：缩略图 `save_thumbnail` 必须在 `save_message`（`service.py:324`）**之后**执行，因 `thumbnails` 表对 `(channel_id, telegram_msg_id)` 有 `FOREIGN KEY … REFERENCES messages`（`sqlite_repo.py:154-155`）；调整调用点顺序，确保消息先落库。
  5. 失败隔离：`download_thumb` / `save_thumbnail` 异常只 `log`，不阻断消息落库与主流程（沿用 `_maybe_download_thumb` 现有 try/except）；无 `thumbnail_telegram_file_id` 的媒体（如无 thumb 的文档）跳过。
  6. 缩略图仍受 `media_max_bytes` 上限（200MB）约束；默认档同样不下载原文件（D4 开关只管原文件）。
- 验收标准（GWT）：
  - **Given** 默认档（关）+ 一条带 thumb 的照片消息，**When** 消息到达，**Then** `download_thumb` 被调用一次、`thumbnails` 表出现该 `(channel_id, telegram_msg_id, media_idx)` 的 DONE 行、`thumb/<sha>` 对象存在于 ObjectStore；`download_one`（原文件）未被调用。
  - **Given** FULL 档（开）+ 带 thumb 媒体，**When** 消息处理完，**Then** 缩略图只下载一次（断言调用计数=1，非 2），原文件正常下载。
  - **Given** mock 的 `download_thumb` 抛异常，**When** 默认档消息到达，**Then** 消息仍成功落库、状态 PENDING 不变、无未捕获异常（缩略图失败隔离）。
  - **Given** 无 thumb 的文档消息，**When** 默认档处理，**Then** `download_thumb` 不被调用、消息正常落库。
  - **Given** SQLite 与 Postgres 两后端，**When** 默认档下 thumb，**Then** `save_thumbnail` 均在 `save_message` 之后被调用，外键约束不报错。

---

## 八、非功能需求

| 类别 | 要求 | 口径 |
|---|---|---|
| 性能 | 默认不下载媒体，无新增后台负载；手动下载走既有下载队列/OO，不阻塞 UI | `run_coro` 跨线程协议；`download_one` skip-if-stored 快路径 |
| 兼容 | 三平台（Windows `windows` QPA / macOS / Linux）；900px 窄窗口无裁切；CLI `--media-policy` 三态仍可用；旧 `.env` 的 `metadata` / `thumbnail` 值都归"关"侧且不报错 | CI 8/8；pydantic `extra="ignore"`；D4 映射表 |
| 可访问性 | 行操作图标带 tooltip；⋯ 菜单项有文字；状态不只靠状态色（保留字形 + 文本） | `setToolTip`、`QAction.setText` |
| 可维护性 | 行操作 objectName 统一（`#mediaRowAction` / `#mediaRowMore`）；开关语义一处定义；测试不留全平台 skip | `style.qss` 专属规则 + `test_media_actions_209.py` 零 skip |
| 安全 | 不新增明文凭据落盘；媒体 token 不进日志（沿用二维码登录口径） | 沿用现有日志与 token 处理 |

---

## 九、业务流程与状态流转

```
消息到达 → map_message() 生成 MediaDTO(PENDING, object_key=None, thumbnail_file_id 已填)
  ├─ save_message 先落库（消息行必须先于 thumbnails 行，外键约束）        ← FR-09 陷阱②
  └─ media_policy（由「同步时下载媒体本身」开关映射；只控制原文件）
       ├─ 元数据侧（关，默认）：THUMBNAIL / METADATA
       │    → 不下原文件；D8=β：同步调 _maybe_download_thumb → save_thumbnail（在 save_message 之后）
       │    → UI 出缩略图预览；原文件仍 PENDING，按需手动下载
       └─ FULL（开，媒体本身侧）→ 标 DOWNLOADING + 入队 → download_one → DONE + object_key
            → worker 内 save_message 后再 _maybe_download_thumb（唯一路径，不与上面重复）  ← FR-09 陷阱①

媒体管理页行状态 → 操作入口
  ├─ PENDING → 「下载」图标 → download_media(force=False) → DONE
  ├─ FAILED  → 「重试」图标/菜单 → retry_media(force=True) → DONE 或 FAILED（含 download_error）
  ├─ DONE    → 「打开」图标 / Reveal / Copy（只读本地字节）
  └─ 任意    → Delete（删记录+字节）、⋯ 菜单

状态机（不变）：PENDING → DOWNLOADING → DONE / FAILED；DOWNLOADING 重启后可续；FAILED 不自动无限重试，由用户手动触发。
```

**开关生效链路（D6=α）**：设置页「保存并应用」→ `reconfigure()`（不比较策略字段，直接返回）→ `update_env_with_settings()` 写盘 → UI 提示「重启后生效」→ 下次启动 `Settings.load_tolerant()` 读到新 `media_policy` → monitor 用新策略构造下载队列。

---

## 十、数据与埋点需求

本期**无新增数据字段、无 schema 变更、无迁移、无埋点**（D8=β 只启用既有缩略图链路）。

- `MediaDTO`（`dto.py:110-151`）：字段不变；"仅元数据"用现有的 `object_key=None + PENDING` 表达；`thumbnail_telegram_file_id`（`:140`）已在映射期填充（照片总有、文档按 has_thumb，见 `telethon_messages.py:153,190-192`），FR-09 直接消费。
- `thumbnails` 表（`sqlite_repo.py:139-156`、`_thumbnails_pg.py`）：DDL 不变，FR-09 让默认档也往里写 DONE 行；注意其外键 `REFERENCES messages` 要求消息先落库（`sqlite_repo.py:154-155`）。
- 存储（`sqlite_repo.py:102-130`、`_media_pg.py`）：`media` 表 DDL 不变，`download_status` 四态不变，既有"有 object_key 视为 DONE"回填逻辑（`sqlite_repo.py:121-123`、`_lifecycle_pg.py:151-154`）继续有效。
- 配置：不新增 .env key，仅复用 `TG_MEDIA_POLICY`（关→`thumbnail` / 开→`full` / 兼容保留 `metadata`）；`EditableSettings` 增加的 BOOL 是 UI 镜像字段，不落独立 key。
- 事件（`events.py:156-208`）：`MediaDownloaded` / `MediaDownloadProgress` / `MediaRetried` 不变，`download_media` 沿用。

---

## 十一、权限与安全要求

- 媒体下载/重试沿 core 的 `require_ready` 门禁与 downloader 的 `max_bytes` 上限（`config.py:109-117`，默认 200MB），本期不削弱。
- 行操作图标不使用彩色 emoji（守卫 `test_no_decorative_emoji.py`）；用 SVG/tinted 图标 + tooltip。
- 菜单项对本地字节的操作（Reveal/Copy）仅在 DONE 时可用，避免对 PENDING 行触发空 `object_key` 路径。
- 二维码登录 token 不进日志的口径不变；本期不新增任何凭据读取/写入路径（开关只写 `TG_MEDIA_POLICY`）。

---

## 十二、异常处理清单

| 异常 | 处理 | 证据 / 责任 FR |
|---|---|---|
| 媒体页重构破坏 64 位 Signal | 所有承载 channel_id 的 Signal 保持 `"qlonglong"`；新下载信号同签名；新测试用真实负 id 验证 | `test_qt_signal_64bit_ids_2006.py:85-109` / FR-03/04 |
| `run_coro` 的 on_success 跑在后台 loop 线程碰 widget 导致 SIGSEGV | VM 侧 on_success 只 emit Qt signal，主线程槽渲染 | `message_manager_widget.py:222-224` 先例 / FR-06 |
| 菜单项 enabled 与实际状态不同步 | 每次弹出前按当前行 `_RowKey` 查 `_rows` 重算 enabled；不缓存跨行状态 | `_mm_render.py:401-483` / FR-03 |
| 窄窗口文件名列被按钮挤出 | 按钮数从 5 降到 4 图标；文件名列保持 Expanding；900px 目视 | FR-03 验收 |
| 旧 .env 的 `thumbnail` / `metadata` 值在 UI 上如何呈现 | 两者均归"元数据侧"→开关显示为**关**（不是开）；tooltip 说明；保存后 `thumbnail` 保持、`metadata` 可规范化为 `thumbnail`，绝不变成 full | D4 映射表 / FR-05 |
| 用户以为改开关立即生效 | 保存成功提示明示"重启后生效"，tooltip 同步说明 | D6=α / FR-05 |
| FULL 档缩略图被下载两次（_handle + worker） | 默认档同步路径按策略去重（仅 `== THUMBNAIL` 走），FULL 档只由 worker 下；断言调用计数=1 | FR-09 陷阱① |
| thumb 落库早于消息落库触发外键失败 | 缩略图调用安排在 `save_message` 之后；两后端 save_thumbnail 测试验证顺序 | FR-09 陷阱② / `sqlite_repo.py:154-155` |
| 默认档缩略图下载失败阻断消息落库 | 沿用 `_maybe_download_thumb` 的 try/except，失败仅 log；mock 异常测试验证消息仍落库 | FR-09 / `service.py:485-510` |
| 删除媒体页 skip 测试后失去守护 | 先写 `test_media_actions_209.py` 并验证可失败，再删旧文件（同一 PR 内） | D3=β / FR-04 |
| 易碎断言改成恒真 | 保留"人为破坏对齐则失败"的反向验证用例 | FR-02 验收 |
| `_reset_list_for_render` 残留 widget | clear 时对挂件 `deleteLater()`，对齐频道页 | `_mm_render.py:220-227` / FR-03 |
| QSS 新增规则位移导致切片测试红 | 新规则放 `#channelRowAction` 块尾，改测试字符串定位方式前先跑全量 | `test_ui_polish_round2_2005.py:57-64` / FR-03 |

---

## 十三、上线计划、依赖与上线后验证

### 13.1 依赖就绪度评估

| 依赖项 | 就绪状态 | 分级 | 最晚就绪时限 | 影响/替代 |
|---|---|---|---|---|
| 三处只读诊断证据（附录 A） | 已完成，全部带文件:行号 | 已具备 | — | 无 |
| 图标资源（下载/打开/重试/更多） | 待确认 `resources/icons/` 是否已有合适图标 | 阻塞（FR-03） | PR-2 开工前 | 无则新增 4 个 SVG，复用 `tinted_action_icon()` |
| 测试基线数字 | 已确认 `1619 passed / 38 skipped / 63 deselected` | 已具备 | — | 基线用于核对删除效果 |
| offscreen 测试范式 | 已具备（`test_ui_208.py:274-305`、`tests/fixtures/_qt_runtime.py`） | 已具备 | — | FR-04 直接复用 |
| `download_one(force=False)` 快路径 | 已具备（`downloader.py:124-162`） | 已具备 | — | FR-06 直接复用 |
| QSS 单一真值源 | 已具备（`app.py:215-219`）；`dist/` 副本过期 | 可绕过 | 打包前 | 不手改 `dist/`，随构建刷新 |

### 13.2 建议 PR 切分（每分支一主题，squash merge）

顺序原则：先测试地基、再 UI 布局、最后业务语义；每个 PR 自带对应测试同步，不做"先改代码后补测试"的跨 PR 红窗。

1. **PR-1 `test(ui-cleanup)`**：FR-01 + FR-02 + FR-04 的新测试骨架（先立守护网）。删三个死/重复测试、修易碎断言、新增 `test_media_actions_209.py`（此 PR 内先只覆盖**现状**按钮契约，保证绿）。
2. **PR-2 `refactor(media-row-actions)`**：FR-03 + FR-04 更新（覆盖新图标/菜单契约）+ 图标资源 + QSS。保留 6 个 Signal 与 64 位契约。
3. **PR-3 `feat(media-download-toggle)`**：FR-05 + FR-07 + 配置/设置页测试。BOOL 开关映射 media_policy 两态，默认关。
4. **PR-4 `feat(default-thumbnail-on-sync)`**：FR-09 + `tests/test_monitor_media.py` 缩略图断言。把空 hook 接到 `_maybe_download_thumb`，处理 FULL 去重与外键时序（D8=β）。core-only 改动，不涉及 UI/schema。
5. **PR-5 `feat(media-manual-download)`**：FR-06 + core/VM/UI 测试（策略矩阵四档）。新增 download_requested → download_media(force=False)。
6. **PR-6 `chore(release)`**：版本 bump 2.0.8→2.0.9 + CHANGELOG（版本落点：`pyproject.toml`、`__init__.py`、`uv.lock`、`tgmonitor.spec` 两字段、`AGENTS.md`、`CHANGELOG.md`），`uv lock --check --offline`；**tag 与 Release 由用户确认后再触发，不手工 `gh release create`**。

每个 PR：本地全套 pytest + ruff + mypy 全绿 → 开 PR → 跟踪 CI 8/8 → squash merge → 同步 main 并删分支。基线 `1619 passed / 38 skipped / 63 deselected`；删测试后跳过数下降属预期。

### 13.3 上线后验证清单

- 默认安装：不下载原文件；带 thumb 的媒体在 thumbnails 表落缩略图、媒体页/消息页可见缩略图预览；原文件仍 PENDING，「下载」图标可点。
- 设置页：只有「同步时下载媒体本身」一个开关，默认关；勾选保存后提示重启生效；重启后新消息原文件自动下载。
- 媒体页：每行 3 个图标 + ⋯ 菜单；900px 窄窗口文件名仍可读；PENDING 点下载、FAILED 点重试、DONE 点打开均正确；⋯ 菜单三态正确。
- 64 位负 channel_id 频道：点下载/打开/重试无崩溃。
- 全套 pytest + 三平台 CI 绿；paint-probe 仍跑 `test_status_bar.py`；二维码登录、消息页、状态栏无回归。

---

## 十四、风险与遗留问题

### 14.1 风险登记册

| 编号 | 风险描述 | 类别 | 概率 | 影响 | 等级 | 应对措施 | 状态 |
|---|---|---|---|---|---|---|---|
| RK-01 | 媒体页重构碰 6+1 个 Signal 的 64 位签名，复发 OverflowError/SystemError | 技术 | 3 | 5 | 15 高 | 全部 `"qlonglong"` + 真实负 id 新测试 + 不动 VM/controller 接线 | 跟踪中 |
| RK-02 | `run_coro` 回调跨线程碰 widget 致 SIGSEGV | 技术 | 2 | 5 | 10 高 | on_success 只 emit signal；主线程槽渲染；复用消息页范式 | 跟踪中 |
| RK-03 | 删 skip 测试后重构无守护，静默破坏按钮契约 | 流程 | 3 | 4 | 12 高 | FR-04 先写可失败的 offscreen 契约，同 PR 验证后再删旧文件 | 跟踪中 |
| RK-04 | 开关映射误改默认行为（把默认变成下载） | 产品 | 2 | 4 | 8 中 | 默认 BOOL=False → THUMBNAIL（元数据侧）；`from_settings` 只有 `==FULL` 才为开；策略矩阵测试锁定"默认不下原文件" | 跟踪中 |
| RK-05 | 旧 `thumbnail`/`metadata` 值被误显示为开（v1.0 映射错误） | 兼容 | 3 | 2 | 6 中 | 两者均归关侧；配置容错测试覆盖 `thumbnail→关`、`metadata→关`，禁止 `!=METADATA` 推导 | 跟踪中 |
| RK-06 | ⋯ 菜单项 enabled 缓存导致跨行状态错误 | 技术 | 3 | 3 | 9 中 | 弹出前按当前行重算；三态 GWT 独立断言 | 跟踪中 |
| RK-07 | 易碎断言改成恒真，丢"两栏对齐"守护 | 质量 | 2 | 3 | 6 中 | 保留反向破坏验证用例（FR-02 验收第二条） | 观察 |
| RK-08 | 图标资源缺失拖慢 PR-2 | 依赖 | 2 | 2 | 4 低 | 开工前确认现有 icons，缺则补 4 个 SVG | 观察 |
| RK-09 | QSS 规则位移导致切片测试红 | 技术 | 3 | 2 | 6 低 | 新规则放块尾；必要时拆 PR | 观察 |
| RK-10 | FR-09 默认档缩略图在 FULL 档重复下载（调用点两处） | 技术 | 3 | 3 | 9 中 | 同步路径仅 `== THUMBNAIL` 走，FULL 只由 worker 下；调用计数断言=1 | 跟踪中 |
| RK-11 | FR-09 thumb 落库早于消息落库触发外键约束失败 | 技术 | 2 | 4 | 8 中 | 缩略图调用排在 `save_message` 之后；两后端顺序测试 | 跟踪中 |
| RK-12 | 默认档缩略图下载拖慢消息落库或失败阻断主流程 | 性能/技术 | 2 | 3 | 6 中 | 沿用 `_maybe_download_thumb` 失败仅 log；mock 异常测试验证消息仍落库；监控实际下载耗时 | 观察 |

### 14.2 遗留问题

- **L1** 策略字段热重载（D6=β）：需重建下载队列与 worker，本期明确不做。
- **L2** 按频道粒度媒体策略：需新增 schema 列 + repair 常量 + UI，触发条件明确再立。
- **L3** PENDING 批量下载一键入口与限速（D7=β 受控版本）：待用户确认是否要历史补齐。
- **L4** `test_main_window_close.py` 3 个 paint-skip 用例与 `style.qss` 零消费者规则清理（D1=β 的剩余部分）。

---

## 十五、附录

### 附录 A — 诊断证据索引（0af4f15 只读排查）

- **A-1 UI 测试**：`test_media_manager_widget.py:24-38`（三平台全 skip）、`:91-633`（27 例）；`test_main_window_activity_label.py:27-113`（与 `test_status_bar.py:103-170` 重复）；`test_main_window_statusbar.py:71-87`（重复映射）；`test_ui_2007_hotfix.py:82-93`（`mapToGlobal` 易碎断言）；`ci.yml:82-113`（paint-probe 唯一目标 `test_status_bar.py`）。
- **A-2 媒体页 UI**：`media_manager_widget.py:86-151`（facade + 14 Signal）、`:200-205`（QListWidget）、`:341-413`（工具栏）；`_mm_render.py:296-300`（sizeHint 800×48）、`:321-399`（5 个固定宽度列）、`:401-483`（5 个文字按钮）、`:485-490`（hover）、`:494-518`（进度）；`channel_widget.py:373-392`（QToolButton 范式）、`:335-344`（findChild 门控）、`:348-355`（deleteLater）；`style.qss:592-603`（`#channelRowAction` 三态）。
- **A-3 媒体逻辑**：`config.py:41-47`（MediaPolicy 三态：METADATA/THUMBNAIL 元数据侧、FULL 媒体本身侧）、`:108`（默认 THUMBNAIL）、`:109-117`（max_bytes）；`monitor/service.py:285-303`（策略分支）、`:518-523`（空 thumb hook，FR-09 接线目标）、`:476-510`（现成 `_maybe_download_thumb`）、`:466`（worker 内 thumb 调用）、`:389-474`（下载 worker）、`:324`（save_message，thumb 须在其后）；`monitor/downloader.py:192-264`（现成 `download_thumb`）；`telethon_messages.py:153,190-192`（thumb file_id 填充）；`sqlite_repo.py:139-156`（thumbnails 表 + 外键 `:154-155`）、`_thumbnails_pg.py:58`、`repository.py:419`（save_thumbnail）；`channel_sync/service.py:345-370`（全量同步下载）；`dto.py:75-87`（四态）、`:110-151`（MediaDTO）；`media_service/_media_os_actions.py:332-406`（retry：FAILED 守卫 + force=True）；`downloader.py:124-162`（skip-if-stored）；`settings_store.py:95-124,171-292,332-390`（序列化/EditableSettings/diff）；`settings_page.py:512-550,642-720`（策略 UI）；`_reconfigure.py:83-85`（策略变更短路）；`ui/async_bridge.py:41-87`（run_coro）。
- **A-4 测试基线**：`1619 passed / 38 skipped / 63 deselected`；mypy 144 files、ruff 全绿；CI 8/8（PR #106 run `44958602839`）。
- **A-5 版本落点**：`pyproject.toml`、`src/tgmonitor/__init__.py`、`uv.lock`、`tgmonitor.spec` 两字段、`AGENTS.md`、`CHANGELOG.md`；发版 PR 改完 `uv lock --check --offline`。

### 附录 B — 需求追踪矩阵（RTM）

| 需求编号 | 需求描述 | 来源 | PRD 章节 | 测试用例 | 状态 |
|---|---|---|---|---|---|
| R-01 | 删无覆盖/重复 UI 测试 | 诉求 1 | FR-01 | grep + 全套 pytest | 待实施 |
| R-02 | 修易碎 DPI 断言 | 诉求 1 | FR-02 | `test_ui_2007_hotfix.py:82-93`（改造） | 待实施 |
| R-03 | 行操作按钮布局优化 | 诉求 2 | FR-03 | `test_media_actions_209.py` | 待实施 |
| R-04 | 媒体页测试可执行 | 诉求 2 | FR-04 | 新测试零 skip | 待实施 |
| R-05 | 默认下载开关（仅控制原文件，缩略图属元数据侧） | 诉求 3 | FR-05、D4/D8 | `test_settings_store.py` / `test_settings_page_storage.py` | 待实施 |
| R-06 | 手动下载 + 重试 | 诉求 3 | FR-06 | `test_media_manager.py` / `test_media_downloader.py` | 待实施 |
| R-07 | 文档与配置示例 | 诉求 3 | FR-07 | `.env.example` / README 审阅 | 待实施 |
| R-08 | 无回归 | 质量基线 | FR-08、§8、§13 | 全套 pytest + ruff + mypy + CI 8/8 | 待实施 |
| R-09 | 默认档补齐缩略图下载 | 诉求 3 语义裁决 | FR-09、D8=β | `test_monitor_media.py` 缩略图断言（含去重/外键时序/失败隔离） | 待实施 |

### 附录 C — 评审决策记录

| 编号 | 决策点 | 推荐 | 本文采用 | 用户决定（待填） |
|---|---|---|---|---|
| D1 | UI 测试删除范围 | α 保守（只删零覆盖+纯重复，易碎单修） | α | 待确认 |
| D2 | 行操作按钮形态 | β 高频图标常驻 + 低频 ⋯ 菜单 | β | 待确认 |
| D3 | 媒体页测试处置 | β 重写为 offscreen 安全契约 | β | 待确认 |
| D4 | 下载开关形态 | β BOOL 映射「元数据侧/媒体本身侧」（关→THUMBNAIL、开→FULL；`==FULL` 才为开） | β（v1.1 修正映射） | 待确认 |
| D5 | 下载 vs 重试入口 | β 新增独立 Download（force=False） | β | 待确认 |
| D6 | 开关生效时机 | α 重启生效 + UI 明示 | α | 待确认 |
| D7 | 存量数据处理 | α 不补下，按需手动 | α | 待确认 |
| D8 | 默认档是否落盘缩略图 | β 补齐默认档缩略图下载 | **β（用户已拍板）** | **β 已确认** |

> **v1.1 语义裁决记录（用户 2026-11-22）**：缩略图与元数据同属元数据，只有原文件才是「媒体本身」。该裁决仅改动 D4 映射方向与文案口径（开关只管原文件），不改变 D5/D6/D7 的实施结论；D8 为由此衍生的新决策点。
>
> **v1.2 决策记录（用户 2026-11-22）**：D8 拍板 **β**。核实后确认是"接线"而非"造轮子"——`download_thumb` + `_maybe_download_thumb` + `thumbnails` 表全现成，仅空 hook 待接；已纳入 FR-09 与 PR-4，并登记两个实施陷阱（RK-10 FULL 去重、RK-11 外键时序）。至此 D1–D8 全部拍板。

### 附录 D — 合规声明

本方案为产品设计参考，具体实施请结合公司合规与法务要求确认。
