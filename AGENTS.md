# AGENTS.md — tgmonitor 项目指南

> 本文件为 AI agent 与人类开发者快速上手而写。内容基于仓库现有代码与文档
> (README、pyproject.toml、docs/ARCHITECTURE.md、CONTRIBUTING.md、.github/ workflows)
> 归纳,改代码时如与本文件矛盾,以实际代码为准并顺手更新本文件。

## 项目概述

**tgmonitor** — Telegram 频道监听桌面应用:监听白名单频道的新消息,自动落库并下载媒体,提供桌面 UI 查看、检索与导出。

- Python `~=3.13.0`,GUI 用 **PySide6**,异步用 **qasync**(Qt 主线程即 asyncio 事件循环),Telegram 协议走 **TDLib**(通过自编译 libtdjson 的 ctypes 绑定 `tdlib_json`),单文件启动。
- 当前版本 **1.0.7**(以 `pyproject.toml` 为准)。
- 包管理/构建:uv + hatchling,src 布局,console script 入口 `tgmonitor = tgmonitor.__main__:main`。
- 桌面打包:PyInstaller(`tgmonitor.spec`)→ Linux AppImage + macOS .app;发布走 GitHub Release + SHA256SUMS。
- 许可证 MIT,作者 forcetone;项目注释与文档均用中文,新代码注释随项目习惯用中文。

## 技术栈与运行架构

### 依赖(pyproject `[project]`)

| 依赖 | 版本 | 用途 |
|---|---|---|
| PySide6 | >=6.6 | Qt GUI |
| qasync | >=0.27 | Qt 事件循环接 asyncio |
| tdlib-json-client | (workspace) | 自编译 libtdjson 的 ctypes 绑定(仓库内子项目 `packages/tdlib_json`,零第三方依赖) |
| pydantic-settings | >=2.2 | 配置加载(`TG_` 前缀) |
| Jinja2 | >=3.1 | HTML 导出模板 |
| platformdirs | >=4.11.0 | 平台数据目录 |

可选依赖组:`postgres`(asyncpg)、`mongo`(motor)、`objectstore`(aioboto3,配 S3)、`all`。dev 组:pytest、pytest-asyncio、ruff、mypy、coverage、pyinstaller。

### 分层与边界(最重要的架构约束)

```
UI(PySide6 widgets/viewmodels)
  │  只依赖:AppService 门面 + EventBus + DTO
  ▼
core(app_service 门面 + 领域服务)
  │  只依赖:StorageRepository / ObjectStore / TelegramClient / Exporter 四套抽象
  ▼
存储(postgres_repo / mongo_repo / jsonl_store) · 对象存储(local/folder/s3) · 导出(json/csv/markdown/html)
```

- **`core/` 禁止 import PySide6 / qasync** — 保持领域逻辑与 UI 框架解耦,可离线单测。
- **跨边界只传 DTO**(`dto.py` 里的 ChannelDTO / MediaDTO / MessageDTO / ExportRequest / ExportResult / SyncOptions / SyncResult),不传 TDLib 对象、ORM 对象、Qt 对象。
- 四套可插拔抽象:`StorageRepository`(ABC)、`ObjectStore`(ABC)、`TelegramClient`(runtime_checkable Protocol)、`Exporter`(ABC + 注册表)。新增实现见下文「扩展点」。
- UI 跨线程调 coroutine 必须走 `ui/_async.py` 的 `run_coro(loop, coro, *, on_success, on_error, error_label)`,不要自己套 `asyncio.run_coroutine_threadsafe` + try/except 模板。

### 核心模块一览(`src/tgmonitor/`)

| 路径 | 职责 |
|---|---|
| `__main__.py` / `app.py` | 入口。`run()` 是组合根:构建 qasync `QEventLoop` 后**单 loop `run_forever` + `ensure_future`**(历史坑:不要用 `run_until_complete`);凭据未配置时 factory 返回占位 client,应用正常启动显示未登录引导;真 client 构造失败弹 QMessageBox;退出码 0/1/130 |
| `core/config.py` | pydantic-settings 配置,`TG_` 环境变量前缀 |
| `core/events.py` | EventBus:async pub/sub,订阅者异常被吞掉(不崩主流程)。领域事件如 LoginStateChanged / ConnectionStateChanged / ChannelSubscribed / MessageReceived / ExportDone / ErrorOccurred / AuthErrorOccurred / SettingsChanged / ChannelSyncProgress |
| `core/dto.py` | 跨边界数据传输对象 |
| `core/app_service.py` | UI 唯一入口门面,含热重载 `reconfigure()` |
| `core/auth_service.py` | 登录/登出与登录状态管理 |
| `core/settings_store.py` | `.env` 保形读写(`.part` + rename 原子写),`EditableSettings` 供 UI |
| `core/telegram/` | TDLib 封装:`tdlib_client` / `tdlib_channels` / `tdlib_messages` / `tdlib_proxy` / `tdlib_errors` / `factory`;`unconfigured.py` 是凭据缺失时 factory 返回的占位 client(启动不崩);`fake_client.py` 是测试用假客户端 |
| `core/monitor/service.py` | MonitorService:白名单监听 + 幂等落库 + 断线退避重连(1s→30s);MediaDownloader 单文件 200MB 上限 |
| `core/channel_sync/service.py` | 手动全量同步:`chat_delay_ms` / `page_delay_ms` 节流,支持断点续拉 |
| `core/storage/` | repository ABC + postgres_repo / mongo_repo / jsonl_store + 懒加载 factory;唯一键 `(channel_id, telegram_msg_id)`;media 只存引用不存二进制 |
| `core/objectstore/` | base ABC + local_store(平铺)/ folder_store(两级分片)/ s3_store + factory,默认 **FOLDER** |
| `core/export/` | base 注册表 `EXPORTERS` + `@exporter` 装饰器 + json/csv/markdown/html + 流式导出 service |
| `ui/` | PySide6 界面:main_window、widgets/、viewmodels/、theme.py、icon.py;`_async.py` 为跨线程样板 |

