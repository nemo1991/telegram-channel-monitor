# Changelog

本项目的所有显著变更都会记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
版本遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 🛠️ Refactored
- **清 `run_coro` `coroutine-never-awaited` RuntimeWarning**(`[本轮]` `ui/_async.py`):
  - `test_main_window_close.py` 跑时 2 处 warning — `_go` coro 在 loop close 时未 tick 完
    GC 被收。`_async.py` 加 module-level `_PENDING_FUTS: set[asyncio.Future]` hold
    future 强引用,`_on_done` 回调时 `discard`,业务 / 接口都不变
  - pytest `tests/test_main_window_close.py` 7 个 case 干净跑过,0 warning
- **mypy 修 `ui/` + `app.py` 107 错清零**(`f7aec72` 2026-08-04):
  - 之前 107 错 / 22 文件:
    - `attr-defined` × 63(PySide6 stub 不全:`Qt.AlignCenter/UserRole/PointingHandCursor`、
      `QFrame.NoFrame`、`QSizePolicy.Expanding/Fixed`、`QHeaderView.Stretch`、
      `QDialogButtonBox.Ok/AcceptRole` 等)
    - `union-attr` × 21(`_HeaderBar` 类变量 `None` 占位 + `.btn_logout.clicked` 等
      实例属性调用)
    - `unused-ignore` × 8 / `valid-type` × 4(`callable` 当 type annotation)/
      `misc` × 4 / `arg-type` × 4 / `return-value`/`var-annotated`/`assignment` 各 1
  - 分层修法:
    - 11 个 ui 文件顶部加 `# mypy: disable-error-code="attr-defined"` —
      PySide6 stub 不全是已知上游问题,源码层无业务含义,源头一次性关
    - `_HeaderBar` 删 4 个 `btn_X = None  # type: ignore[assignment]` 占位
      类变量 — `_HeaderBar()` 永远走 `__init__`,占位是历史遗留,清 21 union-attr
    - `Callable[[], None]` 替换 `callable`(sync_dialog + dashboard 共 4 处)
    - `_async.py` `cast(asyncio.Future[T], ...)` 修 `run_coroutine_threadsafe`
      返 concurrent.futures.Future 不匹配 stub 的问题
    - `main_window.py:closeEvent` `fut` 显式标 `concurrent.futures.Future[None]` +
      cast `Coroutine[Any, Any, None]` 修 `Awaitable`/`Coroutine` 类型不兼容
    - `login_dialog.py:submit_code/password` lambda 展开 `(state, detail)` tuple
    - `sync_dialog.on_done` 用 isinstance 窄化 `ChannelSyncDone.result` (`object`)
      → `SyncResult`,保留 events.py 的 `object` 占位(避免 ui→core 循环 import)
    - `monitor_vm._on_settings_changed` isinstance 窄化 `SettingsChanged.new_settings`
      → `Settings`,`Settings` 走 `TYPE_CHECKING` import 不引入新依赖
    - `channel_widget._empty_hint` 类型 `object` → `QWidget | None`
    - `settings_page._set_form_row_visible` `item.widget()` 显式 None 守卫
    - 删 3 处 unused-ignore(`app_get_state` name-defined / `EditableSettings` /
      `channel_widget._empty_hint`)
  - 结果:**107 → 0 错 / 22 文件**,pytest 全过,ruff 0 warning

### 🔧 Changed
- **CI 依赖与平台边界修复**(2026-08-04):
  - `mypy>=2.3.0` 补进 `[dependency-groups].dev` + `uv.lock` — 之前本地环境有
    mypy,但 CI `uv sync --group dev` 后没有可执行文件,导致 16 个 mypy matrix job
    全部报 `Failed to spawn: mypy`
  - `tests/test_visual_regression.py` 在非 macOS 平台整模块 skip — golden 来自 macOS,
    Ubuntu 因字体 hinting / widget size 不同会稳定失败(8/8),与独立
    `visual-regression.yml` 只跑 `macos-latest` 的既有边界对齐
  - CI `Verify CLI entry` 从 `uv run tgmonitor --help || true` 改为仅加载
    `console_scripts` entry point — 本应用没有 argparse `--help` 路径,旧命令会实际
    启动 Qt event loop,macOS runner 永久卡在该步骤
- **mypy `src` 整包 0 错**(2026-08-04):
  - 之前 71 错 / 5 文件:
    - `tdlib_channels.py` × 28(2026-08-02 composition 拆出来的新文件,从没过 mypy):
      `Cannot assign to a type` × 10(aiotdlib `None` 占位 + TYPE_CHECKING import
      冲突)/ `func-returns-value` × 5 / `Missing @extra/@type/offset/limit` × 8 /
      `arg-type str→int` × 2(DownloadFile/GetFile stub 误报)
    - `tdlib_client.py` × 11:`ClientSettings = None` 占位 × 4 / `start` 返回
      `Coroutine[...]` vs `aiotdlib.Client.start` supertype mismatch × 1 /
      `iter_chat_history` override × 1 / `Check*/SetLog*/GetAuthorizationState/
      LogOut` 缺 `@extra/@type` × 5
    - `settings_store.py` × 3:`reload_settings` 错传 `_env_file=...`(pydantic
      实际接收 `env_file=...`)→ 改 `env_file` + 加 `# type: ignore[call-arg,arg-type]`
    - 7 个 aiotdlib 实例化点加 `# type: ignore[call-arg]`(`Check*` / `SetLog*` /
      `GetChats` / `GetChatHistory` / `SearchPublicChat` / `LogOut` / `GetAuthorizationState`)
  - 分层修法:
    - `tdlib_channels.py` / `tdlib_proxy.py` / `tdlib_messages.py` 顶部加
      `# mypy: disable-error-code="misc,assignment"` — aiotdlib `None` 占位
      与 pydantic stub 缺失是已知上游问题,源头一次性关
    - `tdlib_client.py` 顶部加 `# mypy: disable-error-code="misc,assignment,override"`
      — 同上,多关 override(aioTDLib Client.start / iter_chat_history 父类
      签名跟我们的子类实现形状不同)
    - TYPE_CHECKING 块 import 真实类型(`from aiotdlib.api import _GetChat` 等),
      让 aiotdlib stub 在 mypy 推断时被类型化,避免 `cast(Any, None)` 的
      `Cannot assign to a type` 噪声
    - `func-returns-value` 是 aiotdlib stub 误报(实例化必填 `@type` 缺失 →
      mypy 推断为 `None`),针对性加 `# type: ignore[func-returns-value]`
    - `arg-type str→int` 同根因(DownloadFile/GetFile stub 误报 file_id
      必须是 int,实际接受 str)— `# type: ignore[arg-type]`
  - 结果:**71 → 0 错 / 65 文件**,pytest 全过(248 passed + 2 个已知 warning),
    ruff 0 warning
- **mypy CI 矩阵 7 → 8 entry**(`.github/workflows/ci.yml`):
  - `module` 列表加 `src`(整包全局 sanity check),守住 `__main__.py` /
    `core/settings_store.py` / `core/telegram/tdlib_{channels,proxy,messages}.py`
    这些没在 entry 矩阵里的文件
  - entry 数:7 × 2 OS = 14 → 8 × 2 OS = 16 job
