# tgmonitor 代码 Review 报告

> 时间: 2026-07-17
> 范围: 全量 `src/tgmonitor/**/*.py` + `tests/**` + `.github/workflows/*`
> 工具: 手工通读 + grep(`type: ignore` / `noqa` / `except` / `TODO` / 长函数 / 行数)
> 修复策略: 仅修"高信心、不改外部行为"的小项;结构性变更一律留 PR 后续

---

## TL;DR

扫了 52 个源模块 + 15 个测试模块 + 1 个 CI workflow,自己**改了 5 处**(详见
"Important"),大部分发现是"重要但需要专门设计"或"广泛涉及,改起来风险高",
放到 Minor / Cosmetic 段里**备案延后**,留作后续 PR。

**Critical 项:无** —— 没人用 secret 写代码、没有崩溃循环、没有数据 race。

---

## 方法论

### Grep 关键词
- `# type: ignore` 总数: **65**
- `# noqa` 总数: **88**
- `except Exception:` / `except:` 总数: **38**
- `TODO` / `FIXME` / `XXX` / `HACK` 总数: **0**(干净)
- 单文件行数 > 400 的:`core/telegram/tdlib_client.py` (1401)、
  `core/storage/jsonl_store.py` (428)、`ui/main_window.py` (360,临界)、
  `ui/widgets/account_widget.py` (432)、`ui/widgets/settings_dialog.py` (362,临界)

### 完整读了
- `src/tgmonitor/ui/icon.py`(图标系统)
- `src/tgmonitor/ui/widgets/channel_widget.py`(频道侧栏)
- `src/tgmonitor/ui/main_window.py`(主窗口 360 行)
- `src/tgmonitor/core/monitor/service.py`(监听 + whitelist)
- `src/tgmonitor/core/telegram/factory.py`(Telegram 客户端入口)
- `src/tgmonitor/app.py`(应用启动 / shutdown)
- `src/tgmonitor/core/telegram/tdlib_client.py:395-415`(`_set_state` 重构区域)
- `pyproject.toml`、`CHANGELOG.md`、`.github/workflows/ci.yml`、`CONTRIBUTING.md`

### 故意不碰
- `main_window.py:closeEvent` —— macOS CFRunLoop mutex 路径,3 个 commit 调过的敏感
  区域
- `core/telegram/tdlib_client.py` 整文件切分 —— 1401 行 + TDLib 状态机,
  需要 lifecycle + signal rebinding 设计,留给下一轮
- `core/channel_sync/service.py:sync_channels` —— 142 行含元数据 / 历史 / 取消 /
  退避 4 个独立维度,需要先写行为测试再拆
- `ui/widgets/account_widget.py:_build` 118 行 / `settings_dialog.py:_build`
  108 行 —— Qt 样板多,但 ROI 低,等抽出 `FormRow` helper 后再说

---

## Critical(无)

明确的"今天必须修否则 PR 不能合"的问题:**无**。
代码可跑、可测、CI 绿、没已知 data loss / crash / 安全漏洞。

---

## Important(本 PR 修复)

### 1. Layering 违规:`ui` 直接读 `core` 的 private 属性

**位置**: `src/tgmonitor/ui/main_window.py:236, 241, 351`(原行号)

**问题**: UI 层三次读 `self.monitor._whitelist`(private),带 `# type: ignore[attr-defined]`,
违反 `core → ui` 的分层约定(`ui/__init__.py` 明说 UI 只摸 `AppService` +
`EventBus` + DTO)。

**修复(本 PR)**:
- `core/monitor/service.py` 加 public property `subscribed_ids: frozenset[int]`
  (每次返回新副本,避免 UI 改内部状态)
- `ui/main_window.py` 三处改用 `subscribed_ids`,三处 `# type: ignore[attr-defined]` 全删

### 2. Dead-code 三元:`if False else asyncio.create_task(...)`

**位置**: `src/tgmonitor/core/telegram/tdlib_client.py:402-407`(原行号)

**问题**: `_set_state` 里历史遗留的"两路径开关",默认走 `create_task`,
`publish_threadsafe(...)` 分支被 `if False` 钉死。看着像 typo,实际是中间
过渡期的残骸,藏了 5 行视觉噪音。

**修复(本 PR)**: 删三元,直接 `asyncio.create_task(self._safe_publish_state(...))`,
把 fire-and-forget 注释留在原位置。

