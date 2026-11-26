# PySide6 6.12 解禁归因史（已归档）

> **历史文档**（2026-11-26 自 `AGENTS.md` 迁出）。点时间快照，记录 2026-11-14
> 把 PySide6 上限从 `<6.12` 放宽到 `<6.13` 的完整归因过程。现状以 `AGENTS.md`
> 与 `pyproject.toml` 为准；改依赖上限时回看这里。
>
> 结论先行：**6.12 可用**。当年的 SIGSEGV 由测试侧跨线程 loop fixture +
> widget 累积触发，治理（`qloop` 全会话共享 + per-test `deleteLater`）后消失，
> 三平台 CI 与生产退出路径探针均已验证。

## 6.12.0 的破坏性变更（记录保留作历史依据）

- `QPixmap.loadFromData` 的 format 参数在**运行时**由 `bytes` 改为 `str`（不只是
  mypy stub 变严），显式传 `b"GIF"` 直接 `TypeError`。GIF 首帧解码已改为省略
  format 让 Qt 自动探测，跨版本成立。
- 解释器 shutdown 时 Qt 对象销毁顺序收紧。
- meta 包新增 WebEngine/PDF 硬依赖（对打包体积**无影响**，见下文 ④）。

## 初次归因（2026-11-14，6.12.0 麻离 worktree 实测）

> 探针脚本：`scripts/diagnostics/pyside6_shutdown_probe.py`

- 不跑 pytest 的探针三档（纯 Qt / 生产 MainWindow / 跨线程残留）共 60 次**零崩**。
- 全套 pytest 5 次里 4 次 SIGSEGV（3 次在 100% 后的纯 C++ 析构 `<no Python frame>`，
  1 次在 17% 的 `qloop_16` fixture teardown）、1 次 timeout hang。
- UI 三文件合跑 8/8 崩、非 Qt 四文件 8/8 正常、三个 UI 文件各自单跑 5/5 正常。
- faulthandler 栈指向 `qloop` / `LoopThread` 的跨线程 future 等待或 fixture
  teardown，无生产代码帧 → **直接触发者是测试侧跨线程 loop fixture + widget
  累积到进程退出**，生产启动 / 退出路径未复现。但被析构对象是生产 widget 实例，
  且 6.11 时代生产侧另有未根治的 segfault 记录（`ui/shutdown.py` /
  `closeEvent` / `processEvents`），当时不能 100% 排除参与。

## 结论修正（同日，PR #38 反向验证 + 生产退出路径探针）

- 治理后崩溃在 6.12 完全消失（`qloop` 共享 + per-test `deleteLater`），触发条件
  就是测试侧累积状态 —— 从"推断"升为"实测坐实"。
- 生产退出路径探针（真 `MainWindow` → `closeEvent` → `run_shutdown_coro_sync`
  → `processEvents`，60 次独立进程）在 6.12 下**零崩**。
- 那批"历史 segfault"是 6.11 时代且**已有绕开**：Windows offscreen
  `processEvents` 必崩 → 已改 `windows` 插件；嵌套 `QEventLoop.exec()` segfault
  → 已改 `processEvents` 而非 exec。均不阻塞解禁，属独立质量债。

## 反向验证（治理后，6.12.0 隔离 worktree）

`tests/fixtures/_qt_runtime.py` 把 4 处 `qloop` + 1 处 `bg_loop` 统一成**全会话
共享 1 个后台 loop**，并 per-test `deleteLater()` 顶层 widget（不调 `close()`）、
`pytest_sessionfinish` 停掉 loop 线程。同一组实验：

| 场景 | 基线 | 治理后 |
|---|---|---|
| UI 三文件合跑 | 8/8 SIGSEGV | 3/3 `rc=0` |
| 全套 | 4× SIGSEGV + 1× hang | 3/3 `rc=0`，跑到 100% |
| 非 Qt 四文件 | 1/1 正常 | 1/1 正常 |

## 解禁前剩余动作（收尾记录）

- ① ②（治理 `qloop` 共享 + per-test `deleteLater`）：完成并被反向验证坐实。
- ③ 三平台 CI 复验：由解禁 PR 的 CI 承担（含 Windows `windows` QPA 的
  `sendPostedEvents(DeferredDelete)`）。
- ④ 6.12 新增 WebEngine/PDF 对打包体积的影响：**无影响，划掉**。6.12 隔离环境
  打包 `dist/` = 361M（与 6.11 基线一致），WebEngine **0 文件**进包（PyInstaller
  按 import 收集，本应用不 import `QtWebEngine`）；QtPdf 被拉入但体积不变。
  "6.12 新增硬依赖致体积膨胀"是 PR #37 的误判。`tgmonitor.spec` 的 `excludes=[]`
  保持不动。
- ⑤ 生产侧历史 segfault：不阻塞解禁（均有绕开），建议后续单独治理。
- ⑥ **2026-11-14 用户拍板解禁，PR #44 合并**：上限改 `<6.13`，`uv.lock` 升
  6.12.0；CI 三平台 8/8 全绿 + 本地 PyInstaller bundle 体积与 6.11 基线一致。
  评估期 `.github/dependabot.yml` 临时加的 `pyside6 >=6.12` ignore 已随解禁删除。
