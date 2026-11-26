# AGENTS.md — tgmonitor 项目指南

> 本文件为 AI agent 与人类开发者快速上手而写,**只写现状**。历史归因、已完成的
> 计划与点时间快照统一放 `docs/history/`(索引见 `docs/README.md`)。改代码时如与
> 本文件矛盾,以实际代码为准并顺手更新本文件。
>
> 最后校准:2026-11-26(2.1.7)。文中的数字(用例数 / job 数 / 覆盖率)会漂移,
> 标注了「以 … 为准」的地方请现查,不要照抄。

## 项目概述

**tgmonitor** — Telegram 频道监听桌面应用:监听白名单频道的新消息,自动落库并下载媒体,提供桌面 UI 查看、检索与导出。

- Python `~=3.13.0`,GUI 用 **PySide6**,异步用 **qasync**(Qt 主线程即 asyncio 事件循环),Telegram 协议走 **Telethon**(纯 Python MTProto,`cryptg` 加速加密、`python-socks` 走 SOCKS5),单文件启动。
- 当前版本 **2.1.7**(以 `pyproject.toml` 为准)。
- 包管理/构建:uv + hatchling,src 布局,console script 两个入口:`tgmon = tgmonitor.__main__:main`(**只启动 GUI**)、`tgmon-cli = tgmonitor.cli.main:main_tgmon_cli`(**独占 CLI**,13 个子命令含 `help`)。`tgmonitor` 命令 2.1.2 已删除 —— 新写文档 / 提示语不要再出现它。
- 桌面打包:PyInstaller(`tgmonitor.spec`)→ Linux AppImage + macOS .app + Windows onedir zip;发布走 GitHub Release + SHA256SUMS。**纯 Python 后端,三平台均无需编译原生引擎**。
- 许可证 MIT,作者 forcetone;项目注释与文档均用中文,新代码注释随项目习惯用中文。
- **登录只有二维码扫码一种方式**:只需 `api_id` / `api_hash`,不涉及手机号 / 验证码 / 2FA。GUI 在登录对话框扫码;headless 用 `tgmon-cli login`(二维码画在 stderr)。

## 技术栈与运行架构

### 依赖(pyproject `[project]`)

| 依赖 | 版本 | 用途 |
|---|---|---|
| PySide6 | >=6.6,<6.13 | Qt GUI(6.12 解禁的归因史见 `docs/history/pyside6-6.12-unban.md`) |
| qasync | >=0.27 | Qt 事件循环接 asyncio |
| telethon | >=1.45.0 | 纯 Python MTProto Telegram 后端 |
| cryptg | >=0.6.1 | Telethon 加密加速(C 扩展,可选但默认装) |
| python-socks[asyncio] | >=3.2.1 | SOCKS5 代理 |
| pydantic-settings | >=2.2 | 配置加载(`TG_` 前缀) |
| Jinja2 | >=3.1 | HTML 导出模板 |
| platformdirs | >=4.11.0 | 平台数据目录 |
| aiosqlite | >=0.22.1 | 默认 SQLite 后端(WAL) |

可选依赖组:`postgres`(asyncpg)、`objectstore`(aioboto3,配 S3)、`all`。**无 mongo**。
dev 组(以 pyproject `[dependency-groups]` 为准):pytest、pytest-asyncio、ruff、mypy>=2.4.1、coverage、pyinstaller>=6.21.0、moto[s3]>=5、pre-commit>=4.7.0、testcontainers[postgres]>=4.8、hypothesis>=6.100、pytest-timeout>=2.3。**没有 pytest-cov** —— 覆盖率用 `coverage run`。

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

