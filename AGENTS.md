# AGENTS.md — tgmonitor 项目指南

> 本文件为 AI agent 与人类开发者快速上手而写。内容基于仓库现有代码与文档
> (README、pyproject.toml、docs/ARCHITECTURE.md、CONTRIBUTING.md、.github/ workflows)
> 归纳,改代码时如与本文件矛盾,以实际代码为准并顺手更新本文件。

## 项目概述

**tgmonitor** — Telegram 频道监听桌面应用:监听白名单频道的新消息,自动落库并下载媒体,提供桌面 UI 查看、检索与导出。

- Python `~=3.13.0`,GUI 用 **PySide6**,异步用 **qasync**(Qt 主线程即 asyncio 事件循环),Telegram 协议走 **Telethon**(纯 Python MTProto,`cryptg` 加速加密、`python-socks` 走 SOCKS5),单文件启动。
- 当前版本 **2.0.0**(以 `pyproject.toml` 为准)。
- 包管理/构建:uv + hatchling,src 布局,console script 入口 `tgmonitor = tgmonitor.__main__:main`。
- 桌面打包:PyInstaller(`tgmonitor.spec`)→ Linux AppImage + macOS .app + Windows onedir zip;发布走 GitHub Release + SHA256SUMS。**纯 Python 后端,三平台均无需编译原生引擎**。
- 许可证 MIT,作者 forcetone;项目注释与文档均用中文,新代码注释随项目习惯用中文。

> **v2.0.0 重大变化**(相对 v1.x):TDLib(自编译 libtdjson + `tdlib_json` 子项目)→ **Telethon**;
> 默认数据库 PostgreSQL → **SQLite**(WAL),**MongoDB 后端删除**;745 行 god facade → **按域服务**;
> MonitorService 5 mixin → **显式组合**;CI 去掉 libtdjson 编译 / warm-cache / vcpkg,纯 `uv sync` + PyInstaller。
> session 格式不兼容,升级需重新登录。

## 技术栈与运行架构

### 依赖(pyproject `[project]`)

| 依赖 | 版本 | 用途 |
|---|---|---|
| PySide6 | >=6.6 | Qt GUI |
| qasync | >=0.27 | Qt 事件循环接 asyncio |
| telethon | >=1.45.0 | 纯 Python MTProto Telegram 后端 |
| cryptg | >=0.6.1 | Telethon 加密加速(C 扩展,可选但默认装) |
| python-socks[asyncio] | >=3.2.1 | SOCKS5 代理 |
| pydantic-settings | >=2.2 | 配置加载(`TG_` 前缀) |
| Jinja2 | >=3.1 | HTML 导出模板 |
| platformdirs | >=4.11.0 | 平台数据目录 |
| aiosqlite | >=0.22.1 | 默认 SQLite 后端(WAL) |

可选依赖组:`postgres`(asyncpg)、`objectstore`(aioboto3,配 S3)、`all`。**无 mongo**。
dev 组:pytest、pytest-asyncio、ruff、mypy>=2.3.0、coverage、pyinstaller>=6.21.0、moto[s3]>=5、pre-commit>=3.7、testcontainers[postgres]>=4.8、hypothesis>=6.100、pytest-timeout>=2.3。

### 分层与边界(最重要的架构约束)

```
UI(PySide6 widgets/viewmodels)
  │  只依赖:AppService 组合根 + 按域服务 + EventBus + DTO
  ▼
core(AppService 组合根/生命周期 + 领域服务)
  │  只依赖:StorageRepository / ObjectStore / TelegramClient / Exporter 四套抽象
  ▼
存储(sqlite_repo / postgres_repo) · 对象存储(local/folder/s3) · 导出(json/csv/markdown/html)
```