### 3. `CHANGELOG.md` 重复标题

**位置**: `CHANGELOG.md:34 + 36`(原行号,两行连续的 `## [0.2.0] - 2026-07-13`)

**问题**: rebase 后没 dedupe,第一个标题是孤儿(下面没内容),第二个标题才
上 `### ✨ Added`。

**修复(本 PR)**: 删 L34-L35 孤儿标题,保留 L36 那个真正开 0.2.0 节的标题。
同步在 `[Unreleased]` 段加 bullet 描述本次变更。

### 4. 重复 `setWindowIcon`

**位置**: `src/tgmonitor/ui/main_window.py:71`(原行号)

**问题**: `app.py:120` 已经设过 `QGuiApplication.setWindowIcon(load_app_icon())`,
进程级生效;`MainWindow.__init__` 再设一次除了浪费 `lru_cache` slot,还会 shadow
PyInstaller 打包时绑的 `.icns`(系统级 dock icon)。

**修复(本 PR)**: 删 L71 这一行;加一行注释解释为什么不在 `MainWindow` 自己设。

### 5. CI 没有 coverage 产出

**位置**: `pyproject.toml`(原 `tool.pytest` 后没 `tool.coverage.*`);
`.github/workflows/ci.yml` 的 `Run pytest` 后没 artifact

**问题**: 没有 `coverage.xml` 可看,就算本地能跑 `coverage report` 也
CI 上无追溯。

**修复(本 PR)**:
- `pyproject.toml` 加 `[tool.coverage.run]` / `[tool.coverage.report]`,
  dev 依赖加 `coverage>=7.0`
- `ci.yml` 加两步:`Run pytest with coverage`(收集)+ `Upload coverage
  artifact`(上传 per-OS-per-Python-version 命名)
- **显式不设覆盖率阈值门控**(避免新代码就被 churn 拒绝)

---

## Minor(延后,备案)

下面这些在后续 PR 里可能重新打开,**不在本次范围**:

### M1. 长函数 / 厚模块
- `core/telegram/tdlib_client.py` 1401 行 —— TDLib 状态机 + MessageContent dispatch
  table 都在一起,**切分要重画 lifecycle 与 signal rebinding**,先做行为测试
- `core/channel_sync/service.py:sync_channels` 142 行 —— 元数据 + 历史 +
  取消 + 退避四件事缠在一起,先写 forward 把 4 条路径行为钉住再拆
- `core/storage/jsonl_store.py` 428 行 —— 主体 `_ChannelFile` 类可拆
- `core/storage/postgres_repo.py` 385 行(临界)
- `core/app_service.py` 329 行(临界)
- `ui/main_window.py:_build_ui` 57 行
- `ui/main_window.py:_on_sync_requested` 50 行 —— 包含 dialog 启停 / progress
  信号管理,逻辑可以但视觉上粘
- `ui/widgets/account_widget.py:_build` 118 行 / `settings_dialog.py:_build`
  108 行 —— Qt 样板多,先抽 `FormRow` helper 再拆
- `ui/main_window.py:closeEvent` 63 行 —— 见方法论"故意不碰"

### M2. Stub / 死代码
- `core/monitor/service.py:180-182` `MediaDownloader.download_one` 返回 `None`
  —— 真下载要 wire `TdlibClient.download_file(file_id)`,要设计 storage
  write-back 与 retry。需要单独 PR。
- `core/telegram/tdlib_client.py:964-969` `iter_messages` stub(返回空)——
  Protocol 定义了但谁都不调用;要么实现要么删 protocol 方法
- `core/telegram/client.py:75-79` Protocol 定义 `iter_messages` 没消费者
- `core/telegram/client.py:48` 旧版 `login(phone)` Protocol 方法没人调
- `core/monitor/service.py:153-158` `_maybe_store_thumb` 是 `return None`
  stub,留作 MediaDownloader 接入点
- `_set_state` 旧 dead-switch `if False else` 见 Important-2 已修

### M3. 信号生命周期 / 资源管理
- `ui/main_window.py:325-336` SyncProgressDialog 在 `_go` future 的
  `finally` 里 disconnect —— 用户关窗时 dialog 先 destroy,可能留 dangling slot
  reference(Qt C++ 侧),直到 MainWindow 整体 GC 才释放。**本次不动**(用户
  在 plan 中明确"intentionally skip")