## 目录结构

```
├── src/tgmonitor/              # 全部源码(src 布局,见上表)
├── packages/tdlib_json/        # workspace 子项目:tdlib_json(ctypes 绑定自编译 libtdjson)
├── tests/                      # pytest 测试(257 用例);conftest.py 提供公共 fixtures
│   ├── golden/                 # 视觉回归黄金图(png)
│   └── fonts/                  # 测试字体资源
├── scripts/build_libtdjson.sh  # 编译 libtdjson(锁定 TDLib 1.8.46,macOS / Linux)
├── scripts/build_libtdjson.ps1 # Windows 编译 libtdjson(vcpkg 方式)
├── scripts/build_appimage.sh   # Linux AppImage 构建
├── tgmonitor.spec              # PyInstaller 打包配置
├── docs/ARCHITECTURE.md        # 架构详解
├── docs/SUBSCRIBED_DRIFT_ANALYSIS.md
├── .github/workflows/          # ci / build / audit
└── pyproject.toml              # 构建 + 工具配置(ruff/mypy/pytest 全在这里)
```

## 常用命令

> 运行/开发所需工具(2026-08-11 实测,macOS 已全部就位):
> - **uv**(必须,包管理 + 自动下载 Python;CI 同步)
> - **git**(克隆/提交)
> - **Python 3.13** — 由 uv 自动装(`uv python list`),不依赖系统 Python
> - **编译 libtdjson 需**:macOS `brew install cmake gperf openssl@3`;Linux `apt install cmake gperf libssl-dev zlib1g-dev build-essential`;Windows vcpkg + VS C++ 工具链(CI 的 windows-latest 自带)
> - macOS 跑 PySide6 无需额外系统库(wheel 自带);Linux 打包才需要 appimagetool / rsvg-convert
> - 可选 DB/S3 后端(extra)非运行必需;不装也能用默认 JSONL + Local 存储

```bash
# 编译 TDLib 引擎 libtdjson(首次 clone 后必跑;二进制被 gitignore,需自编译)
bash scripts/build_libtdjson.sh                     # macOS / Linux
powershell -File scripts/build_libtdjson.ps1        # Windows(vcpkg)

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
uv run mypy src/tgmonitor/...(按 CI 的 8 入口矩阵逐个跑)

# 覆盖率(有分支覆盖,但 CI 不设阈值)
PYTHONPATH=src uv run pytest --cov=tgmonitor

# 打包
uv run pyinstaller --clean -y tgmonitor.spec   # 产物在 dist/
bash scripts/build_appimage.sh                 # Linux AppImage(仅 Linux)
```

## 代码风格与约定(详见 CONTRIBUTING.md)

- 每个模块文件第一行 `from __future__ import annotations`;公共方法全量 type hints。
- 语法按 Python 3.10+ 写:`dataclass(slots=True)`、`str | None` 联合类型等。
- 函数第一行是 docstring(中文);`core/` 内日志用 `logging` 模块,不要 `print`。
- **ruff**:line-length 100,`select=["E","F","I","B","UP","N","SIM","ASYNC"]`,ignore E501/UP042/UP035/SIM105/N818/B008,零容忍(CI 不通过即报错)。
- **mypy**:低严格度(`disallow_untyped_defs=false`),第三方模块按 overrides 忽略;PySide6/tdlib_json 的 stub 噪声用文件头 `# mypy: disable-error-code=...` 屏蔽。
- commit 格式:`type(scope): subject`,type ∈ feat/fix/docs/refactor/test/chore/perf。
- 编码约定:4 空格缩进、LF 行尾、100 列上限(.editorconfig)。

## 测试策略