- **mypy CI 矩阵 5 → 7 entry**(`.github/workflows/ci.yml`):
  - `module` 列表加 `src/tgmonitor/ui` + `src/tgmonitor/app.py`(上一轮 fix 后
    已 0 错,纳入 CI 守住)
  - entry 数:5 × 2 OS = 10 → 7 × 2 OS = 14 job

## [1.0.7] - 2026-08-03

🛠️ **mypy 矩阵扩展 + 视觉回归扩 MainWindow + UPDATE_GOLDENS CI** —
4 件套:app.py 8 处 pre-existing 类型错清零;mypy CI 矩阵从 1 模块扩到
5 模块(monitor / storage / export / objectstore / telegram);视觉回归
覆盖扩到 MainWindow(8 widget);CI 加 `visual-regression.yml` workflow,
UI/测试改动自动重生成 golden + 上传 artifact 供 reviewer 比对。
零用户可见行为变更。

### 🛠️ Refactored
- **修 `app.py` 8 处 pre-existing 类型错**(`[tool.mypy] + 协议补漏`,
  `2026-08-03`):
  - `StorageRepository` 协议补 `@abstractmethod init_schema()`(`core/storage/
    repository.py`,+5 行)— 之前 3 个 repo 都实现但协议没声明,mypy
    报 "StorageRepository has no attribute init_schema",运行时
    `_load_from_settings` 调不到引发崩溃
  - `app.py:32` 删 unused `# type: ignore[call-arg]`(Settings 构造已支持)
  - `app.py:191` `state` dict 类型注解:`dict[str, object]` →
    `dict[str, AppService | MonitorService | object]`;`_shutdown_async`
    收 `_state.get(...)` 后用 `isinstance(x, AppService/MonitorService)` 守卫
    代替 `# type: ignore[assignment]`,mypy 看到真类型,3 个 attr-defined 错消失
  - `app.py:32` `Settings() # type: ignore[call-arg]` → `Settings()`
  - `pyproject.toml [tool.mypy.overrides]` 扩 `PySide6.*` / `qasync` /
    `asyncpg` / `motor.*` / `aioboto3.*` / `jinja2`(全视为 Any)

### 🔧 Changed
- **mypy CI 矩阵扩 5 模块**(`0a8609c` → 本轮,`.github/workflows/ci.yml`):
  - `[tool.mypy.overrides]` module 列表扩展到 7 个外部依赖
  - mypy job `matrix.module` 从 `["src/tgmonitor/core/telegram"]` 扩到:
    `core/monitor` / `core/storage` / `core/export` / `core/objectstore` /
    `core/telegram`
  - `core/storage` 顺手清 6 个 mypy 错:`mongo_repo._doc_to_message` /
    `save_message` `MessageDTO.id` 走 `int(str(...))`(Mongo `_id` 是
    ObjectId str,DB 主键语义不变);`jsonl_store.py:353` + `mongo_repo.py:253`
    删 unused `# type: ignore[return-value]`
- **CI 加 `visual-regression.yml`**(`2026-08-03`,`.github/workflows/`):
  - 触发:`tests/test_visual_regression.py` / `tests/golden/**` /
    `src/tgmonitor/ui/**` 改动
  - 跑 `UPDATE_GOLDENS=1 pytest`,把重新生成的 8 个 golden PNG 上传为
    artifact(`goldens-regen-<os>-py<ver>`),reviewer 下载跟 PR 改动对比
  - 只跑 `macos-latest` — goldens 对字体 hinting / anti-aliasing 敏感,
    跨 OS golden 一致性不在本期范围

### ✅ Tests
- **视觉回归扩 MainWindow 初始状态**(`[本轮]` `tests/test_visual_regression.py`):
  - `test_main_window_initial`:Mock `app` + `monitor` + 真 `Settings`,
    grab MainWindow(1180×740)— dashboard 视图全空:已订 0 / 已加入 0 / 空消息区
  - `tests/golden/main_window_initial.png` 入库(第 8 个 widget)
  - 收尾清理:`asyncio.sleep(0)` 让 `bootstrap_ui` 排进 loop 的 task tick 一次,
    `cancel()` + `gather(*, return_exceptions=True)` 避免
    "coroutine was never awaited" 警告
- **视觉回归覆盖 widget 总览**:`MessageView`(空 / 3 条) +
  `ChannelWidget`(3 频道) + `SettingsPage`(默认) + `DashboardWidget`(空) +
  `ExportDialog`(默认) + `LoginDialog`(初始) + `MainWindow`(初始 dashboard) — **8 widget**

## [1.0.6] - 2026-08-03

🛠️ **架构清理 + 类型 stub + 测试基建 release** — 3 commits 收尾:
AppService facade 微切 + aiotdlib type stubs 35→0 + Qt visual regression
基建。零用户可见行为变更,纯代码质量 / 类型 / 测试提升。

### 🛠️ Refactored
- **`AppService` facade 微切**(`989b9af` 2026-08-03)— 357 行 facade 拆 3 件套:
  - **NEW `AuthService`** (`core/auth_service.py`,+85 行)— 持 `bus + client +
    settings`,只管鉴权这一摊;3 个 submit_* 方法 + `_check_credentials` +
    `_fail(source, exc_or_msg)` 统一错误路径。AppService 3 个 submit 收成 1 行
    delegate
  - **`SettingsDiff` dataclass**(`core/settings_store.py`,+53 行)— frozen
    dataclass:`needs_relogin` / `storage_changed` / `objects_changed`,
    `diff_settings(old, new) → SettingsDiff` 纯函数;给 `reconfigure` 当决策表
  - **`_what_label(diff)` helper**(`core/app_service.py`,+15 行)— 从 `reconfigure`
    抽出,`SettingsChanged.what` 字符串生成器
  - **`reconfigure` 75 行 → 22 行** + `_rebuild_storage` (15 行) +
    `_rebuild_objects` (10 行)
  - **顺手修 bug**:`bootstrap` L115 `event_bus=self._bus` → `self.bus`
    (latent since v1.0.4,401 retry 路径 AttributeError)

### 🔧 Changed
- **`aiotdlib type stubs 清理**(`6dc3cc8` 2026-08-03)— `pyproject.toml` 新增
  `[tool.mypy]` section(strict 默认 + `aiotdlib` / `platformdirs` /
  `pydantic` / `pydantic_settings` `ignore_missing_imports`)。4 个 tdlib 模块
  35 处 `# type: ignore` 全清(plan 原估消 14-16,超额完成):3 处 import-guard
  fallbacks + 19 处 `X = None` 兜底赋值 + 2 处 call-arg/arg-type 都成 unused
  (aiotdlib 视作 Any 后自然消失)
- **mypy 配置文件就位** — `warn_unused_ignores = true` / `warn_redundant_casts`
  / `no_implicit_optional = true` 启用;严格度有意放低(`disallow_untyped_defs
  = false`),pyright-like 全开会扫到 pydantic / Qt / 测试 fixture 几百个噪声