- **`core/` 禁止 import PySide6 / qasync** — 保持领域逻辑与 UI 框架解耦,可离线单测。`tests/test_architecture.py` 用 AST 守护(含延迟 import)。
- **跨边界只传 DTO**(`dto.py` 里的 ChannelDTO / MediaDTO / MessageDTO / ExportRequest / ExportResult / SyncOptions / SyncResult 等),不传 Telethon 对象、ORM 对象、Qt 对象。
- **datetime 约定:aware UTC 是规范形,naive 一律视为 UTC**(`MessageDTO.date` 默认工厂就是 `datetime.now(UTC)`)。读回的形状因后端而异 —— PG(`TIMESTAMPTZ`)返回 aware,SQLite 原样 round-trip,InMemory 保留原对象 —— 所以比较时间要比「瞬时」,不要比 tzinfo 有无(见 `tests/test_storage_backends.py::_instant`)。PG 写路径的 datetime 参数先过 `_sqlite_common._ensure_utc`。
- 四套可插拔抽象:`StorageRepository`(8 个按域 ABC 组合)、`ObjectStore`(ABC + local_path/uri 能力方法)、`TelegramClient`(runtime_checkable Protocol)、`Exporter`(ABC + 注册表)。新增实现见下文「扩展点」。
- **AppService 不再是 god facade**:只保留生命周期 / 热重载 / 导出 + 后端持有;域操作 UI 直调 `app.auth/channels/messages/media/batch/channel_sync`。公开表面由 `tests/test_app_service_surface.py` allowlist 锁死,新增域转发会被守护测试拒绝。
- UI 跨线程调 coroutine 必须走 `ui/async_bridge.py` 的 `run_coro(loop, coro, *, on_success, on_error, error_label)`,不要自己套 `asyncio.run_coroutine_threadsafe` + try/except 模板。

### 核心模块一览(`src/tgmonitor/`)

| 路径 | 职责 |
|---|---|
| `__main__.py` / `app.py` | 入口。`run()` 是 GUI 组合根:构建 qasync `QEventLoop` 后**单 loop `run_forever` + `ensure_future`**(历史坑:不要用 `run_until_complete`);凭据未配置时 factory 返回占位 client,应用正常启动显示未登录引导;真 client 构造失败弹 QMessageBox;退出码 0/1/130。`__main__.main()` 是 **纯 GUI 入口**:无参数 / `gui` → `app.run()`;其余参数 → stderr 报错 + 退出码 2 并提示 `tgmon-cli`。CLI 全部走 `tgmon-cli` |
| `core/runtime.py` | **CLI / GUI 共享 composition root**:`bootstrap()` 与 `shutdown()`(配套清理) |
| `core/config.py` | pydantic-settings 配置,`TG_` 环境变量前缀;`DBBackend` SQLITE(默认)/ POSTGRES;完整字段见 `.env.example` |
| `core/events.py` | EventBus:async pub/sub,订阅者异常被吞掉(不崩主流程)。领域事件如 LoginStateChanged / ConnectionStateChanged / ChannelSubscribed / MessageReceived / MessageEdited / MessageDeleted / MediaDownloaded / ExportDone / BatchDone / ChannelSyncProgress / ErrorOccurred / AuthErrorOccurred / SettingsChanged |
| `core/dto.py` | 跨边界数据传输对象 |
| `core/app_service/` | **组合根 + 生命周期**(`app_service.py` + `_lifecycle` / `_reconfigure` / `_backend_sync`)。装配域服务,不再是 god facade |
| `core/auth_service.py` | AuthService:二维码登录 / 登出与登录状态管理(`app.auth`) |
| `core/channel_service.py` | ChannelService:订阅 / 退订 / 白名单(`app.channels`) |
| `core/message_service.py` | MessageService:消息查询 / 收藏 / 标签 / 备注 / 单删(`app.messages`) |
| `core/media_service/` | MediaService:媒体列表 / CRUD / 打开 / retry / reconcile(`app.media`)。「打开」只放行白名单扩展名(2.1.7) |
| `core/batch_service.py` | BatchService:6 个批量 op(删 / 转发 / 置顶 / 取消置顶 / 加 / 减回应)+ `cancel_current_batch`,每个 op 自持 cancel event(`app.batch`) |
| `core/channel_sync/` | ChannelSyncService:手动全量同步,`chat_delay_ms` / `page_delay_ms` 节流,断点续拉(`app.channel_sync`) |
| `core/settings_store.py` | 数据目录 `.env` 保形读写(临时文件 + rename 原子写,文件权限 `0600`),`EditableSettings` 供 UI |
| `core/telegram/` | Telethon 封装:`client`(Protocol)/ `telethon_client`(TelethonTelegramClient)/ `telethon_messages`(归一化)/ `proxy`(python-socks)/ `errors` / `factory`;`unconfigured.py` 是凭据缺失时 factory 返回的占位 client(启动不崩);`fake_client.py` 是测试用假客户端 |
| `core/monitor/` | MonitorService(**显式组合**,非 mixin):`service`(宿主 facade)+ `_runtime`(`MonitorRuntime`,组件间共享状态,5 组件只依赖它、不反向持有宿主)+ `orchestrator`(主循环 + 退避重连 1s→30s)+ `whitelist` / `event_handlers` / `downloader`(MediaDownloader 单文件 200MB 上限)/ `backfill` / `delete` |
| `core/storage/` | 8 个按域 ABC(SchemaLifecycle / ChannelStore / MetaStore / MessageStore / MediaStore / ThumbnailStore / UserMetaStore / ActivityStore)组合成 `StorageRepository`;`sqlite_repo`(默认,aiosqlite WAL)/ `postgres_repo`(asyncpg),两者都是 facade + 按域 helper 模块(`_*_sqlite.py` / `_*_pg.py`)+ 懒加载 factory;唯一键 `(channel_id, telegram_msg_id)`;media 只存引用不存二进制 |
| `core/objectstore/` | base ABC + local_store(平铺)/ folder_store(两级分片)/ s3_store + factory,默认 **FOLDER** |
| `core/export/` | base 注册表 `EXPORTERS` + `@exporter` 装饰器 + json/csv/markdown/html + 流式导出 service |
| `cli/` | argparse 13 个子命令(含 `help`);裸 `asyncio.run()` 事件循环,无 PySide6。`main.py` 是注册表单一真源(`CLI_SUBCOMMANDS` + `_DISPATCH`);`_common.py` 收口 `cli_context` / `emit_json` / `progress` / `CliError` |
| `ui/` | PySide6 界面:`main_window.py`、`widgets/`、`viewmodels/`、`controllers/`、`icon.py`、`nav_bar.py`、`ui_assembler.py`、`shutdown.py`、`thumbnail.py`、`state_labels.py`;`async_bridge.py` 是跨线程样板;全局样式表 `ui/resources/style.qss` |

