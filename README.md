# 📡 tgmonitor

> Telegram 频道监听桌面应用 — 监听 / 保存 / 导出,UI 与 core 彻底分离。

[![CI](https://github.com/nemo1991/telegram-channel-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/nemo1991/telegram-channel-monitor/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

基于 [TDLib](https://github.com/tdlib/td) 官方库的 Telegram 频道监听桌面应用。通过用户账号登录,
监听白名单频道的新消息,自动落库并下载媒体;内置 PySide6 桌面 UI,支持查看、检索与
JSON / CSV / Markdown / HTML 导出。

---

## ✨ 特性

- 🧩 **UI 与 core 严格分离** — UI 只依赖 `AppService` 门面 + `EventBus` + DTO
- 🗄️ **多数据库后端** — PostgreSQL / MongoDB / JSONL(默认,无需 DB 服务)
- 📦 **多对象存储后端** — 本地平铺 / 本地两级分片(默认) / S3(AWS / MinIO / 阿里 OSS)
- 🔌 **官方 TDLib 集成** — 自编译 libtdjson + ctypes 绑定(`tdlib_json` 子项目,零第三方依赖)
- 📤 **多格式导出** — JSON / CSV / Markdown / HTML,后台流式,大消息量不占内存
- 🔁 **自动重连** — 监听遇错指数退避后自动重订阅
- ⚙️ **运行时热重载** — 设置对话框改后端 / 代理 / 媒体策略,无需重启
- 🧪 **全离线可测** — 4 套抽象接口 + fake 实现,core 完全可脱网单测

---

## 🚀 快速开始

需要 [uv](https://github.com/astral-sh/uv)(包管理 + 自动下载 Python 3.13):

```bash
git clone https://github.com/nemo1991/telegram-channel-monitor.git
cd tgmonitor
uv sync --all-extras        # 含 PostgreSQL / MongoDB / S3 后端(可选)
cp .env.example .env        # 填下面的 3 个变量
uv run tgmonitor
```

最小配置(凭据在 [my.telegram.org/apps](https://my.telegram.org/apps) 申请):

```env
TG_API_ID=123456
TG_API_HASH=abcdef0123456789abcdef0123456789
TG_PHONE=+8613800000000
```

启动后:左栏「账户」填凭据并登录,「频道」双击订阅 / 退订,右侧实时消息流。
国内网络需代理:设置 → 网络代理,填 `socks5://user:pass@host:port` → 测试连接 → 保存并应用。

---

## ⚙️ 配置(环境变量)

| 变量 | 说明 |
|---|---|
| `TG_API_ID` / `TG_API_HASH` | Telegram 应用凭据([my.telegram.org](https://my.telegram.org/apps)) |
| `TG_PHONE` | 登录手机号(含国家区号) |
| `TG_DB_BACKEND` | `postgres` \| `mongo` \| `jsonl`(默认,无需 DB 服务) |
| `TG_DB_DSN` | 数据库连接串 |
| `TG_OBJECTSTORE_BACKEND` | `local` \| `folder`(默认,两级分片) \| `s3` |
| `TG_OBJECTSTORE_ROOT` | 本地对象存储根目录 |
| `TG_OBJECTSTORE_*` | S3 endpoint / region / key / bucket |
| `TG_MEDIA_POLICY` | `metadata` \| `thumbnail`(默认) \| `full`(元数据+缩略图+原文件) |
| `TG_PROXY` | SOCKS5 代理,如 `socks5://user:pass@127.0.0.1:1080` |

所有数据与 `.env` 存于 OS 标准用户数据目录,见 [数据目录](#-数据目录)。

---

## 🏗️ 架构

```
UI (PySide6)
  │  只依赖:AppService 门面 + EventBus + DTO
  ▼
core(领域服务)
  │  只依赖:StorageRepository / ObjectStore / TelegramClient / Exporter 四套抽象
  ▼
存储(postgres/mongo/jsonl) · 对象存储(local/folder/s3) · 导出(json/csv/markdown/html)
```

- **`core/` 禁止 import PySide6** — 领域逻辑与 UI 解耦,可离线单测
- **跨边界只传 DTO**,不传 TDLib / ORM / Qt 对象
- **四套可插拔抽象** — 新增存储 / 对象存储 / 导出 / Telegram 后端 = 加一个实现类 + 注册

详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 🖥️ 平台支持

| 平台 | 状态 |
|---|---|
| **Linux**(Ubuntu 22.04+ / Debian 12+ / Fedora 39+) | ✅ CI 验证 |
| **macOS 12+**(Intel + Apple Silicon) | ✅ CI 验证 |
| **Windows 11 + WSL2** | ✅(在 Ubuntu 里跑,体验同 Linux) |
| **Windows 原生 x64** | ✅ CI 验证(vcpkg 编译 libtdjson,见 [CONTRIBUTING.md](CONTRIBUTING.md#windows-原生编译)) |

TDLib 引擎 `libtdjson` 自编译(锁定 TDLib 1.8.46,首次 clone 后需跑一次;二进制被
gitignore):macOS / Linux 用 `scripts/build_libtdjson.sh`,Windows 用
`scripts/build_libtdjson.ps1`(vcpkg 方式)。

---

## 🗂️ 数据目录

| 平台 | 路径 |
|---|---|
| **macOS** | `~/Library/Application Support/tgmonitor/` |
| **Linux** | `$XDG_DATA_HOME/tgmonitor/`(默认 `~/.local/share/tgmonitor/`) |
| **Windows** | `%APPDATA%/tgmonitor/` |

```
tgmonitor/
├── .env                   # 配置
├── session/               # TDLib session
├── messages/              # JSONL 存储(默认后端)
└── media/                 # 本地对象存储(local / folder)
```

---

## 📥 下载安装

到 [Releases 页面](https://github.com/nemo1991/telegram-channel-monitor/releases) 下载,每个 release 附 `SHA256SUMS`:

| 平台 | 文件 |
|---|---|
| **Linux x86_64** | `tgmonitor-x86_64.AppImage`(`chmod +x` 后直接运行) |
| **macOS 13.0+** | `tgmonitor.app.zip`(解压拖进 `/Applications`) |
| **Windows 10/11 x64** | `tgmonitor-windows-x64.zip`(解压后运行 `tgmonitor.exe`) |

- **macOS 首次启动**:`.app` 未签名会被 Gatekeeper 拦截 → 「系统设置 → 隐私与安全性 → 仍要打开」
- **Linux**:缺 Qt 系统库报 `could not load Qt platform plugin "xcb"` 时:
  `sudo apt-get install -y libegl1 libgl1 libxkbcommon0 libxcb-cursor0`
- **Postgres(2026-09-03 v1.6.0 PR #Q1)**:搜索加速用 `pg_trgm` 扩展。
  首次部署**需 superuser** 手动授权:
  ```sql
  CREATE EXTENSION pg_trgm;
  ```
  heroku / GCP Cloud SQL / 阿里 RDS / AWS RDS 默认允许;self-hosted
  PostgreSQL 需手动跑一次。`init_schema()` 检测到无权限会 log
  warning + 跳过 GIN 索引创建,`LOWER LIKE` 全表扫仍可用(只是慢)。

---

## 🧪 测试与开发

```bash
uv sync --all-groups --all-extras   # 全量 dev 依赖
PYTHONPATH=src QT_QPA_PLATFORM=offscreen uv run pytest   # 全部测试(含 UI,全离线)
uv run ruff check src tests packages                     # lint
```

开发 / 提交规范、新增后端步骤、代理与 TDLib 调试见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 🔐 安全

session 文件与 `.env` 等同账号密码,请勿提交进 git,也不要粘贴到 issue / PR。
漏洞报告见 [SECURITY.md](SECURITY.md)。

---

## 📄 许可证

[MIT](LICENSE) · 第三方致谢见 [ATTRIBUTIONS.md](ATTRIBUTIONS.md)