- `ui/viewmodels/monitor_vm.py:64-72` VM 订阅 bus 但从不退订;VM 生命周期
  等于 MainWindow 没问题,但写测试/动态重建时可能累积 subscriber

### M4. 类型 / `# type: ignore` 分布
- 65 个 `type: ignore`,22 个集中在 `tdlib_client.py`(aiotdlib 缺 stub),
  3 个集中 `main_window.py`(已修,见 Important-1),1 个 `export_dialog.py:92`
  monkey-patch `mousePressEvent`,剩下零散在 tests / settings_store / 等
- 88 个 `# noqa`,50 个是 `BLE001`(broad except);5 个 `E402`
  (tdlib 内 aiotdlib 的 try/except import guard);`N802` 1 个
  (`MainWindow.closeEvent` Qt 强制命名)
- 加 `mypy --ignore-missing-imports` 能搬掉一半(type stubs 装上即可)
- `core/storage/jsonl_store.py:383` `return message.id  # type: ignore[return-value]` —
  L378 写入但类型对不上,可换成显式 `cast` 让 lint 理解

### M5. Bare `except Exception`
- 38 处。`core/storage/postgres_repo.py:119` 与 `mongo_repo.py:124` 的
  `ping()` 返回 `False` 是有意;`jsonl_store.py:261, 416` 是 flush / set_meta
  容错设计;`app.py:127, 164, 169, 199` 在 shutdown 路径上故意宽 catch。
  真正"该记日志没记"的有 2-3 处,留给后续专题 PR 一并扫。

### M6. 文档差距
- `pyproject.toml` 没 `[project.urls]` —— 本次 B.1 修复
- README 没有"How CI works"小节 —— 本次 B.4 修复
- `CONTRIBUTING.md` 没写代理 / SOCKS5 / aiotdlib 开发步骤 —— 本次 B.5 修复
- ci.yml step name 注释稀 —— 本次 B.8 修复
- 函数级 docstring 覆盖低(core 63/234、ui 9/22)—— **不重写**,工程量大,
  留给专门一次 sweep

### M7. 其他
- `_subscribed: set[int]` in-memory 与 storage 之间潜在 drift
  (`core/app_service.py:66, 102, 169, 180, 304`)—— reload 路径可能漏同步
- 测试无 coverage 阈值配置(本 PR 加了 config 但不设阈值)
- 没有 `pytest --cov` failure gate(显式跳过)
- 没有 `F401` 之类 baseline 之前的"历史 noqa"清理(写一次 PR 一次性 unlock)

---

## Cosmetic

- 65 type-ignore + 88 noqa 分布见 M4
- `_paint_color_block`(`QPainter` 在 `QPixmap` 上画色块)+ `_kind_color` + 三色
  `_ICON_*` 常量 —— 杂糅在 `channel_widget.py`,本次**图标重做(C)**顺带
  全部删掉
- `src/tgmonitor/resources/icons/{account, channel}.svg` 是 orphan(没人调),
  本次**图标重做(C)**一并删

---

## Out of scope

下面这些**不在本次 review 范围**:

- **aiotdlib 内部机制**:异步桥、状态机、属性 setter、LIFO queue 顺序等
- **安全审查**:`session` 文件落盘、`.env` 读 / 写权限、`TG_API_*` 处理——
  这是 `security-review` skill 的事
- **性能基准**:TDLib 请求频次、SOCKS5 握手延迟、Qt 渲染瓶颈等
- **i18n**:`tr()` / Qt linguist / 单复数
- **打包 / 安装**:PyInstaller spec / RPM 包 / Homebrew formula / winget
- **CI 平台迁移**:自托管 runner / Coverage 平台 codecov 集成(本 PR 只产 artifact)

---

## 后续 PR 候选(优先级粗排)

1. **tdlib_client.py 切分** —— 影响最深,但要先保证行为测试覆盖
2. **sync_channels 重构** —— 同样的前置:行为测试
3. **MediaDownloader 真下载** —— 设计 doc -> 实现
4. **Docstring sweep**(核心服务层优先)
5. **FormRow / FormGroup helper 抽出** —— 让 `_build` 长函数自然缩短
6. **Qt offscreen visual regression** —— `test_message_view` 加截图对比
7. **aiotdlib type stubs** —— 22 个 `# type: ignore` 一半可消