## 目录结构

```
├── src/tgmonitor/              # 全部源码(src 布局,见上表)
├── tests/                      # pytest 测试(约 1180 个默认用例 + 105 个 integration 标记;以 pytest --collect-only 为准)
│   ├── fixtures/               # settings/storage/objectstore/bus/client/monitor/app/make_* 等;_pg.py 是真 PG DSN
│   ├── integration/            # 真 PG 集成(testcontainers,CI 跑,本地无 Docker 自动 skip)
│   └── perf/                   # 本地手工基准,默认不收集,显式 `pytest tests/perf/<文件>` 跑
├── scripts/build_appimage.sh   # Linux AppImage 构建(唯一脚本;无 libtdjson 编译)
├── tgmonitor.spec              # PyInstaller 打包配置(纯 Python,binaries=[])
├── docs/                       # 文档:README.md 是索引;history/ 是点时间快照
├── .github/workflows/          # ci / build / audit
└── pyproject.toml              # 构建 + 工具配置(ruff/mypy/pytest/coverage 全在这里)
```

## 常用命令

> 运行/开发所需工具:uv(必须)、git。Python 3.13 由 uv 自动装,无需任何原生编译工具链;macOS 跑 PySide6 无需额外系统库,Linux 打包才需要 appimagetool / rsvg-convert。

