# 📡 tgmonitor

> **Telegram 频道监听桌面应用** — 监听 / 保存 / 导出,UI 与 core 彻底分离,边界清晰。

[![CI](https://github.com/forcetone/tgmonitor/actions/workflows/ci.yml/badge.svg)](https://github.com/forcetone/tgmonitor/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
**v0.2.0** — 新增 JSONL 消息存储、两级分片对象存储、设置对话框与热重载

一个基于 [TDLib](https://github.com/tdlib/td) 官方库的 Telegram 频道监听桌面应用。
通过用户账号登录,可监听任何已加入的频道(含公开频道),将消息结构化保存到数据库,媒体二进制入对象存储,并支持 JSON / CSV / Markdown / HTML 四种导出格式。

---

## ✨ 特性

- 🧩 **UI 与 core 严格分离** — UI 只依赖一个 `AppService` 门面 + `EventBus` + DTO,core 禁 import 任何 UI 框架
- 🗄️ **多数据库后端** — 消息数据持久化支持 **PostgreSQL** / **MongoDB** / **JSONL 文件**(无需 DB 服务),config 切换
- 📦 **多对象存储后端** — 缩略图/媒体走 **本地平铺** / **本地两级分片** / **S3 协议**(AWS S3 / MinIO / 阿里 OSS),DB 仅存引用 key
- 🔌 **官方 TDLib 集成** — 通过 `aiotdlib` 使用 TDLib 官方协议引擎,业务侧只见 `TelegramClient` 接口
- 📤 **多格式导出** — JSON / CSV / Markdown / HTML(HTML 可内嵌 base64 缩略图)
- 🧪 **100% 可离线单测** — 4 套 ABC 抽象 + Fake/Fake 实现,core 完全可脱网测试
- 🔁 **自动重连** — 监听循环遇错指数退避后自动重订阅
- ⚙️ **运行时设置对话框** — 改 Telegram/DB/对象存储/媒体策略;支持**热重载**(无需重启 app)
- 🎨 **原生桌面 UI** — PySide6 (Qt) + qasync 单事件循环,无 web 引擎

---

## 🏗️ 架构

```
┌─────────────────────────── UI (PySide6) ───────────────────────────┐
│  LoginDialog · ChannelPanel · MessageView · ExportDialog · Settings │
│        │             │            │             │            │      │
│        └─────────────┴──────── ViewModels (QObject) ──────┬────┘      │
│                            │ 订阅↓                     │             │
│                       AppService (facade) ◀──────────────┘             │
└─────────────────────────────────│─────────────────────────────────────┘
                                  │ DTO + Event
┌───────────────────────────── Core (asyncio) ─────────────────────────┐
│  MonitorService ──► ObjectStore (S3 / Local) ──► StorageRepo         │
│       │                       (缩略图/媒体)       (Postgres / Mongo) │
│       └────► EventBus (领域事件) ◄── TelegramClient (TDLib)          │
└──────────────────────────────────────────────────────────────────────┘
```

详细架构与模块指引见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

### 边界守则(架构核心)

1. **`AppService` 门面** — UI 唯一入口。所有跨层方法都是 `async`,返回 DTO。
2. **`EventBus` 异步 pub/sub** — core 发布领域事件,UI 订阅。core 永远不 import PySide6。
3. **DTO(`@dataclass`)** — 跨边界只传纯数据对象(`MessageDTO` / `ChannelDTO` / `MediaDTO`),不传 TDLib 或 ORM 对象。
4. **接口/实现分离** — `StorageRepository` / `ObjectStore` / `TelegramClient` / `Exporter` 全部 ABC/Protocol,可 mock、可替换。
5. **`qasync`** — Qt 与 asyncio 共享一个事件循环,跨线程用 `run_coroutine_threadsafe` 投递。

---

## 📦 安装

需要 **Python 3.11+**(代码使用 `str | None` 与 `dataclass(slots=True)` 等 3.10+ 特性)。

```bash
git clone https://github.com/forcetone/tgmonitor.git
cd tgmonitor
```

使用 [uv](https://github.com/astral-sh/uv)(推荐):

```bash
uv sync --all-extras
```

或使用 pip:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"
```

### 选择性安装(按需)

```bash
# 只用 PostgreSQL + S3
pip install -e ".[postgres,objectstore]"

# 只用 MongoDB + S3
pip install -e ".[mongo,objectstore]"

# 只要核心(默认 Postgres + Local 存储)
pip install -e .
```

---

## ⚙️ 配置

```bash
cp .env.example .env
```

编辑 `.env`(从 https://my.telegram.org/apps 申请 `TG_API_ID` / `TG_API_HASH`):

```env
# Telegram 凭据(也可在主窗口左侧「账户」面板里直接填)
TG_API_ID=123456
TG_API_HASH=abcdef0123456789abcdef0123456789
TG_PHONE=+8613800000000

# SOCKS5 代理(国内直连 Telegram 不通时常需)
# TG_PROXY=socks5://user:pass@127.0.0.1:1080

# 数据库后端: postgres | mongo | jsonl
TG_DB_BACKEND=postgres
TG_DB_DSN=postgresql://tgmonitor:tgmonitor@localhost:5432/tgmonitor

# 对象存储后端: local | s3
TG_OBJECTSTORE_BACKEND=local
TG_OBJECTSTORE_ROOT=./data/media

# S3 协议(AWS S3 / MinIO / 阿里 OSS)启用时填:
# TG_OBJECTSTORE_ENDPOINT=http://localhost:9000
# TG_OBJECTSTORE_REGION=us-east-1
# TG_OBJECTSTORE_ACCESS_KEY=minioadmin
# TG_OBJECTSTORE_SECRET_KEY=minioadmin
# TG_OBJECTSTORE_BUCKET=tgmonitor

# 媒体下载策略: metadata | thumbnail | full
TG_MEDIA_POLICY=thumbnail
```

| 变量 | 说明 |
|---|---|
| `TG_API_ID` / `TG_API_HASH` | Telegram 应用凭据([my.telegram.org](https://my.telegram.org/apps)) |
| `TG_PHONE` | 登录手机号(含国家区号) |
| `TG_DB_BACKEND` | `postgres` 或 `mongo` |
| `TG_DB_DSN` | 数据库连接串 |
| `TG_OBJECTSTORE_BACKEND` | `local` 或 `s3` |
| `TG_OBJECTSTORE_ROOT` | local backend 数据根目录 |
| `TG_OBJECTSTORE_ENDPOINT` | S3 协议 endpoint(MinIO/OSS 时显式指定) |
| `TG_OBJECTSTORE_BUCKET` | 目标桶 |
| `TG_MEDIA_POLICY` | 媒体下载强度,见下表 |

`TG_MEDIA_POLICY`:

| 值 | 行为 |
|---|---|
| `metadata` | 仅元数据(类型/mime/大小/尺寸),不下任何二进制 |
| `thumbnail` | 元数据 + 缩略图(默认) |
| `full` | 元数据 + 缩略图 + 原文件 |

---

## 🚀 运行

```bash
# uv
uv run python -m tgmonitor

# pip + venv
python -m tgmonitor
```

主窗口布局:

- **左栏「账户」**(常驻):填 API ID / API Hash / 手机号,**保存到 .env**;
  状态点会从「未配置 → 未登录」流转。点「登录」按钮后,验证码 / 2FA 输入框**就地切换**,
  无需再去设置弹窗。
- **左栏「频道」**(常驻):上半「全部(已加入)」双击订阅,下半「已监听」双击退订。
- **右侧** 实时消息流。
- **工具栏** 仅 3 动作:`刷新频道` · `导出…` · `设置…`(后端 / 代理 / 媒体策略等低频)。

需要 SOCKS5 代理?设置 → 网络代理 → 填 `socks5://user:pass@host:port` → 测试连接 → 保存并应用。

---

## 📤 导出

工具栏 → **导出…**,选择:

- **频道**(多选)
- **时间范围**(可选)
- **格式**:JSON · CSV · Markdown · HTML
- **输出路径**
- HTML 可选**内嵌 base64 缩略图**(从 ObjectStore 拉取)

导出在后台流式进行,大消息量也不会一次性塞内存。

---

## 🧪 测试

```bash
# 全部测试(20 个,全离线,无需 Telegram/DB/S3)
PYTHONPATH=src python3 -m pytest

# 详细
PYTHONPATH=src python3 -m pytest -v

# 某个文件
PYTHONPATH=src python3 -m pytest tests/test_exporters.py

# Lint
ruff check src tests

# Coverage(xml 上传到 CI artifact)
python -m coverage run -m pytest -q
python -m coverage report
```

测试覆盖:

| 文件 | 范围 | 用例数 |
|---|---|---|
| `tests/test_storage.py` | InMemoryRepository 查询/去重/级联删除 | 5 |
| `tests/test_objectstore.py` | LocalObjectStore CRUD + 越界防御 | 5 |
| `tests/test_exporters.py` | 4 格式快照 + 缩略图内嵌 + 注册表 | 6 |
| `tests/test_monitor_and_app.py` | Monitor 去重/事件/AppService 登录/订阅 | 4 |

### 🤖 CI (GitHub Actions)

每次 push / PR 都会触发两道 job(见 `.github/workflows/ci.yml`):

- **`test` 矩阵** — Ubuntu + macOS × Python 3.11 / 3.12 / 3.13(6 个组合)
  - 装 Qt offscreen 系统库(`libegl1` 等)+ `QT_QPA_PLATFORM=offscreen`,
    Linux runner 上 PySide6 才不崩在 `libEGL.so.1`
  - 跑 `pytest -v --tb=short`
  - 跑 `coverage run` + `coverage xml`,per-OS-per-Python-version 上传成
    `coverage-${{ matrix.os }}-${{ matrix.python-version }}` artifact
  - **不设覆盖率阈值**(避免新代码被 churn 拒绝,各 PR 自己看 artifact)
- **`lint`** — `ruff check src tests`(任何 noqa 0 容忍)

CI 跑无凭据:不读 `TG_API_ID` / `TG_API_HASH` / `TG_PHONE`,所有 Telegram
相关代码路径走 `FakeTelegramClient`。见 [SECURITY.md](SECURITY.md)。

本地复跑:

```bash
# 与 CI 等价(本地装了 PySide6 / libEGL 后)
PYTHONPATH=src QT_QPA_PLATFORM=offscreen pytest -v --tb=short
```

---

## 🛠️ 开发

```bash
# 装开发依赖
pip install -e ".[all]"
pip install pytest pytest-asyncio ruff

# 装 pre-commit(可选)
pip install pre-commit
pre-commit install
```

### 项目结构

```
tgmonitor/
├── pyproject.toml
├── README.md
├── LICENSE
├── CHANGELOG.md
├── CONTRIBUTING.md
├── SECURITY.md
├── REVIEW.md                  # 代码 review 报告(每次 sweep 增量)
├── ATTRIBUTIONS.md            # 第三方许可(Lucide 等)
├── .env.example
├── .github/
│   ├── workflows/ci.yml
│   └── ISSUE_TEMPLATE/
├── docs/ARCHITECTURE.md
├── src/tgmonitor/
│   ├── __main__.py            # 入口
│   ├── app.py                 # 组合根 + qasync
│   ├── core/                  # ⚠ 禁 import UI
│   │   ├── config.py          # pydantic Settings
│   │   ├── events.py          # EventBus + 领域事件
│   │   ├── dto.py             # 跨边界 DTO
│   │   ├── app_service.py     # UI 唯一门面
│   │   ├── settings_store.py  # .env 读/写
│   │   ├── telegram/          # TDLib 集成(唯一接触点)
│   │   ├── monitor/           # 监听/去重/落库 (MonitorService + MediaDownloader)
│   │   ├── channel_sync/      # 多选全量同步(元数据 + 历史)
│   │   ├── storage/           # Postgres / Mongo / JSONL 仓储
│   │   ├── objectstore/       # S3 / Local / Folder 对象存储
│   │   └── export/            # JSON / CSV / Markdown / HTML
│   ├── ui/                    # PySide6
│   │   ├── main_window.py
│   │   ├── icon.py            # SVG → QIcon (Lucide 风格)
│   │   ├── viewmodels/
│   │   ├── widgets/           # AccountWidget / ChannelWidget / dialogs / MessageView
│   │   └── resources/         # QSS 主题
│   └── resources/             # app icon + toolbar SVGs(importlib.resources)
└── tests/                     # 130+ 用例,全离线
```

---

## 🤝 贡献

欢迎 PR!请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。
请勿在 issue / PR 中粘贴你的 `TG_API_ID` / `TG_API_HASH` / 验证码 / session 文件。

---

## 🔐 安全

本应用会保存你的 Telegram 用户 session 文件到 `TG_SESSION_DIR`(默认 `./data/session`)。
请妥善保管本机访问,不要把 session 目录提交到 git。

漏洞报告见 [SECURITY.md](SECURITY.md)。

---

## 📄 许可证

[MIT](LICENSE)

---

## 🙏 致谢

- [TDLib](https://github.com/tdlib/td) — Telegram 官方客户端库
- [aiotdlib](https://github.com/pylakey/aiotdlib) — TDLib 的 Python asyncio 封装
- [PySide6](https://www.qt.io/qt-for-python) · [qasync](https://github.com/CabbageDevelopment/qasync)
- [asyncpg](https://github.com/MagicStack/asyncpg) · [motor](https://github.com/mongodb/motor) · [aioboto3](https://github.com/terricain/aioboto3)
- [Lucide](https://lucide.dev/) — 工具栏 / 频道类型图标(ISC 许可),见 [ATTRIBUTIONS.md](ATTRIBUTIONS.md)