- **`core/` 禁止 import PySide6 / qasync** — 保持领域逻辑与 UI 框架解耦,可离线单测。
- **跨边界只传 DTO**(`dto.py` 里的 ChannelDTO / MediaDTO / MessageDTO / ExportRequest / ExportResult / SyncOptions / SyncResult 等),不传 Telethon 对象、ORM 对象、Qt 对象。
- 四套可插拔抽象:`StorageRepository`(7 个按域 ABC 组合)、`ObjectStore`(ABC)、`TelegramClient`(runtime_checkable Protocol)、`Exporter`(ABC + 注册表)。新增实现见下文「扩展点」。
- **AppService 不再是 god facade**:只保留生命周期 / 热重载 / 导出 + 后端持有;域操作 UI 直调 `app.auth/channels/messages/media/batch/channel_sync`。公开表面由 `tests/test_app_service_surface.py` allowlist 锁死,新增域转发会被守护测试拒绝。
- UI 跨线程调 coroutine 必须走 `ui/async_bridge.py` 的 `run_coro(loop, coro, *, on_success, on_error, error_label)`,不要自己套 `asyncio.run_coroutine_threadsafe` + try/except 模板。

### 核心模块一览(`src/tgmonitor/`)

| 路径 | 职责 |
|---|---|
| `__main__.py` / `app.py` | 入口。`run()` 是 GUI 组合根:构建 qasync `QEventLoop` 后**单 loop `run_forever` + `ensure_future`**(历史坑:不要用 `run_until_complete`);凭据未配置时 factory 返回占位 client,应用正常启动显示未登录引导;真 client 构造失败弹 QMessageBox;退出码 0/1/130。`__main__.main()` argv 分流:无参数 → `app.run()`;`sync` / `monitor` / `--help` → `cli.main()` |
| `core/runtime.py` | **CLI / GUI 共享 composition root**:`bootstrap()` 与 `shutdown()`(配套清理),`core/` 内禁止 import PySide6 / qasync |
| `core/config.py` | pydantic-settings 配置,`TG_` 环境变量前缀;`DBBackend` SQLITE(默认)/ POSTGRES |
| `core/events.py` | EventBus:async pub/sub,订阅者异常被吞掉(不崩主流程)。领域事件如 LoginStateChanged / ConnectionStateChanged / ChannelSubscribed / MessageReceived / MessageEdited / MessageDeleted / MediaDownloaded / ExportDone / BatchDone / ChannelSyncProgress / ErrorOccurred / AuthErrorOccurred / SettingsChanged |
| `core/dto.py` | 跨边界数据传输对象 |
| `core/app_service/` | **组合根 + 生命周期**(app_service.py + _lifecycle / _reconfigure / _backend_sync)。装配域服务,不再是 god facade |
| `core/auth_service.py` | AuthService:登录/登出与登录状态管理(`app.auth`) |
| `core/channel_service.py` | ChannelService:订阅 / 退订 / 白名单(`app.channels`) |
| `core/message_service.py` | MessageService:消息查询 / 收藏 / 标签 / 备注 / 单删(`app.messages`) |
| `core/media_service/` | MediaService:媒体列表 / CRUD / 打开 / retry / reconcile(`app.media`) |
| `core/batch_service.py` | BatchService:7 批量 op + 取消,自持 cancel event(`app.batch`) |
| `core/channel_sync/` | ChannelSyncService:手动全量同步,`chat_delay_ms` / `page_delay_ms` 节流,断点续拉(`app.channel_sync`) |
| `core/settings_store.py` | `.env` 保形读写(`.part` + rename 原子写),`EditableSettings` 供 UI |
| `core/telegram/` | Telethon 封装:`client`(Protocol)/ `telethon_client`(TelethonTelegramClient)/ `telethon_messages`(归一化)/ `proxy`(python-socks)/ `errors` / `factory`;`unconfigured.py` 是凭据缺失时 factory 返回的占位 client(启动不崩);`fake_client.py` 是测试用假客户端 |
| `core/monitor/` | MonitorService(**显式组合**,非 mixin):`service` + `orchestrator`(主循环 + 退避重连 1s→30s)/ `whitelist` / `event_handlers` / `downloader`(MediaDownloader 单文件 200MB 上限)/ `backfill` / `delete` |
| `core/storage/` | 7 个按域 ABC(SchemaLifecycle / ChannelStore / MetaStore / MessageStore / MediaStore / ThumbnailStore / UserMetaStore)组合成 `StorageRepository`;`sqlite_repo`(默认,aiosqlite WAL)/ `postgres_repo`(asyncpg,+ `_*_pg` 分域 helper)+ 懒加载 factory;唯一键 `(channel_id, telegram_msg_id)`;media 只存引用不存二进制 |
| `core/objectstore/` | base ABC + local_store(平铺)/ folder_store(两级分片)/ s3_store + factory,默认 **FOLDER** |
| `core/export/` | base 注册表 `EXPORTERS` + `@exporter` 装饰器 + json/csv/markdown/html + 流式导出 service |
| `cli/` | argparse 子命令(sync / monitor);裸 `asyncio.run()` 事件循环,无 PySide6。CLI 路径在 `__main__.py` argv 分流后调,永不 import `app` |
| `ui/` | PySide6 界面:main_window、widgets/、viewmodels/、theme.py、icon.py、nav_bar、ui_assembler;`async_bridge.py` 为跨线程样板(`run_coro`) |