```bash
# 安装依赖(dev 全量 + 可选 DB/S3 后端;少了 extras 会有测试模块收集失败)
uv sync --all-groups --all-extras

# 运行应用(GUI)
uv run tgmon

# 命令行(全部 CLI 操作)
uv run tgmon-cli help

# 跑测试(核心单测全离线;UI 测试需要 offscreen 平台)
PYTHONPATH=src uv run pytest -v
PYTHONPATH=src QT_QPA_PLATFORM=offscreen uv run pytest -v            # 含 UI 测试
PYTHONPATH=src QT_QPA_PLATFORM=offscreen uv run pytest tests/<文件> -v -k <名字>

# 只跑集成测试(需 Docker;没有则自动 skip)
uv run pytest -m integration

# lint / typecheck —— 用锁定版 ruff,与 CI 同版本(别用 `uv tool run --from "ruff>=0.5"`,那是最新的浮动版)
uv run ruff check src tests
uv run ruff format --check src tests          # 只跑 check 不跑 format 会漏格式问题
uv run mypy src/tgmonitor                    # 只跑 src;tests 目录有既有类型噪声,别 `mypy src tests`

# 覆盖率(分支覆盖;CI coverage job 同款,阈值 70)
PYTHONPATH=src QT_QPA_PLATFORM=offscreen uv run coverage run -m pytest && uv run coverage report --fail-under=70

# 打包
uv run pyinstaller --clean -y tgmonitor.spec   # 产物在 dist/
bash scripts/build_appimage.sh                 # Linux AppImage(仅 Linux)
```

> 全套测试本地约 100~150s。`addopts` 已带 `-q`,命令行再加一个 `-q` 等于 `-qq`,**汇总行会被吞掉**。
> 用 Bash 跑时 timeout 设 ≥ 600000ms。

## 代码风格与约定(详见 CONTRIBUTING.md)

- 每个模块文件第一行 `from __future__ import annotations`;公共方法全量 type hints。
- 语法按 Python 3.10+ 写:`dataclass(slots=True)`、`str | None` 联合类型等。
- 函数第一行是 docstring(中文);`core/` 内日志用 `logging` 模块,不要 `print`。
- **ruff**:line-length 100,`select=["E","F","I","B","UP","N","SIM","ASYNC","BLE","C90","RUF100"]`,ignore E501/UP042/UP035/SIM105/N818/B008,零容忍。圈复杂度上限 15(唯一豁免:`app.py:run()`);静默吞任意异常必须写 `# noqa: BLE001` 知情抑制;**失效的 `# noqa` 即错误**(RUF100)—— 加豁免前先确认规则真的触发。ANN 暂不开启。
- **mypy**:低严格度(`disallow_untyped_defs=false`),第三方模块按 overrides 忽略;PySide6 的 stub 噪声用文件头 `# mypy: disable-error-code="attr-defined"` 屏蔽。CI 单 job 整包跑 `uv run mypy src/tgmonitor`。
- **UI 资源**:`ui/resources/style.qss` 由 `app.py` 启动加载并替换 `{accent}` / `{accentHover}` 占位符;无翻译文件编译步骤。
- commit 格式:`type(scope): subject`,type ∈ feat/fix/docs/refactor/test/ci/chore/perf。
- 分支/tag:`main` 唯一长期分支(禁直推);主题分支 `<type>/<kebab-描述>`(不带版本号/PR 号),合并即删;tag `vX.Y.Z` 只打 main,历史 v1.x tag 永久保留。详见 CONTRIBUTING.md「🌿 分支与 tag 规范」。
- 编码约定:4 空格缩进、LF 行尾、100 列上限(.editorconfig)。

## 测试策略

- **离线原则**:`core/` 单测不碰网络/真实 Telegram/真实数据库,全靠 fake — `FakeTelegramClient`、`InMemoryRepository`、`LocalObjectStore`、fake S3(moto)。
- pytest-asyncio 全局 `asyncio_mode="auto"`,`addopts="-ra -q -m 'not integration'"`,`timeout=300` / `timeout_method="thread"`(配置在 pyproject;防 CI fixture hang)。
- `tests/fixtures/` 常用 fixtures:`settings`(stub 配置)、`storage`(InMemoryRepository)、`objectstore`、`bus`、`client`(FakeTelegramClient)、`monitor`、`app`、`make_message` / `make_photo` 工厂。`Settings.for_test()` 默认 SQLite + METADATA + LOCAL,离线可跑。
- **UI 测试必须 `QT_QPA_PLATFORM=offscreen`**(无头环境跑 Qt;Windows CI 用原生 `windows` 插件);测试系统库依赖见 CI workflow。全会话共享 1 个后台 loop,per-test `deleteLater()` 顶层 widget(不要 `close()`)。
- **存储 parity 三后端通吃**:`tests/test_storage_backends.py` 的 `seeded_repo` / `meta_seeded_repo` 参数化 `inmemory` / `sqlite` / `pg`。`pg` 参数带 `integration` marker,默认 deselect;CI integration job 用 `-m integration` 选中在真 PG 上跑。
- **集成测试**(`tests/integration/` + 上述 `pg` 参数,真 PG via testcontainers):本地无 Docker 自动 skip;**CI 断言零 skip**(`pg_engine` 起不来时 job 会红,不会静默全绿)。
- **守护测试清单已改自动发现**(2.1.7):存储层「无裸 `subscribed` 列」、monitor「组件不反向持有宿主」、CLI「零 GUI 依赖 + 注册表三重对齐」都按目录 glob 扫描 —— **新增模块自动纳入,不需要登记**;若目录结构大改,先看对应的 inventory 元测试。
- 新增领域逻辑尽量走纯 core 单测(offline),不靠 UI 测试覆盖。

