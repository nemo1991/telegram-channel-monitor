# 📡 tgmonitor

> Telegram 频道监听桌面应用 — 监听 / 保存 / 导出,UI 与 core 彻底分离。

[![CI](https://github.com/nemo1991/telegram-channel-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/nemo1991/telegram-channel-monitor/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

基于 [Telethon](https://github.com/LonamiWebs/Telethon) 纯 Python MTProto 库的 Telegram 频道监听桌面应用。
通过用户账号登录,监听白名单频道的新消息,自动落库并下载媒体;内置 PySide6 桌面 UI,
支持查看、检索与 JSON / CSV / Markdown / HTML 导出。

> **v2.0.0 重大变化**:Telegram 后端从 TDLib(自编译 libtdjson)换成纯 Python 的 Telethon,
> **无需再编译任何原生引擎**;默认数据库从 PostgreSQL 改为 **SQLite**(单文件零部署),
> MongoDB 后端已移除。从 v1.x 升级需**重新登录**(session 格式不兼容),详见 [CHANGELOG.md](CHANGELOG.md)。

---

## ✨ 特性

- 🧩 **UI 与 core 严格分离** — UI 只依赖 AppService 组合根 + 按域服务 + `EventBus` + DTO
- 🗄️ **多数据库后端** — SQLite(默认,单文件零部署) / PostgreSQL(可选,多设备/大规模)
- 📦 **多对象存储后端** — 本地平铺 / 本地两级分片(默认) / S3(AWS / MinIO / 阿里 OSS)
- 🔌 **纯 Python Telegram 后端** — Telethon MTProto + cryptg 加密加速 + python-socks 代理,零原生编译
- 📤 **多格式导出** — JSON / CSV / Markdown / HTML,后台流式,大消息量不占内存
- 🔁 **自动重连** — 监听遇错指数退避后自动重订阅
- ⚙️ **运行时热重载** — 设置对话框改数据库 / 对象存储后端与代理,无需重启;媒体策略改后需重启
- 🧪 **全离线可测** — 4 套抽象接口 + fake 实现,core 完全可脱网单测

---

## 🚀 快速开始

需要 [uv](https://github.com/astral-sh/uv)(包管理 + 自动下载 Python 3.13):

```bash
git clone https://github.com/nemo1991/telegram-channel-monitor.git
cd telegram-channel-monitor
uv sync --all-extras        # 含 PostgreSQL / S3 后端(可选;默认 SQLite 无需额外依赖)
uv run tgmon                # 首次启动后在「设置 → 账户凭证」填 API ID / Hash,点「二维码登录」
```

凭据在 [my.telegram.org/apps](https://my.telegram.org/apps) 申请。也可以用环境变量(或在**数据目录**里放 `.env`,见下方 [数据目录](#️-数据目录);仓库根目录的 `.env` 不会被读取):

```bash
TG_API_ID=123456 TG_API_HASH=abcdef0123456789abcdef0123456789 uv run tgmon
```

启动后:「设置 → 账户凭证」填好 API ID / Hash → 点「二维码登录」用手机 Telegram App 扫码;
在「设置 → 监听频道管理」里把频道加入 / 移出监听,新消息实时出现在「大盘」,
「消息」「媒体」页可检索与导出。
国内网络需代理:设置 → 网络代理,填 `socks5://user:pass@host:port` → 测试连接 → 保存。

---

## 💻 CLI(无 GUI 模式)

2.1.0 起 CLI 具备完整功能,可替代桌面 UI 的大部分操作,适用于服务器 / SSH / cron /
systemd。CLI 路径不加载 Qt,冷启动快;与 GUI 共用同一套配置与数据库。

2.1.2 起命令彻底分离:**`tgmon` 只启动桌面界面,所有命令行操作走 `tgmon-cli`**
(从进程起点就不加载 Qt,冷启动快)。误把 CLI 子命令传给 `tgmon` 只会报用法错误,
不会再弹窗口。

```bash
uv run tgmon-cli help                  # 帮助总览
uv run tgmon-cli <command> --help      # 查看某命令的完整参数
```

**会话与状态**

```bash
uv run tgmon-cli status --json                         # 登录/监听状态 + 统计
uv run tgmon-cli login --api-id 123 --api-hash xxxx    # headless 二维码登录(二维码直接打到终端)
uv run tgmon-cli logout --yes                          # 登出(清 session)
```

> 二维码 URL 含一次性登录 token,默认只渲染成终端二维码打到 stderr;仅在终端可信时
> 用 `--show-url` 额外打印原文。

**频道 / 监听 / 同步**

```bash
uv run tgmon-cli channels list --source monitored --json   # joined / monitored / subscribed
uv run tgmon-cli channels refresh --yes                     # 从 TG 拉取已加入频道落库(会级联停掉已退出频道的监听)
uv run tgmon-cli channels add 100 200 --yes                 # 加入监听白名单
uv run tgmon-cli channels remove 100 --yes                  # 移出监听
uv run tgmon-cli monitor                                    # 前台监听(SIGINT / SIGTERM 干净退出)
uv run tgmon-cli pause / resume --json                     # 暂停 / 恢复监听(写 TG_PAUSED)
uv run tgmon-cli sync 100 200 --resume --media-policy metadata   # 全量同步历史
```

**消息 / 媒体 / 导出**

```bash
uv run tgmon-cli messages list --channel 100 --search 关键词 --json
uv run tgmon-cli messages get 100 5 --json
uv run tgmon-cli messages favorite 100 5 [--off]
uv run tgmon-cli messages tag 100 5 --tags a,b
uv run tgmon-cli messages note 100 5 --text "备注"
uv run tgmon-cli messages delete 100 5 6 --yes

uv run tgmon-cli media list --undownloaded --json              # 输出含 channel/msg/idx 三元组
uv run tgmon-cli media download 100 5 0
uv run tgmon-cli media download-all --yes
uv run tgmon-cli media retry 100 5 0
uv run tgmon-cli media delete 100 5 0 --yes
uv run tgmon-cli media reconcile                                # 孤儿对账(默认 dry-run)
uv run tgmon-cli media reconcile --apply --yes                  # 实际清理

uv run tgmon-cli export messages --format html -o out.html --channel 100
uv run tgmon-cli export media --format csv -o media.csv
```

**配置管理**

```bash
uv run tgmon-cli config show --json              # 有效配置(敏感项打码)
uv run tgmon-cli config get media_policy
uv run tgmon-cli config set sync_chat_delay_ms 750 --yes   # 校验 + 热重载 + 写 .env
uv run tgmon-cli config validate                  # 校验存储 / 对象存储后端可达
```

**约定**

- 命令名:`tgmon` 仅启动桌面界面(无参数 / `gui`),不接任何 CLI 子命令;
  全部 CLI 操作走 `tgmon-cli <cmd>`,`tgmon-cli help` 查看总览。
  `python -m tgmonitor` 等价 `tgmon`,CLI 也可 `python -m tgmonitor.cli.main`。
- 退出码:`0` 成功 / `1` 业务错误(未登录、校验失败等) / `2` 用法错误 / `130` 用户中断;
  `monitor` 收到 SIGINT 按守护进程约定返回 `0`。
- stdout 只输出数据(可被重定向或管道消费),进度 / 错误 / 二维码走 stderr。
- 查询 / 状态命令统一支持 `--json`。
- 会改变远端或本地状态的写操作必须显式加 `--yes`:`channels add / remove / refresh`、`logout`、
  `messages delete`、`media delete / download-all / reconcile --apply`、`config set`。
- 完整设计见 [`docs/history/PRD-2.1.0-CLI.md`](docs/history/PRD-2.1.0-CLI.md)。

---

## ⚙️ 配置(环境变量)

| 变量 | 说明 |
|---|---|
| `TG_API_ID` / `TG_API_HASH` | Telegram 应用凭据([my.telegram.org](https://my.telegram.org/apps)),二维码登录用 |
| `TG_DB_BACKEND` | `sqlite`(默认) \| `postgres` |
| `TG_SQLITE_PATH` | SQLite 数据库文件路径(默认:数据目录下 `tgmonitor.db`) |
| `TG_DB_DSN` | PostgreSQL 连接串(`postgres` 后端用) |
| `TG_OBJECTSTORE_BACKEND` | `local` \| `folder`(默认,两级分片) \| `s3` |
| `TG_OBJECTSTORE_ROOT` | 本地对象存储根目录 |
| `TG_OBJECTSTORE_*` | S3 endpoint / region / key / bucket |
| `TG_MEDIA_POLICY` | `thumbnail`(默认,元数据侧:元数据+缩略图、不下原文件) \| `full`(媒体本身侧:额外下载原文件) \| `metadata`(元数据侧子集,CLI 兼容保留);设置页对应「同步时下载媒体本身」开关,改后重启生效 |
| `TG_PROXY` | SOCKS5 代理,如 `socks5://user:pass@127.0.0.1:1080` |

所有数据与 `.env` 存于 OS 标准用户数据目录,见 [数据目录](#-数据目录)。

---

## 🏗️ 架构

```
UI (PySide6)
  │  只依赖:AppService 组合根 + 按域服务(auth/channels/messages/media/batch)+ EventBus + DTO
  ▼
core(组合根 + 领域服务)
  │  只依赖:StorageRepository / ObjectStore / TelegramClient / Exporter 四套抽象
  ▼
存储(sqlite/postgres) · 对象存储(local/folder/s3) · 导出(json/csv/markdown/html)
```

- **`core/` 禁止 import PySide6** — 领域逻辑与 UI 解耦,可离线单测
- **跨边界只传 DTO**,不传 Telegram / ORM / Qt 对象
- **四套可插拔抽象** — 新增存储 / 对象存储 / 导出 / Telegram 后端 = 加一个实现类 + 注册

详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 🖥️ 平台支持

| 平台 | 状态 |
|---|---|
| **Linux**(Ubuntu 22.04+ / Debian 12+ / Fedora 39+) | ✅ CI 验证 |
| **macOS 13+**(Intel + Apple Silicon) | ✅ CI 验证 |
| **Windows 11 + WSL2** | ✅(在 Ubuntu 里跑,体验同 Linux) |
| **Windows 原生 x64** | ✅ CI 验证 |

v2.0.0 起 Telegram 后端是纯 Python(Telethon),**三平台均无需编译原生引擎**;
clone 后 `uv sync` 即可跑测试与打包。

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
├── session/               # Telethon session(tgmonitor.session)
├── tgmonitor.db           # SQLite 数据库(默认;messages / media / channels 表)
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
- **PostgreSQL(可选后端)**:搜索加速用 `pg_trgm` 扩展。
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
uv run ruff check src tests                              # lint
```

开发 / 提交规范、新增后端步骤、代理与 Telethon 调试见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 🔐 安全

session 文件与 `.env` 等同账号密码,请勿提交进 git,也不要粘贴到 issue / PR。
漏洞报告见 [SECURITY.md](SECURITY.md)。

---

## 📄 许可证

[MIT](LICENSE) · 第三方致谢见 [ATTRIBUTIONS.md](ATTRIBUTIONS.md)