## 目录结构

```
├── src/tgmonitor/              # 全部源码(src 布局,见上表)
├── tests/                      # pytest 测试(1604 用例);conftest.py + fixtures/ 提供公共 fixtures
│   ├── fixtures/               # settings/storage/objectstore/bus/client/monitor/app/make_* 等
│   └── integration/            # 真 PG 集成(testcontainers,CI-only,默认 skip)
├── scripts/build_appimage.sh   # Linux AppImage 构建(唯一脚本;无 libtdjson 编译)
├── tgmonitor.spec              # PyInstaller 打包配置(纯 Python,binaries=[])
├── docs/ARCHITECTURE.md        # 架构详解
├── .github/workflows/          # ci / build / audit
└── pyproject.toml              # 构建 + 工具配置(ruff/mypy/pytest 全在这里)
```

> v2.0.0 已删除 `packages/tdlib_json/` 子项目与 `scripts/build_libtdjson.{sh,ps1}`。

## 常用命令

> 运行/开发所需工具(macOS 已全部就位):
> - **uv**(必须,包管理 + 自动下载 Python;CI 同步)
> - **git**(克隆/提交)
> - **Python 3.13** — 由 uv 自动装(`uv python list`),不依赖系统 Python
> - **无需任何原生编译工具链**(Telethon 纯 Python;cryptg 有 wheel)
> - macOS 跑 PySide6 无需额外系统库(wheel 自带);Linux 打包才需要 appimagetool / rsvg-convert
> - 可选 DB/S3 后端(extra)非运行必需;不装也能用默认 SQLite + Folder 存储

```bash
# 安装依赖(dev 全量 + 可选 DB/S3 后端,与 CONTRIBUTING.md 一致)
uv sync --all-groups --all-extras

# 运行应用
uv run tgmonitor

# 跑全部测试(核心单测全离线;UI 测试需要 offscreen 平台)
PYTHONPATH=src uv run pytest -v
PYTHONPATH=src QT_QPA_PLATFORM=offscreen uv run pytest -v   # 含 UI 测试

# 只跑某文件 / 某个测试
PYTHONPATH=src uv run pytest tests/test_channel_sync.py -v -k <名字>

# lint / typecheck
uv tool run --from "ruff>=0.5" ruff check src tests
uv tool run --from "ruff>=0.5" ruff format --check src tests
uv run mypy src/tgmonitor          # 本地全量;CI 按 8 入口矩阵逐个跑(见下)

# 覆盖率(有分支覆盖,但 CI 不设阈值)
PYTHONPATH=src uv run pytest --cov=tgmonitor

# 打包
uv run pyinstaller --clean -y tgmonitor.spec   # 产物在 dist/
bash scripts/build_appimage.sh                 # Linux AppImage(仅 Linux)
```

> 全套测试约 140~150s(含一个注入 60s rate-limit sleep 的 channel_sync 用例),
> 用 Bash 跑时 timeout 设 ≥ 600000ms,别用默认 120s(会被墙钟杀掉,误判 hang)。

## 代码风格与约定(详见 CONTRIBUTING.md)