- **离线原则**:`core/` 单测不碰网络/真实 TDLib/真实数据库,全靠 fake — `FakeTelegramClient`、`InMemoryRepository`、`LocalObjectStore`,以及 `stub_tdlib_init` fixture 拦截 `tdlib_json.TdlibJsonClient` 构造(不加载真实 dylib)。
- pytest-asyncio 全局 `asyncio_mode="auto"`,`addopts="-ra -q"`(配置在 pyproject)。
- `tests/conftest.py` 常用 fixtures:`settings`(stub 配置)、`storage`、`objectstore`、`bus`、`client`、`monitor`、`app`、`make_message` / `make_photo` 工厂。
- **UI 测试必须 `QT_QPA_PLATFORM=offscreen`**(无头环境跑 Qt);测试系统库依赖见 CI workflow。
- 视觉回归(仅本地,不进 CI):黄金图在 `tests/golden/`,改动 UI 后本地 `UPDATE_GOLDENS=1 PYTHONPATH=src QT_QPA_PLATFORM=offscreen uv run pytest tests/test_visual_regression.py` 重生成并人工比对。macOS CI runner 与开发机对系统 emoji 字体的渲染有固定差异(golden 跨机比对不稳),故 2026-08-12 起从 CI 摘除;golden 仍走 git 管理、本地维护。
- 新增领域逻辑尽量走纯 core 单测(offline),不靠 UI 测试覆盖。

## CI/CD 与发布流程(`.github/workflows/`)

- **ci.yml**(push/PR → main):test 矩阵(ubuntu + macOS + windows × 3.13)、ruff job、mypy 8 入口矩阵、coverage 分支统计(不设阈值)。Linux/macOS 装 Qt offscreen 系统库并设 `QT_QPA_PLATFORM=offscreen`、`QT_LOGGING_RULES=*.warning=false`;视觉回归(`tests/test_visual_regression.py`)不进 CI,`pytest` 统一 `--ignore`。
- **build.yml**(tag `v*` + main push + 手动触发):先编译 libtdjson(Linux/macOS 走 `build_libtdjson.sh`,Windows 走 `build_libtdjson.ps1` vcpkg)→ PyInstaller 打包 → Linux AppImage(`scripts/build_appimage.sh`,appimagetool + rsvg-convert)+ macOS .app zip + Windows onedir zip → GitHub Release + SHA256SUMS。
- **libtdjson 编译缓存预热**:GitHub 缓存只有默认分支(main)写入的缓存其他 ref 才能恢复,故 push main 时 `warm-cache` job 只编译、不打包,把三平台产物写回 main scope;之后打 tag 发布用同一 key(`libtdjson-${{ runner.os }}-${{ runner.arch }}-v1`)直接命中,免去每次冷编译(Windows vcpkg 约 2h → 命中后几分钟)。缓存 7 天未访问自动清除;改 key 的 `v1` 可作废。
- **audit.yml**(每周一 09:30 UTC):`pip-audit --strict` 依赖漏洞扫描。
- **dependabot.yml**:uv 生态 + GitHub Actions 每周一(Asia/Shanghai)扫描依赖,前缀 `deps` / `ci`。
- 发布流程(打 tag 前):本地过全部测试 → CI 全绿 → 打 `v<version>` tag 推远端 → build.yml 出产物。

## 配置与数据目录

- 配置走环境变量(`TG_` 前缀,见 `core/config.py` 与 `.env.example`),运行期修改会保形写回数据目录的 `.env`。
- 数据目录由 platformdirs 决定(`platformdirs.user_data_dir`):
  - macOS:`~/Library/Application Support/tgmonitor`
  - Linux:`$XDG_DATA_HOME/tgmonitor`(默认 `~/.local/share/tgmonitor`)
  - Windows:`%APPDATA%/tgmonitor`
- 子目录:`session/`(TDLib session)、`messages/`、`media/`;`.env` 也在该目录。
- 代理:SOCKS5 走 `TG_PROXY`。

## 安全注意事项

- **session 文件等同账号密码**:TDLib session 文件、`.env`(可能含 api_id / api_hash / 数据库口令)一律不得提交进 git(`.gitignore` 已覆盖,但新增凭据类文件要自觉)。
- CI 无任何凭据注入,测试全部用 fake;不要为了「让测试过」往 CI 塞真凭据。
- 安全漏洞报告走 `SECURITY.md` 的渠道。

## 扩展点

- **新增存储后端**(如 MySQL):在 `core/storage/` 实现 `StorageRepository` ABC,注册到 factory,复用 DTO 与领域层,零改 UI。
- **新增对象存储后端**:在 `core/objectstore/` 实现 `ObjectStore` ABC,注册 factory(默认 FOLDER,平铺/分片/平铺可切换)。
- **新增导出格式**:在 `core/export/` 用 `@exporter` 装饰器注册到 `EXPORTERS`,service 流式导出自动可见;含模板的(如 HTML)用 Jinja2。
- **新增 Telegram 后端/假客户端**:实现 `TelegramClient` Protocol;测试用 `fake_client.FakeTelegramClient`。
- **新增领域事件**:在 `core/events.py` 定义,EventBus 发布;UI 侧订阅刷新。
- **UI 侧新增跨线程异步调用**:一律走 `ui/_async.py` 的 `run_coro`,不要另起模板。