### 🧪 Test
- **Qt offscreen visual regression 基建**(`870869b` 2026-08-03)— REVIEW M1
  长期候选落地:`tests/test_visual_regression.py`(~150 行)+ `tests/golden/`
  3 个 PNG(55 KB 总)
  - `MessageView` 空状态(「暂无消息」overlay)
  - `MessageView` 3 条 mock 消息
  - `ChannelWidget` 3 条 mock 频道
  - 纯 `QImage.pixelColor` 逐像素比对(零新依赖)
  - 容差 0.1% 像素差异(留 anti-aliasing / sub-pixel 抖动余地)
  - `UPDATE_GOLDENS=1` env var 重新生成 golden
  - 失败存 `_diffs/<name>_current.png` + `_expected.png` 给人眼对比
- **243 测试通过**(240 + 3 new),ruff 0 warning,行为零变化

## [1.0.5] - 2026-08-03

📚 **Docstring sweep release** — 给所有公开 API / 类 / `__init__` / 公开方法
补 docstring,跨 5 层(objectstore / storage / export / telegram / core / ui)
+ `__main__`。零功能变更,纯文档补全;`ruff D102/D101/D103/D105/D107` 从
不达标 → 全绿。

### 📚 Documentation
- **objectstore 4 文件 + 38 docstring**(`d937aa5` 2026-08-03)— `LocalObjectStore` /
  `FolderObjectStore` / `S3ObjectStore` / `InMemoryObjectStore` 公共方法签名 +
  写盘策略(分片 / 去重 / multipart 上传阈值)
- **storage 6 文件 + 74 docstring**(`8fc49ec` 2026-08-03)— `Repository` Protocol +
  Postgres / Mongo / JSONL 三个实现,upsert / 分页 / 索引策略;补 `StorageFactory`
  + `ensure_dirs` 语义
- **export 6 文件 + 17 docstring**(`4b422d3` 2026-08-03)— JSON / CSV / Markdown /
  HTML 四种格式的字段映射 + base64 缩略图策略;补 `ExportService.run` 进度回调
  协议
- **telegram 5 文件 + 22 docstring**(`62bf408` 2026-08-03)— `TelegramClient`
  Protocol + `TdlibTelegramClient` lifecycle + `ChannelsApi` + `tdlib_messages` /
  `tdlib_proxy` 错误语义
- **core 7 文件 + 49 docstring**(`1dc90f6` 2026-08-03)— `AppService` facade +
  `EventBus` + `dto` + `settings_store` + `config` + `monitor` + `channel_sync`
  公共方法签名
- **UI cluster A:4 文件 + 16 docstring**(`1a04b9a` 2026-08-03)— `MainWindow` +
  `VerticalNavBar` + `Theme` + `MonitorViewModel`(包括 `closeEvent` 同步阻塞
  shutdown 10s 超时说明)
- **UI cluster B:3 文件 + 11 docstring**(`c1369e9` 2026-08-03)— `LoginDialog` +
  `ExportDialog` + `SyncOptionsDialog` / `SyncProgressDialog`(包括 `on_progress`
  stage→icon 映射表)
- **UI cluster C:6 文件 + 14 docstring**(`47b42ae` 2026-08-03)— `ChannelWidget` /
  `DashboardWidget` / `MessageDetail` / `MessageView` / `SearchBar` /
  `SettingsPage`(包括 `MessageView.append` 去重表 + row index 维护说明)
- **__main__ 1 文件 + 1 docstring**(`99ef9ff` 2026-08-03)— `main()` 退出码
  语义(0/1/130)
- **236 测试通过**(0 新增 / 0 回归),ruff 0 warning,行为零变化

## [1.0.4] - 2026-08-02

🛠️ **`ChannelsApi` composition 拆分 release** — `tdlib_client.py` 余下
1025 行再切,channels 子块(287 行)抽到独立 `ChannelsApi` composition 类。
零功能变更,纯架构清理。

### 🛠️ Refactored
- **`tdlib_client.py` 1025 行 → 762 行 + `tdlib_channels.py` 379 行**(2026-08-02)—
  channels 子块(287 行:6 个公开方法 + 2 个内部 helper)抽到 `ChannelsApi`
  composition 类,持 `client: TdlibTelegramClient` 引用访问 lifecycle 资源
  (`request` / `send` / `_check_alive` / `_state` / `_closing` / `_wait_for_state`)。
  `tdlib_client.py` 上 6 个同名方法改为 thin delegate,`TelegramClient` Protocol
  形状不变,所有 caller 零改动
- 4 个 `_iter_resolved_chats` 测试修复:`client._iter_resolved_chats` /
  `client._resolve_channel_metadata` → `client.channels._iter_resolved_chats` /
  `client.channels._resolve_channel_metadata`(monkey-patch 跟随迁移)
- `core/telegram/__init__.py` 加 `ChannelsApi` re-export(便于类型注解 / 测试可见,
  外部 caller 仍走 `client.list_joined_channels(...)`)
- 子模块 reverse import 校验:`tdlib_channels.py` 通过 `TYPE_CHECKING` 块软引用
  `TdlibTelegramClient`,runtime **不**反向 import `tdlib_client`,无循环依赖
- 236 测试通过(72 + 72 + 72 + 20),ruff 0 warning,行为零变化

### 🔧 Changed
- **`version = "1.0.3"` → `"1.0.4"`** — `pyproject.toml` +
  `src/tgmonitor/__init__.py` 双处对齐(避免 v1.0.0 修过的 drift bug 复发)

## [1.0.3] - 2026-08-02

🛠️ **`tdlib_client.py` 长文件切分 release** — 1674 行单文件按职责拆 4 个
内聚模块,lifecycle controller 收窄,无功能变更,纯架构清理。

### 🛠️ Refactored
- **`tdlib_client.py` 1674 行拆 4 文件**(2026-08-02)— pure functions
  抽到独立模块,无功能变更:
  - `tdlib_errors.py`(51 行)— `_extract_error_detail` /
    `TelegramRateLimitError` / `ClientClosingError`
  - `tdlib_proxy.py`(239 行)— `parse_socks5_proxy` /
    `_load_or_create_encryption_key` / `_probe_proxy` /
    `_translate_boot_error` / `_AUTH_STATE_MAP`
  - `tdlib_messages.py`(433 行)— `_map_message` + 媒体 / service 派发表
    (8 media + 32 service handlers)
  - `tdlib_client.py` 1674 → 1025(−39%)— 保留 aiotdlib.Client 子类化 +
    lifecycle controller + channels 子块;REVIEW 警告的"信号 rebinding"
    集中区整块保留,本轮**不**拆
- 子模块 reverse import 校验:3 个新模块**不**反向 import `tdlib_client`,
  无循环依赖。`TelegramRateLimitError` / `ClientClosingError` /
  `parse_socks5_proxy` 在 `core/telegram/__init__.py` re-export,
  外部 caller (`channel_sync/service.py`) 改走 `tgmonitor.core.telegram`
  顶层 import
