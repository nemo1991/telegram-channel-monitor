# Changelog

本项目的所有显著变更都会记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
版本遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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
