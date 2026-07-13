# Changelog

本项目的所有显著变更都会记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
版本遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Planned
- 媒体原文件下载器(`MediaDownloader.download_one` 接入 `client.download_file`)
- 消息编辑 / 删除事件完整处理
- 国际化(英文 UI)
- 历史回放(`TelegramClient.iter_messages` 真正实现)

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