- 测试 import 同步更新:7 个测试文件 + 2 个 src 文件;
  `tdlib_client as tdc` 模块别名 6 处仍可工作(模块本身未删)
- 236 测试通过(72 + 72 + 72 + 20),ruff 0 warning,行为零变化

### 🔧 Changed
- **`version = "1.0.2"` → `"1.0.3"`** — `pyproject.toml` +
  `src/tgmonitor/__init__.py` 双处对齐(避免 v1.0.0 修过的 drift bug 复发)

## [1.0.0] - 2026-07-23

🎉 **首个正式 release** — Stage D 打包就绪 + Stage C REVIEW M2 修复一并交付。

### ✨ Added
- **PyInstaller 打包**(`tgmonitor.spec` + `hooks/hook-aiotdlib.py`)
  — 跨 Linux / macOS 双平台产物;`collect_data_files` 自动收集
  aiotdlib TDLib native lib + SVG / QSS / icons
- **Linux AppImage**(`scripts/build_appimage.sh`)— 单文件免安装,
  rsvg-convert 转 PNG icon + .desktop entry + AppRun,AppImageKit
  `appimagetool` 打包
- **macOS `.app` bundle**(BUNDLE spec)— LSMinimumSystemVersion 13.0,
  ad-hoc 签名(用户拍板不申请 Developer ID)
- **GitHub Actions `build.yml`** — `git tag v1.0.0` 触发双平台
  matrix build + SHA256SUMS + `softprops/action-gh-release@v2` 自动发
  GitHub Release
- **Hatchling wheel 资源 inclusion**(`tool.hatch.build.targets.wheel.include`)
  — `pip install tgmonitor`(从 PyPI)能跑,SVG / QSS 跟 binary 一起 ship
- **README「📥 下载安装」章节** — 用户下载指引 + 系统需求 +
  macOS Gatekeeper 解锁步骤

### 🐛 Fixed
- **`__version__` drift bug** — `src/tgmonitor/__init__.py:3` 之前是
  `"0.1.0"` vs `pyproject.toml:3` 是 `"0.2.0"`,v1.0.0 bump 一起对齐
- **REVIEW M2.1 — FULL 模式用户之前下不到任何原文件** —
  `MediaDownloader.download_one` 真实现随 v1.0.0 一起交付
  (Stage C 详细 changelog 见 Unreleased)

### 📦 Packaging
- `.app` 未签名 / 未公证 — 用户 Gatekeeper 手动允许
- `.AppImage` 自带 TDLib,无需装 system TDLib
- **不打 Windows 版本** — upstream `aiotdlib 0.27.x` 无 Windows wheel
- **不打 `.deb` / `.rpm` / Homebrew formula / winget** — 留 v1.0.x

### 🔧 Changed
- **`version = "0.2.0"` → `"1.0.0"`** — 首个 release tag
- **`pyinstaller>=6.21.0`** 加到 `[dependency-groups].dev` — build workflow
  直接 `uv sync --group dev` 就够

## [1.0.1] - 2026-07-23

🗂️ **数据目录迁移到 platform-native path** — macOS `.app` 双击启动
不再写到 `/data/`(root 不可写),所有数据走 OS 标准 user-data 目录。

### ✨ Added
- **数据目录走 platform-native**:
  - macOS: `~/Library/Application Support/tgmonitor/`
  - Linux: `$XDG_DATA_HOME/tgmonitor/`(fallback `~/.local/share/tgmonitor/`)
  - Windows: `%APPDATA%/tgmonitor/`(为未来 Windows 平台预留)
- **Settings 对话框「默认」按钮** — 4 个 Path 字段(`session_dir` /
  `db_root` / `objectstore_root` / `data_root`)各加一个,点一下重置为
  platform-native 默认路径。
- **macOS `.app` libtdjson hotfix**(commit `6e0e64f`)— PyInstaller spec
  改 aiotdlib data destination 从 `"aiotdlib"` 到 `"aiotdlib/tdlib"`,
  保留 `tdlib/` 子目录让 aiotdlib loader 解析得到。

### 🔧 Changed
- 新增 dep:`platformdirs>=4.11.0`
- `Settings` 4 个 Path field 改 `default_factory=lambda: _user_data_dir() / subdir`,
  `model_config.env_file` 改 `str(_user_data_dir() / ".env")`
- `app.py` 删除 4 个 `.resolve()`(已经是绝对路径);`Path(".env")` fallback
  改 `_user_data_dir() / ".env"`
- `MainWindow.env_path` fallback 同步改 `_user_data_dir() / ".env"`

### 🐛 Fixed
- **v1.0.0 `.app` 启动崩溃** — `cwd=/`,`Path("./data/...").resolve()`
  写到 `/data/`(root 不可写)→ app 立刻挂;v1.0.1 走 platform-native
  路径后,从 `/Applications` 双击启动正常工作。

### 📦 迁移
- **没有自动迁移代码** — v1.0.0 没正式发板,无存量用户。早期用户在
  第一次跑 v1.0.1 前手动把数据复制到 platform-native 目录即可。

## [1.0.2] - 2026-08-02

🛠️ **稳定性 + 长函数切分 release** — `_subscribed` cache 漂移三连修复
+ 8 项 REVIEW M1/M2 refactor sweep + sync_channels 拆 4 阶段 +
`jsonl_store` 子模块化。无功能新增,纯健壮性。

### ✨ Added
- **`run_coro` helper(`src/tgmonitor/ui/_async.py`)**(2026-07-30,commits
  `7c8ebfe` + `4319d6b`)— 11 处 `asyncio.run_coroutine_threadsafe(coro, loop)
  + add_done_callback(_on_done)` 样板切到统一入口 `run_coro(loop, coro, *,
  on_success, on_error, error_label)`,覆盖 6 个 UI 文件
  (monitor_vm / channel_widget / dashboard_widget / main_window /
  login_dialog / settings_page),净减 36 行。异常归一(任何 BaseException
  走 `log.exception(error_label)` + 调 `on_error`);on_success / on_error
  自身抛 → `log.exception` 兜底,不污染 future;helper 设计延续 #6 (`form_row.py`)
  思路 — 抽"如何 fire + 异常归一"成本,不抽 on_success 内部业务。
  9 个新单元测试覆盖 success / 异常 / self-throw / TypeVar 透传 4 路径。
- **form_row.py 扩展 `combo_field` + `spin_field`**(2026-07-30,commit
  `a19a32b`)— 延续 #6 思路:`combo_field(form, label, options)` 支持
  `Iterable[Enum]` / `Iterable[tuple[Any, str]]` 两种形态;`spin_field(
  form, label, *, min, max, value, suffix, single_step, tooltip)` 全
  keyword-only 参数避免与 builtin `min`/`max` 关键字混淆。settings_page.py
  6 处切到 helper(3 QComboBox + 4 QSpinBox → 实际合并 6 块),净减 21 行,
  QComboBox / QSpinBox import 0 caller 清理。10 个新单元测试覆盖
  placeholder / echo / on_default / Enum / tuple / addRow / spin
  suffix+step+tooltip。