## CI/CD 与发布流程(`.github/workflows/`)

- **ci.yml**(push/PR → main,7 个 job,全部阻塞合并,无 `continue-on-error`;以文件为准):
  - `pytest` × 3 平台(ubuntu / macOS / Windows × 3.13,裸 `python -m pytest`)
  - `coverage`(ubuntu,分支覆盖率,`coverage report --fail-under=70` **是门禁**)。插桩曾在 runner 上 hang,所以只放独立 job;hang 由 `pytest-timeout` 兜底成一次带线程栈的失败
  - `integration`(ubuntu,`-m integration` 跑真 PG;输出 junit 并断言用例数 > 0、零 skip、`[pg]` 参数化项实跑过)
  - `ruff`(check + format;**版本取自 `uv.lock`**,另跑 `uv lock --check`)
  - `mypy`(单 job 整包)
  - 改 console script 入口必须同步 `ci.yml` 的 "Verify CLI entry" 步骤,否则三平台 pytest job 会秒红。
- **build.yml**(tag `v*` + 手动触发):三平台 `uv sync` → PyInstaller 打包 → Linux AppImage + macOS .app zip + Windows onedir zip → `release` job 建 GitHub Release + SHA256SUMS。**纯 Python,无原生库编译**。
- **audit.yml**(每周一 09:30 UTC):`uv export` → `pip-audit --strict -r`(全量供应链含 dev)。pip-audit 不直接读 uv.lock,必须先 export;裸 `pip-audit --strict` 会误审计工具自身环境。
- **dependabot.yml**:uv 生态 + GitHub Actions 每周一(Asia/Shanghai)扫描依赖,前缀 `deps` / `ci`。
- 发布流程(打 tag 前):本地过全部测试 → CI 全绿 → 只推 lightweight `v<version>` tag → build.yml 出产物。版本落点 6 处:`pyproject` / `__init__.py` / `uv.lock` / `tgmonitor.spec` 两字段 / 本文件 / `CHANGELOG.md`。

## 配置与数据目录

- 配置走环境变量(`TG_` 前缀,字段清单与默认值见 `.env.example`),运行期修改会保形写回数据目录的 `.env`。
- **`.env` 在平台数据目录,不在仓库根、不相对 cwd** —— 往仓库根放 `.env` 不会被读取。完整清单见 `.env.example`;日常改配置走设置页。
- 数据目录由 platformdirs 决定(`user_data_dir("tgmonitor", appauthor=False)`):
  - macOS:`~/Library/Application Support/tgmonitor`
  - Linux:`$XDG_DATA_HOME/tgmonitor`(默认 `~/.local/share/tgmonitor`)
  - Windows:`%LOCALAPPDATA%\tgmonitor`(Local,非 Roaming)
- 子目录:`session/`(Telethon session,`tgmonitor.session`)、`media/`、`logs/tgmonitor.log`(5MB × 3 轮转);SQLite 数据库 `tgmonitor.db` 与 `.env` 也在该目录。
- `TG_LOG_LEVEL` 只读**进程环境变量**(不走 `.env`),排查监听问题时设 `DEBUG` 能看到 monitor 心跳。
- 数据库:`TG_DB_BACKEND=sqlite`(默认)/ `postgres`;`TG_SQLITE_PATH` 自定义 SQLite 路径;`TG_DB_DSN` 配 PG。旧 `mongo` / `jsonl` 等非法值启动时由 `Settings.load_tolerant()` 回退默认 SQLITE 并弹提示,不阻断启动。
- 代理:SOCKS5 走 `TG_PROXY`(`parse_socks5_proxy` → python-socks tuple → Telethon `proxy=`)。