- 每个模块文件第一行 `from __future__ import annotations`;公共方法全量 type hints。
- 语法按 Python 3.10+ 写:`dataclass(slots=True)`、`str | None` 联合类型等。
- 函数第一行是 docstring(中文);`core/` 内日志用 `logging` 模块,不要 `print`。
- **ruff**:line-length 100,`select=["E","F","I","B","UP","N","SIM","ASYNC"]`,ignore E501/UP042/UP035/SIM105/N818/B008,零容忍(CI 不通过即报错)。
- **mypy**:低严格度(`disallow_untyped_defs=false`),第三方模块按 overrides 忽略(telethon / python_socks / PySide6 / qasync / pydantic 等);PySide6 的 stub 噪声(扁平 enum 别名 / form helper 返回 QWidget)用文件头 `# mypy: disable-error-code="attr-defined"` 屏蔽。CI mypy 8 入口矩阵:`core/monitor`、`core/storage`、`core/export`、`core/objectstore`、`core/telegram`、`ui`、`app.py`、`src`(× ubuntu/macos)。
- **i18n**:翻译源是 `src/tgmonitor/i18n/*.ts`,`.qm` 是 `pyside6-lrelease` 编译产物(**gitignore**,CI 用 `compile-qt-translations` composite action 现编)。**严禁跑全量 `pyside6-lupdate`**(会破坏运行时翻译),只做外科式 `.ts` 编辑。
- commit 格式:`type(scope): subject`,type ∈ feat/fix/docs/refactor/test/chore/perf。
- 编码约定:4 空格缩进、LF 行尾、100 列上限(.editorconfig)。

## 测试策略

- **离线原则**:`core/` 单测不碰网络/真实 Telegram/真实数据库,全靠 fake — `FakeTelegramClient`、`InMemoryRepository`、`LocalObjectStore`。v2.0.0 起**无 `stub_tdlib_init`**(TDLib 已删,Telethon 测试直接 mock)。
- pytest-asyncio 全局 `asyncio_mode="auto"`,`addopts="-ra -q -m 'not integration'"`,`timeout=300` / `timeout_method="thread"`(配置在 pyproject;防 CI fixture hang)。
- `tests/fixtures/` 常用 fixtures:`settings`(stub 配置)、`storage`(InMemoryRepository)、`objectstore`、`bus`、`client`(FakeTelegramClient)、`monitor`、`app`、`make_message` / `make_photo` 工厂。`Settings.for_test()` 默认 SQLite + METADATA + LOCAL,离线可跑。
- **UI 测试必须 `QT_QPA_PLATFORM=offscreen`**(无头环境跑 Qt;Windows CI 用原生 `windows` 插件);测试系统库依赖见 CI workflow。
- 集成测试(`tests/integration/`,真 PG via testcontainers)默认 skip,CI 用 `-m integration` 显式跑;本地无 Docker 自动 skip。
- 新增领域逻辑尽量走纯 core 单测(offline),不靠 UI 测试覆盖。

## CI/CD 与发布流程(`.github/workflows/`)

- **ci.yml**(push/PR → main):test 矩阵(ubuntu + macOS + windows × 3.13)、ruff job(check + format)、mypy 8 入口矩阵、coverage 分支统计(不设阈值)。Linux/macOS 装 Qt offscreen 系统库并设 `QT_QPA_PLATFORM=offscreen`;Windows 用原生插件。
- **build.yml**(tag `v*` + 手动触发):三平台 `uv sync` → 编译 Qt 翻译(.ts→.qm)→ PyInstaller 打包 → Linux AppImage(`scripts/build_appimage.sh`,appimagetool + rsvg-convert)+ macOS .app zip + Windows onedir zip → `release` job 建 GitHub Release + SHA256SUMS。**纯 Python,无原生库编译,分钟级完成**。
- **audit.yml**(每周一 09:30 UTC):`uv export --no-emit-project --no-hashes` → `pip-audit --strict -r`(全量供应链含 dev)。pip-audit 不直接读 uv.lock,必须先 export;裸 `pip-audit --strict` 会误审计工具自身环境。
- **dependabot.yml**:uv 生态 + GitHub Actions 每周一(Asia/Shanghai)扫描依赖,前缀 `deps` / `ci`。
- 发布流程(打 tag 前):本地过全部测试 → CI 全绿 → 打 `v<version>` tag 推远端 → build.yml 出产物。
- v2.0.0 已删除 `warm-cache` 预热 job 与 libtdjson / vcpkg 缓存 key(TDLib 时代产物)。

## 配置与数据目录