- **FormRow helper(`src/tgmonitor/ui/widgets/form_row.py`)**(2026-07-30,
  commits `ca85c15` + `18ddd19`)— 集中 `text_field` / `path_field` 两个
  高频样板:`settings_page.py` 6 处 + `export_dialog.py` 1 处切到 helper,
  净减 47 行样板代码。`path_field` 支持 `file_mode=True` 切换
  `getSaveFileName`(export 场景),平台默认路径(helper 不感知 Settings,
  caller 用 `_user_data_dir()` 注入)。`settings_page._browse_dir` /
  `export_dialog._browse` 0 caller 顺手删除。
- **`TelegramClient.download_file` Protocol + TDLib 真实现 + Fake 双胞胎**
  (2026-07-22,commit `d6247b1`)。两步:`DownloadFile(synchronous=False)`
  触发后台下载 + `GetFile` 轮询到 `local.is_downloading_completed` 读 bytes;
  失败 / 30 min hard cap → 返 None 不抛,monitor loop 继续。`Path.read_bytes`
  用 `asyncio.to_thread` off-loop 跑,免 block qasync / uvloop loop
  (ruff ASYNC240 修)。
- **`MediaDownloader.download_one` 真实现 + wire `MonitorService._handle`
  FULL 分支**(commit `29ed08d`)。`media.telegram_file_id` 拉原文件 →
  `objects.put` → `dataclasses.replace` 返新 MediaDTO(`object_key` /
  `object_backend` / `file_size` 已填);`MonitorService` 加可选 `downloader`
  字段,FULL 模式现在**实际下得到原文件**,之前是 metadata + thumb + 空 key。
- **`Settings.media_max_bytes`** — 单文件下载上限,默认 200 MB,0 = 无限制。
  UI `SettingsPage` 加 `QSpinBox`(MB 显示,0-10240 MB);`.env` 字段
  `TG_MEDIA_MAX_BYTES`(bytes)。pydantic 校验 `ge=0`。`EditableSettings`
  用 `media_max_mb: int`(UI 友好)+ `settings_to_pairs` 写 bytes。
- **测试** — `tests/test_media_downloader.py` 9 个新用例覆盖:成功路径、
  file_id 缺失、known-size 拦截、`max_bytes=0` 无限制、download 失败、
  unknown-size hard cap、make_key 稳定性、ObjectMeta size 透传。

### 🐛 Fixed (2026-08-02)
- **`AppService._subscribed` in-memory cache 漂移三连**(commits `8f660b3`
  分析 + `4d42349` 修)— 详见 `docs/SUBSCRIBED_DRIFT_ANALYSIS.md`:
  - **#A unsubscribe_channel 静默失败**:之前 `set_channel_subscribed(False)`
    抛错被 `log.exception` 吞后仍 emit `ChannelUnsubscribed`,UI 移走视觉
    元素但 storage 持久记录还在,下次 reload 频道被「恢复订阅」。现在
    让 storage 异常直接 raise,`run_coro` 走统一 ErrorOccurred 路径,UI
    看到真失败。
  - **#B list_messages(None) 真理不一致**:fallback 原本走 cache,跟
    `list_subscribed_channels()`(走 storage)双真理并存。改为直接
    `await self.storage.list_subscribed_channels()`。
  - **#C reconfigure 全频道自动订阅**(隐性炸雷):切 `db_backend` 时
    `self._subscribed = (new_db_ids | self._subscribed)` 其中
    `new_db_ids = list_channels()`(全频道)→ 一次切 db_backend 后
    所有持久化但未订阅的旧频道被错误标记为「已订」。
  - **整体收尾**:删 `AppService._subscribed` 字段(5 use site + 字段定义
    全清),真理 = storage。`test_no_subscribed_attribute_remains` 防
    回滚 guard,任何人重新加回字段都立刻被抓。232 测试全过。

### 🔧 Changed (2026-07-31 refactor sweep)
- **`TdlibTelegramClient._check_authentication_code` / `_check_authentication_password`
  抽 `_submit_auth_step` helper**(commit `f655c19`)— 22 行
  near-identical 方法合并成 9 行 + 25 行 helper;3 处重复的
  `getattr(e, "message", None) or str(e) or "未知错误"` 统一进
  `_extract_error_detail` 模块级纯函数(`tests/test_error_helpers.py`
  8 个新 case)。
- **`ChannelSyncService.sync_channels` 142 行拆 4 阶段 helper**
  (2026-08-02,commit `25da924`)— REVIEW M1 显式列出的高 ROI 长函数;
  拆成 3 个职责单一的 helper(`_sync_one_channel` 组合 + 异常 /
  `_sync_metadata` 单频道拉元数据 / `_sync_history` 单频道拉历史 +
  分页 + throttle),`sync_channels` 退化为 ~32 行 orchestrator(只负责
  循环 + 累加 + 顶部取消 + 末尾 done 事件)。`ChannelSyncResult` 加
  `new_messages_added: int = 0` 字段(dataclass 默认 0,向后兼容),
  `_sync_one_channel` 返回 `(ch_result, rate_limited_seconds)` 元组,
  `retry_after` 透传到 outer `SyncResult.rate_limited_seconds`。`tests/
  test_channel_sync.py` 14 个用例行为锚点全过,236 tests total green。
- **`jsonl_store._ChannelFile` 抽到独立文件 `channel_file.py`**
  (2026-08-02,commit `d748121`)— 单频道 jsonl 文件视图(内存索引 +
  行级锁 + flush)从 428 行 `jsonl_store.py` 抽到 96 行 `channel_file.py`,
  命名从 module-private `_ChannelFile` 升到 public `ChannelFile`(虽
  当前仅 jsonl_store 用,但命名暗示复用空间)。`jsonl_store.py` 从 428 →
  361 行(-67),职责更清晰:文件级常量 + DTO ↔ dict 转换 + Repository
  调度。31 个相关测试全过,jsonl + map_message + reconfigure 三块零
  回归。
- **启动失败用 Qt `QMessageBox.critical` 弹窗**(commit `4fa866d`)—
  原先 bundle 启动失败只 stderr 静默退出,新用户无任何提示(Windows /
  macOS `.app` 双击 → 一闪而过)。`app.py` module-level
  `_show_setup_failure_dialog(err)` 含 PySide6 import / QApplication
  instance / 内 raise 三层防御性 fallback;`tests/test_app_failure_dialog.py`
  4 个新 case(空 message / 无 Qt app / PySide6 模拟 import 失败)。
- **`state_labels.py` 单源映射**(commit `43ebab5`)— login state 到
  `(dot, label, hint, badge)` 的映射从 3 处分散(`main_window` 22 行双 dict
  + dashboard 内联 3 状态 dict + dashboard L344-353 if/elif chain,后两个
  还互不一致)统一到 `state_labels.py` 4 张表 + 4 helper。新增状态时,
  只改一处。dashboard `_format_*` + dispatch table(`_init_event_dispatch`)
  把 41 行 isinstance 链拆成 7 个小方法,加新事件类型只需注册一行。
