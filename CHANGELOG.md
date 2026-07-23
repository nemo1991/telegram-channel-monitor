# Changelog

本项目的所有显著变更都会记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
版本遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

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

## [Unreleased]

### ✨ Added
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

### 🔧 Changed
- **删 `iter_messages` Protocol + tdlib_client stub + fake_client impl** —
  grep 0 caller,纯冗余;tdlib 真正的历史接口是 `iter_chat_history`
  (`ChannelSyncService` 在用)。
- **删 `login(phone)` Protocol 方法** — 旧版鉴权入口,新代码走
  `submit_phone` + `submit_code`;`FakeTelegramClient.login` 仍保留作
  内部转发(`submit_phone` proxy)。

### 🐛 Fixed
- **REVIEW M2.1**:FULL 模式下用户之前**下不到任何原文件** — `MediaDownloader.download_one`
  永远返 None,只是元数据 + 缩略图 + 一个空 key。现在真实现,完整下载链路打通。

### 🔧 Fixed (Stage A+B, 2026-07-22)
- **test: 加 `tests/__init__.py`** — 把 `tests/` 标成 Python package,
  修 `from tests.conftest import …` 风格的 fragility;`pytest` binary 入口
  和 IDE 单文件跑现在都能正常 collect(此前 151 测试只在 `python -m pytest`
  下能跑)。
- **ci: `actions/upload-artifact@v4` → `@v5`** — 顺手把上次 major 升级漏掉的
  action 也升了,Node 20 deprecation 警告全清。
- **ci: 加 `.github/dependabot.yml`** — uv ecosystem 周一 09:00 扫 `uv.lock`
  开自动 PR;GitHub Actions ecosystem 周一 09:30 扫 workflow 升级。
  不自动合,人工 review。
- **ci: 加 `.github/workflows/audit.yml`** — 周一 09:30 UTC 跑 `pip-audit --strict`,
  基于 `uv.lock` 扫 CVE,不挡主 CI,失败即通知。

### 📝 Docs
- **docs: README / CONTRIBUTING / SECURITY 全切 uv 工作流** — pip 路径
  示例全部替换为 `uv sync` / `uv run`,跟 CI 一致;README「测试覆盖」
  表按 `pytest --collect-only` 实际跑出的 151 用例补全(从老的 4 行 +
  错的 20 用例 → 17 行 + 正确 151);SECURITY.md 受支持版本 `0.1.x` → `0.2.x`;
  REVIEW.md 和 `settings_page.py` docstring 残留的 `settings_dialog.py`
  文件名引用改回新名(`settings_page.py`)。
- **chore: batched quick wins** — `datetime.utcnow()` / `utcfromtimestamp()`
  全切 aware UTC(`datetime.now(UTC)` / `fromtimestamp(ts, UTC)`),11 处
  调用点 + `tests/conftest.py` 修一个 latent 排序 bug;CI actions 升 major
  (`actions/checkout@v4`→`v5`、`astral-sh/setup-uv@v6`→`v7`)避开 Node 20
  deprecation;`pyproject.toml` 版本 `0.1.0` → `0.2.0` 跟 README / SECURITY
  对齐。

### 🛠 Changed
- **CI matrix 移除 `windows-latest`** — upstream `aiotdlib 0.27.x` 在 PyPI 上不发
  Windows wheel(只 `macosx_*` + `manylinux_2_28_*` 四个),`uv sync` 在 Windows 上
  会触发 TDLib sdist 编译,需 MSVC + OpenSSL + gperf + PHP,`windows-latest` runner
  默认不带这套工具链,每次必失败。源码层跨平台(全 `pathlib` / 无 POSIX-only 假设),
  但 CI 不再验证 Windows,跟实际能力对齐。README 新增「🖥️ Platform Support」
  章节,说明 Linux / macOS 由 CI 验证,Windows 推荐用 WSL2,原生编译需自备
  MSVC 工具链。

### 🔧 Fixed (post-Phase-5 UI polish, 2026-07-21)
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

### ✨ Added

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
- **文档** — README 跑通说明改写,标签侧栏 + 代理 + 图标

### 🔧 Changed
- `core/telegram/tdlib_client.py` 重写为 `aiotdlib.ClientSettings(...)` 调用(0.27+ 兼容),
  同时支持老版直接 kwargs 调用
- `core/telegram/factory.py`/`client.py` 等接口未变;边界未变
- `ui/widgets/settings_dialog.py` 删 Telegram 整组,加 Proxy + 测试连接按钮
- `ui/widgets/login_dialog.py` 收尾只剩 code + 2FA 输入(auto-show via bus event)

### 🔧 Changed
- `REVIEW.md`(new)— 一次 sweep 的 review report,列出分层违规、dead-code、CHANGELOG 重复标题、重复 setWindowIcon、coverage 配置缺失等
- `MonitorService.subscribed_ids` 公共属性 — UI 不再读 `_whitelist` private 字段;移除三处 `# type: ignore[attr-defined]`
- `tdlib_client._set_state` 去除历史 dead-switch `if False else asyncio.create_task(...)`,改纯 `create_task`
- `MainWindow` 不再单独 `setWindowIcon(load_app_icon())` — `QGuiApplication.setWindowIcon` 已是 process-wide
- `pyproject.toml` 加 `[tool.coverage.run]` / `[tool.coverage.report]`,CI 加 coverage xml artifact 上传;**不设阈值**
- `pyproject.toml` 加 `[project.urls]`(Homepage / Repository / Issues / Documentation)
- 图标统一到 Lucide(stroke-width=1.75, currentColor, round caps);新增 3 个 kind 图标(megaphone / users / user-round);删 orphan SVG
- `channel_widget.py` 删 `_paint_color_block` / `_kind_color` / `_ICON_*` ~30 行 QPainter 色块代码,改用 `action_icon("kind_channel|supergroup|group")`

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

[Unreleased]: https://github.com/forcetone/tgmonitor/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/forcetone/tgmonitor/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/forcetone/tgmonitor/releases/tag/v0.1.0