- 配置走环境变量(`TG_` 前缀,见 `core/config.py` 与 `.env.example`),运行期修改会保形写回数据目录的 `.env`。
- 数据目录由 platformdirs 决定(`platformdirs.user_data_dir`):
  - macOS:`~/Library/Application Support/tgmonitor`
  - Linux:`$XDG_DATA_HOME/tgmonitor`(默认 `~/.local/share/tgmonitor`)
  - Windows:`%APPDATA%/tgmonitor`
- 子目录:`session/`(Telethon session,`tgmonitor.session`)、`media/`;SQLite 数据库文件 `tgmonitor.db` 与 `.env` 也在该目录。
- 数据库:`TG_DB_BACKEND=sqlite`(默认)/ `postgres`;`TG_SQLITE_PATH` 自定义 SQLite 路径;`TG_DB_DSN` 配 PG。旧 `mongo` / `jsonl` 等非法值启动时由 `Settings.load_tolerant()` 回退默认 SQLITE 并弹提示,不阻断启动(GUI);CLI 路径仍严格校验。
- 代理:SOCKS5 走 `TG_PROXY`(`parse_socks5_proxy` → python-socks tuple → Telethon `proxy=`)。

## 安全注意事项

- **session 文件等同账号密码**:Telethon session 文件(`tgmonitor.session`)、`.env`(可能含 api_id / api_hash / 数据库口令)一律不得提交进 git(`.gitignore` 已覆盖,但新增凭据类文件要自觉)。
- CI 无任何凭据注入,测试全部用 fake;不要为了「让测试过」往 CI 塞真凭据。
- 安全漏洞报告走 `SECURITY.md` 的渠道。

## 扩展点

- **新增存储后端**(如 MySQL):在 `core/storage/` 实现 7 个按域 ABC(或组合 `StorageRepository`),注册到 factory,复用 DTO 与领域层,零改 UI。
- **新增对象存储后端**:在 `core/objectstore/` 实现 `ObjectStore` ABC,注册 factory(默认 FOLDER,平铺/分片可切换)。
- **新增导出格式**:在 `core/export/` 用 `@exporter` 装饰器注册到 `EXPORTERS`,service 流式导出自动可见;含模板的(如 HTML)用 Jinja2。
- **新增 Telegram 后端/假客户端**:实现 `TelegramClient` Protocol;测试用 `fake_client.FakeTelegramClient`。改 Protocol 签名必须同步 telethon_client / fake_client / unconfigured 三实现(见 CONTRIBUTING.md checklist;v2.0.0 无自动签名守护测试,靠 mypy + 离线测试 + 人工)。
- **新增领域事件**:在 `core/events.py` 定义,EventBus 发布;UI 侧订阅刷新。
- **新增域操作**:加在对应域服务(auth/channels/messages/media/batch/channel_sync)上,**不要**加回 AppService facade(allowlist 守护测试会拒)。
- **UI 侧新增跨线程异步调用**:一律走 `ui/async_bridge.py` 的 `run_coro`,不要另起模板。
- **新增 CLI 子命令**:在 `src/tgmonitor/cli/` 加 `xxx.py`,仿 `sync.py` / `monitor.py` 写 `async def run_xxx(args, *, app=None, monitor=None)`;然后在 `cli/main.py:build_parser()` 加 `sub.add_parser("xxx", …)` + 分支 `if args.cmd == "xxx": return asyncio.run(run_xxx(args))`。`__main__.py` 自动 argv 路由。**约束**:
  - CLI 子包内任何模块禁止 import PySide6 / qasync / `tgmonitor.ui.*`(`tests/test_cli.py::test_cli_does_not_import_qt_or_qasync` 是子进程断言守门,跨 conftest 隔离)
  - CLI 入口走 `core/runtime.py:bootstrap()`,与 GUI 共用 composition root;`run_xxx(args, *, app, monitor)` 入参允许测试注入 fakes,生产路径 `None` → 自动走 bootstrap()
  - 鉴权失败(`state != "ready"`):CLI 没有 phone/code 输入面板,直接退出码 1 + stderr 提示去 GUI 完成登录
  - 进度反馈走 stdout `print` + `EventBus.subscribe(ChannelSyncProgress, ...)`,与 GUI 共用事件流