- **`list_joined_channels` 抽 `_iter_resolved_chats` async generator**
  (commit `85f7ed1`)— 73 行方法拆成 lifecycle guard 30 行 +
  per-cid 迭代 20 行(mid-loop `_check_alive` + 单条解析失败 + `None`
  跳过 + 进度日志全在 generator 里)。`tests/test_telegram_lifecycle.py`
  加 4 个 case 覆盖 empty / all succeed / 单条 raise / mid-loop
  `ClientClosingError` 4 路径。
- **`start()` error-code 翻译 → `_translate_boot_error` 纯函数**
  (commit `a6d6b9e`)— start() TimeoutError 分支内 13 行 if/elif chain
  抽 module-level pure function(401 → encryption key 不匹配 +
  AppService 据此 rotate key / 429 → TDLib 限流 / 其他 → DC 握手失败 /
  空 → 启动超时)。`tests/test_error_helpers.py` 加 5 个 case 覆盖四
  分支 + 优先级(401 over 429)。
- **`empty_hint(icon, title, hint)` helper + 首屏引导**(commit `4691d27`)—
  抽 MessageDetail 同款「icon + title + hint」占位为 form_row helper,
  MessageView(LIVE tab)+ ChannelWidget.lst_joined 两处接入:首启打开
  LIVE 看到「💬 暂无消息 — 先去「频道」页双击订阅一个频道」;
  Channels 卡片空时显示「📋 暂无已加入频道 — 请先登录后点「刷新」」。
  tests/test_form_row.py + test_message_view.py + test_main_window_channels.py
  共 +9 个新 case。
- **`channel_widget` 双栏同构 → `_ChannelListCard` 内部类**
  (commit `f30f07f`)— `_build()` 里两张 ~30 行复制粘贴的卡(已加入 +
  已监听)抽成可复用 widget class,只在 `__init__` keyword arg 里区分
  title / action 按钮 / 多选 / empty_hint 存在性。`set_items` /
  `add_item` / `remove_by_cid` / `apply_filter` / `selected_cids` 统一
  API,filter 逻辑(原来在 ChannelWidget 内联 for loop)搬到 card 里只写
  一遍。ChannelWidget 旧 public attribute 名(`lst_joined` 等)用引用
  桥接保留,MainWindow + tests 不需改一行。8 个 _ChannelListCard 独立
  case(set_items / clear / add/remove / filter / signals / 模式切 / 
  empty_hint 切换)。
- **`message_detail.py` 6 处 inline `setStyleSheet` → objectName + QSS**
  (commit `34d5c6b`)— `#detailHeader` / `#detailSectionLabel` /
  `#detailTextEdit` / `#mediaItem` / `#rawJsonEdit` / `QLabel#emptyHintIcon`
  6 条 selector 在 `style.qss` + `style_dark.qss` 各一份,主题切换后
  详情面板样式跟得上(原来浅色 hex `#fafbfc` / `#e2e4e9` 写死在 Python
  里,暗色主题下样式全断)。
  `form_row.empty_hint` 同样把 `font-size: 36px` 切到 `QLabel#emptyHintIcon`,
  跟 message_detail 占位共用同一 selector。
  tests/test_message_detail_theme.py 9 个新 case(objectName 命中 +
  widget tree 无 leaked inline styleSheet + QSS 必有 selector)。
- **删 `iter_messages` Protocol + tdlib_client stub + fake_client impl** —
  grep 0 caller,纯冗余;tdlib 真正的历史接口是 `iter_chat_history`
  (`ChannelSyncService` 在用)。
- **删 `login(phone)` Protocol 方法** — 旧版鉴权入口,新代码走
  `submit_phone` + `submit_code`;`FakeTelegramClient.login` 仍保留作
  内部转发(`submit_phone` proxy)。

### 🐛 Fixed
- **`iter_chat_history` Protocol 契约陷阱**(2026-07-30,commit `4e8aa6d`)
  — 之前文档说 `from_msg_id > 0` 是"从该 id 之后正向拉",但 TDLib
  `GetChatHistory.from_message_id` **只能向旧方向拉**(id 递减)。重构让
  文档跟实现一致:参数 `from_msg_id` → `before_msg_id`,Protocol docstring
  明确"截止这条之前的更早消息"是唯一方向;需要"最新消息"走
  `subscribe_updates()` 实时流。零运行时破坏(Protocol 是 typing-only,
  4 处 rename 闭环:Protocol + 实现 + fake + channel_sync 调用方)。
- **UpdateStream 长会话内存泄漏**(2026-07-30,commit `4e8aa6d`)—
  `TdlibTelegramClient._streams` 之前只在 `close()` 全量清空,
  `subscribe_updates()` 后调 `stream.aclose()` 不会从列表移除,长会话
  只增不减。修复:`_AiotdlibUpdateStream` / `FakeUpdateStream` 加
  `on_close` callback,`aclose()` 触发时自动从 `_streams` 移除自己。
  Protocol 文档补:`aclose()` 是契约,必须调一次。
- **REVIEW M2.1**:FULL 模式下用户之前**下不到任何原文件** — `MediaDownloader.download_one`
  永远返 None,只是元数据 + 缩略图 + 一个空 key。现在真实现,完整下载链路打通。

## [0.5.0] - 2026-07-22

🧹 **Stage A+B + post-Phase-5 UI polish + REVIEW sweep** — 一次性把测试
collection fragility / CI 升级 / UI 视觉 / 早期 review 残留 合并发布
(后续被 v1.0.0 取代,这里保留历史 changelog)。

### ✨ Added
- **SOCKS5 代理** — `Settings.proxy` + `TD_PROXY` 环境变量,`AiClient(proxy_settings=...)` 接入;
  `EditableSettings` 校验 `socks5://[user:pass@]host:port` 格式;设置对话框里有「测试连接」按钮
- **侧栏常驻账户面板** — `AccountWidget`:API ID / Hash / 手机号就地编辑 + 保存;
  状态圆点(红/橙/绿);登录动作(登录 / 提交验证码 / 提交 2FA)**就地切换输入框**,不再弹模态
- **侧栏频道双栏** — `ChannelWidget`:已加入 + 已监听,双击切换订阅;
  实时按 `ChannelSubscribed / ChannelUnsubscribed` 事件刷新
- **应用图标** — SVG(蓝底 + 信号波 + 频道点),`setApplicationIcon` + 窗口标题;工具栏 4 图标独立 SVG;
  pyproject 不增依赖,资源走 `importlib.resources`
- **QSS 主题** — `ui/resources/style.qss`:状态点颜色 / 工具栏分组 / 圆角 group box /
  提示/警告/错误三色 role
- **重新组织主窗口**:
  - 工具栏只保留 `刷新频道 · 导出 · 设置`(无「登录」了,已上移侧栏)
  - 状态栏显示「登录状态」实时事件