## 安全注意事项

- **session 文件等同账号密码**:Telethon session 文件(`tgmonitor.session`)、`.env`(含 api_id / api_hash / 数据库口令 / S3 密钥 / 代理口令)一律不得提交进 git(`.gitignore` 已覆盖,但新增凭据类文件要自觉)。
- 日志 / CLI 输出里不得出现凭据:`config show` 对 URL 类字段脱敏(`redact_url_credentials`),其余敏感项打码;二维码 token 不进日志。新增会打印配置或 URL 的代码,沿用同一套 helper。
- 「打开媒体」只放行白名单扩展名(远端频道可任意控制文件名 / 扩展名,`.bat` / `.html` / `.svg` 等一律拒绝)。
- CI 无任何凭据注入,测试全部用 fake;不要为了「让测试过」往 CI 塞真凭据。
- 安全漏洞报告走 `SECURITY.md` 的渠道。

## 扩展点

- **新增存储后端**(如 MySQL):在 `core/storage/` 实现 8 个按域 ABC(或组合 `StorageRepository`),注册到 factory,复用 DTO 与领域层,零改 UI。写路径的 datetime 参数过 `_ensure_utc`。
- **新增对象存储后端**:在 `core/objectstore/` 实现 `ObjectStore` ABC,注册 factory(默认 FOLDER,平铺/分片可切换)。
- **新增导出格式**:在 `core/export/` 用 `@exporter` 装饰器注册到 `EXPORTERS`,service 流式导出自动可见;含模板的(如 HTML)用 Jinja2。
- **新增 Telegram 后端/假客户端**:实现 `TelegramClient` Protocol;测试用 `fake_client.FakeTelegramClient`。改 Protocol 签名必须同步 telethon_client / fake_client / unconfigured 三实现(v2.0.0 无自动签名守护测试,靠 mypy + 离线测试 + 人工)。
- **新增领域事件**:在 `core/events.py` 定义,EventBus 发布;UI 侧订阅刷新。EventBus 订阅者必须是 async。
- **新增域操作**:加在对应域服务(auth/channels/messages/media/batch/channel_sync)上,**不要**加回 AppService facade(allowlist 守护测试会拒)。
- **UI 侧新增跨线程异步调用**:一律走 `ui/async_bridge.py` 的 `run_coro`,不要另起模板。
- **新增 CLI 子命令**:在 `src/tgmonitor/cli/` 加 `xxx.py`,仿 `sync.py` 写 `async def run_xxx(args, *, app=None, monitor=None)`;然后在 `cli/main.py` 三处同步注册表:`build_parser()` 加 `sub.add_parser("xxx", …)`、`CLI_SUBCOMMANDS` 加 `"xxx"`、`_DISPATCH` 加 `"xxx": ("tgmonitor.cli.xxx", "run_xxx")`。`__main__.py` 只管 GUI,无需改动。**约束**:
  - CLI 子包内任何模块禁止 import PySide6 / qasync / `tgmonitor.ui.*`。`tests/test_cli.py::test_cli_does_not_import_qt_or_qasync` **按目录自动发现** `cli/*.py` 逐个 import,并断言 磁盘模块 == `_DISPATCH` 模块 + `_common`/`main`、`CLI_SUBCOMMANDS` == `_DISPATCH` 键 + `help`、parser 认得每个命令 —— 漏任何一处都会红。
  - CLI 入口走 `core/runtime.py:bootstrap()`,与 GUI 共用 composition root;`run_xxx(args, *, app, monitor)` 入参允许测试注入 fakes,生产路径 `None` → 自动走 bootstrap()。
  - 鉴权失败(`state != "ready"`):退出码 1 + stderr 提示执行 `tgmon-cli login`(headless 可用,不必去 GUI)。
  - 进度反馈走 stderr,stdout 只输出数据(`--json` 可机读);写操作强制 `--yes`。