- **测试** — 25 个新单测(proxy URL 解析 12 + 校验 6 + settings/store 往返 5 + TdlibClient 集成 2)
- **REVIEW.md**(new)— 一次 sweep 的 review report,列出分层违规、dead-code、CHANGELOG 重复标题、重复 setWindowIcon、coverage 配置缺失等
- **Stage A+B CI 基建**:
  - `tests/__init__.py` — 把 `tests/` 标成 Python package,修 `from tests.conftest import …` 风格的 fragility
  - `actions/upload-artifact@v4` → `@v5` — Node 20 deprecation 警告全清
  - `.github/dependabot.yml` — uv ecosystem 周一 09:00 扫 `uv.lock` 开自动 PR
  - `.github/workflows/audit.yml` — 周一 09:30 UTC 跑 `pip-audit --strict`,不挡主 CI
- **覆盖率上传** — `pyproject.toml` 加 `[tool.coverage.run]` / `[tool.coverage.report]`,CI 加 coverage xml artifact 上传;**不设阈值**
- **项目 URL 元数据** — `pyproject.toml` 加 `[project.urls]`(Homepage / Repository / Issues / Documentation)

### 🔧 Changed
- **tdlib_client 0.27+ 兼容** — `core/telegram/tdlib_client.py` 重写为 `aiotdlib.ClientSettings(...)` 调用,
  同时支持老版直接 kwargs 调用;`core/telegram/factory.py`/`client.py` 等接口未变;边界未变
- **settings 对话框重构** — `ui/widgets/settings_dialog.py` 删 Telegram 整组,加 Proxy + 测试连接按钮
- **login_dialog 收尾** — 只剩 code + 2FA 输入(auto-show via bus event)
- **CI matrix 移除 `windows-latest`** — upstream `aiotdlib 0.27.x` 在 PyPI 上不发 Windows wheel
  (只 `macosx_*` + `manylinux_2_28_*` 四个),`uv sync` 在 Windows 上会触发 TDLib sdist 编译,
  需 MSVC + OpenSSL + gperf + PHP,每次必失败。源码层跨平台(全 `pathlib` / 无 POSIX-only 假设),
  但 CI 不再验证 Windows,跟实际能力对齐。README 新增「🖥️ Platform Support」章节。
- **公共属性化** — `MonitorService.subscribed_ids` 公共属性,UI 不再读 `_whitelist` private 字段;
  移除三处 `# type: ignore[attr-defined]`
- **`_set_state` 清理** — `tdlib_client._set_state` 去除历史 dead-switch `if False else asyncio.create_task(...)`,改纯 `create_task`
- **图标统一 Lucide** — stroke-width=1.75, currentColor, round caps;新增 3 个 kind 图标
  (megaphone / users / user-round);删 orphan SVG;`channel_widget.py` 删 `_paint_color_block` /
  `_kind_color` / `_ICON_*` ~30 行 QPainter 色块代码,改用 `action_icon("kind_channel|supergroup|group")`
- **MainWindow 图标** — 不再单独 `setWindowIcon(load_app_icon())` — `QGuiApplication.setWindowIcon` 已是 process-wide
- **uv 工作流** — README / CONTRIBUTING / SECURITY 全切 uv,`pip install` 路径示例全部替换为
  `uv sync` / `uv run`,跟 CI 一致;README「测试覆盖」表按 `pytest --collect-only` 实际跑出
  的 151 用例补全(从老的 4 行 + 错的 20 用例 → 17 行 + 正确 151);SECURITY.md 受支持版本
  `0.1.x` → `0.2.x`;REVIEW.md 和 `settings_page.py` docstring 残留的 `settings_dialog.py`
  文件名引用改回新名(`settings_page.py`)
- **datetime UTC aware** — `datetime.utcnow()` / `utcfromtimestamp()` 全切 aware UTC
  (`datetime.now(UTC)` / `fromtimestamp(ts, UTC)`),11 处调用点 + `tests/conftest.py` 修一个
  latent 排序 bug;CI actions 升 major(`actions/checkout@v4`→`v5`、`astral-sh/setup-uv@v6`→`v7`)
  避开 Node 20 deprecation;`pyproject.toml` 版本 `0.1.0` → `0.2.0` 跟 README / SECURITY 对齐

### 🐛 Fixed
- **qasync `RuntimeError: loop ... is not the running loop`** (2026-07-18 08:00 实测) —
  `app.run()` 原本的 `loop.run_until_complete(_setup_async)` + `loop.run_forever()` 模式
  在两步之间留一个 qasync `__is_running=False` 但 `closed=False` 的 paused 窗口;
  aiotdlib 内部 IO thread 在这段时间 wake asyncio Task 时,`Task.__step()` 检查失败抛
  `RuntimeError`。改成单 `run_forever()` + `asyncio.ensure_future(_setup_then_show(), loop=loop)`,
  loop 始终 running,根因消除
- **`list_joined_channels` 启动 race** — `bootstrap_ui` 在 `app.bootstrap()` 之前 fire-and-forget
  拉已订阅频道列表,bridge `_state!="ready"`,撞 aiotdlib 10s `request_timeout`;新增 entry guard
  `if self._state != "ready`: 静默返回 [],DEBUG 日志
- **logged-in 下频道 panel 不显示** (2026-07-18 用户反馈) — 之前 `if state != "ready" → 立即 []`
  在 aiotdlib 走 `WaitTdlibParameters → ... → Ready` 多步过渡时太激进:
  `start()` await 的 `_state_event.wait()` 任何变化都 set,所以可能在
  WaitTdlibParameters 就返;`bootstrap_ui` 紧接着 fire-and-forget 调
  `list_joined_channels` 时 `_state` 还是中间态,guard 立即 [],**错过稍后才到的
  Ready,channels 永不显示**直到用户手动 refresh。改成**最多等 8 秒**让
  `_state` 走到 `ready` 再真请求;仍 best-effort,超时 / `_closing` / 永久
  非 ready 时返 `[]` + DEBUG log
- **`_wait_for_state` spin 冻 UI** (2026-07-18 17:17 用户反馈"卡住无反应") —
  `_state_event` 是 Python `Event` 语义(set-only),之前 polling 路径
  `wait_for(state_event.wait(), 0.5)` 在 event 已 set 时立即返回,**没真正
  yield CPU**;qasync 的 loop 被这个子循环 peg 满,Qt 事件 8s 全没机会 pump,
  UI 完全不响应。改成 event 已 set 时主动 `asyncio.sleep(0.05)` 让出 CPU +
  重新 poll `self._state` —— 是等"状态变化"而非"event set",两者在 set-only
  Event 下不相等
- **`list_joined_channels` close race** — `close()` 标志 + 事务性方法(`submit_phone/code/password/logout/start/get_channel_metadata/join_channel`/`iter_chat_history` 分页入口)的 `_check_alive()` 公共 entry,提前抛 `ClientClosingError`,不进 10s aiotdlib request
- **左侧 nav icon 显示不全** — `icon.py` 加 `tinted_action_icon(name, color)`,在 SVG 字节层
  把 `currentColor` 替换为 `QColor.name()`(Qt `QSvgRenderer` 不解析 `currentColor`,
  所以过去所有 nav / 频道类型图标在 painter 上都是黑团);nav / 顶栏 / ChannelWidget 全切
  到 tinted 入口
- **nav `nav_channels.svg` 错位** — 之前是 Lucide 风格的"火箭/纸飞机"(几何时序误拼),
  跟"频道管理"语义不符;改为 Lucide `list` 风格(3 个 dot + 3 条 line)
- **nav `nav_live.svg` 拥挤** — 4 道弧 + 中心点 24px 下糊;减为 2 道大弧 + 中心点
- **nav hover/active 颜色太深** — dark 模式 active `#2a2a3e` vs hover `#252540` 仅差 2 个
  hex step(肉眼难分);light 模式 hover `#16162a` 比 active `#1e1e2e` 还暗(affordance 反向)。
  重排色阶:dark `idle transparent → hover #2c2c45 → active #3a3a55`,light `idle → hover
  #2a2a40 → active #1e1e2e`,active 永远比 hover 亮 1 阶;inactive fg 提到 `#b0b5c8`
  (WCAG AA 5.8:1,过)
- **active 选中态不明显** — 在 active 时叠 `linear-gradient(90deg, rgba({accent}, 0.18), transparent)`
  + 1px accent glow 描边,跟非 active 的纯 bg 拉开视觉差距
- **nav 顶部 Unicode `●` logo 删除** — 跨字体渲染不一致,看着像占位;header bar 已有
  `appTitle` 文本品牌锚点,nav 不再重复
- **QListWidget 选中态对比太弱** — light 主题 `selected` bg 从 `#d6e4fa` 加深到 `#b6d0f0`
  (对比从 2 阶拉到 4 阶),加 3px accent 左边线;两主题都改
- **disabled 文字过 WCAG** — light `#b0b4c0` → `#8a8d96`,dark `#5a5d6a` → `#6e7180`
- **状态色 2014 → Tailwind v3** — ready `#5cb85c` → `#16a34a`,pending `#f0ad4e` → `#f59e0b`,
  error `#d9534f` → `#dc2626`,unset `#999999` → `#94a3b8`(语义不变,两主题共用,saturated
  状态色在两底色上均过 WCAG)
- **`ThemeManager` accent 集中** — 加 `ACCENT_LIGHT` / `ACCENT_DARK` / `ACCENT_*_HOVER` class
  attribute + `accent(kind)` 方法;QSS 走 `{accent}` / `{accentHover}` 占位符在 `apply()`
  注入,避免 `#5b9cf5` / `#4a8be4` / `#7bb4ff` 散落
- **header 按钮文字加深** — `#headerActionBtn` text `#3a3d4a` → `#5a5d64`,Refresh / Export
  图标(走 tinted 链)fg 与 button color 一致
- **app icon 重设计** — 旧 3-弧 + 中心点在 16×16 下糊成一团、绿点消失;改为「信号塔 +
  频道条」:左 1/3 塔(三角顶 + 矩形杆 + 梯形基座 + 1 道信号波),右 2/3 三条频道 list
  (顶条 highlight 绿)。16×16 下塔尖 1px 三角、绿条 4×1px、白条 4×1px — 全部 ≥ 1px 物理
  像素,taskbar / 256×256 about 都清晰

## [0.2.0] - 2026-07-13

### ✨ Added
- **零依赖文件后端** — `JsonlFileStore`:每频道一个 `.jsonl` 文件,`channels.json` 注册表;适用于单机、轻量场景
- **两级分片对象存储** — `FolderObjectStore`:按文件名两级分片(如 `media/ab/cd/...`),文件多时不慢;仍可用 FS 工具直接浏览
- **设置对话框** (`SettingsDialog`) — UI 编辑 Telegram/DB/对象存储/媒体策略;动态按 backend 显隐字段
- **`.env` 读/写** — `core/settings_store.py`:保形解析(保留注释/空行/key 顺序),含需引号自动加引号
- **热重载** — `AppService.reconfigure(new_settings)`:无重启切换 storage / objects;Telegram 凭据变更时 `SettingsChanged.needs_relogin=True` 通知 UI
- **新事件** `SettingsChanged`(what / new_settings / needs_relogin)
- **新后端枚举值** — `DBBackend.JSONL`、`ObjectStoreBackend.FOLDER`
- **新配置字段** — `db_root`(jsonl 目录)
- **测试** — 19 个新单测(jsonl 5 / folder 5 / settings_store 5 / reconfigure 4),共 39/39 通过

### 🔧 Changed
- `core/storage/factory.py` 与 `core/objectstore/factory.py` 实现类**懒加载**(早已实施,继续适用)
- `core/config.py.ensure_dirs()` 同时处理 `JSONL` / `FOLDER` 本地目录
- 工具栏新增「设置…」动作

## [0.1.0] - 2026-07-13

### ✨ Added
- 初始发布 🎉
- **架构**:UI/core 严格分离 — `AppService` 门面 + `EventBus` + DTO
- **数据库**:PostgreSQL(asyncpg)与 MongoDB(motor)两套实现,config 切换
- **对象存储**:S3 协议(aioboto3,AWS S3 / MinIO / 阿里 OSS)与本地 FS,config 切换
- **Telegram 集成**:通过 `aiotdlib` 接入官方 TDLib,业务侧只见 `TelegramClient` Protocol
- **UI**:PySide6 + qasync 主窗口、登录对话框(phone → code → 2FA → ready)、频道面板、消息流、导出对话框
- **监听**:`MonitorService` 实时订阅 / 频道白名单 / `(channel_id, telegram_msg_id)` 幂等 upsert / 错误时指数退避重连
- **导出**:JSON / CSV / Markdown / HTML 四种,HTML 可内嵌 base64 缩略图
- **测试**:20 个单测,全部离线(`FakeTelegramClient` + `InMemoryRepository` + `LocalObjectStore`)
- **质量**:`ruff` lint 0 警告(`SIM105`/`UP042`/`UP035` 等保留为项目风格)
- **文档**:`README.md` / `docs/ARCHITECTURE.md` / `CONTRIBUTING.md` / `SECURITY.md` / `CHANGELOG.md`

### 🔒 Security
- Session 文件落本地数据目录,**禁止**提交到 git(`.gitignore` 已配)
- 文档明确提示:不要把 `TG_API_ID` / `TG_API_HASH` / 验证码 / session 贴到 issue

[Unreleased]: https://github.com/forcetone/tgmonitor/compare/v1.0.7...HEAD
[1.0.6]: https://github.com/forcetone/tgmonitor/compare/v1.0.5...v1.0.6
[1.0.7]: https://github.com/forcetone/tgmonitor/compare/v1.0.6...v1.0.7
[1.0.5]: https://github.com/forcetone/tgmonitor/compare/v1.0.4...v1.0.5
[1.0.4]: https://github.com/forcetone/tgmonitor/compare/v1.0.3...v1.0.4
[1.0.3]: https://github.com/forcetone/tgmonitor/compare/v0.2.0...v1.0.3
[0.2.0]: https://github.com/forcetone/tgmonitor/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/forcetone/tgmonitor/releases/tag/v0.1.0
