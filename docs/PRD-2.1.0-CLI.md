# PRD 2.1.0 — 完备 CLI 工具

> 状态：**设计稿，待评审**。本文只描述设计，不含实现代码。
> 目标版本：2.1.0（当前 main = `a9b88f1` = v2.0.11）。
> 编写日期：2026-11-24。

## 1. 目标与范围

### 1.1 一句话目标

把 `tgmonitor` 的 CLI 从「两个补漏子命令（sync / monitor）」升级为**可替代桌面 UI 大部分功能的完备命令行工具**——能在无图形环境（服务器、SSH、CI、cron、systemd）下完成登录、状态查询、频道管理、消息/媒体读写、导出、配置管理。

### 1.2 为什么要做

| 现状 | 缺口 |
|---|---|
| `sync` / `monitor` 未登录时只会提示「请先在 GUI 完成登录」 | CLI **无法自助登录**，服务器场景直接卡死 |
| 桌面 UI 的收藏/标签/备注/批量/媒体下载/导出能力，core 层**全部已实现** | CLI 一律没有入口，用户被迫开 GUI |
| 配置只能靠设置页或手工编辑 `.env` | 无脚本化配置手段，批量部署困难 |
| 退出码 130 只在 docstring 承诺，monitor 的 SIGINT 实际返回 0 | 脚本无法可靠判断中断 |

### 1.3 范围内（本期做）

1. **会话**：`status` / `login`（headless 二维码）/ `logout`
2. **频道**：`channels list|refresh|add|remove`
3. **监听控制**：`monitor`（现状增强）/ `pause` / `resume`
4. **同步**：`sync`（保持现状，纳入体系统一约定）
5. **消息**：`messages list|get|favorite|tag|note|delete`
6. **媒体**：`media list|download|download-all|retry|delete|reconcile`
7. **导出**：`export messages|media`
8. **配置**：`config show|get|set|validate`
9. **基础设施**：统一退出码、统一 `--json` 输出、统一错误与进度通道、单一命令注册表

### 1.4 范围外（本期明确不做）

- 任何 GUI 视觉改动（导航、状态栏、设置页布局）—— v2.0.11 刚发版，稳定优先。
- 新的 core 领域能力：全部命令**只组装既有 core 服务**，不新增 service 方法（受 `tests/test_app_service_surface.py` allowlist 约束）。
- `open_media` / `reveal_in_folder` / `copy_media_path` / Lightbox 预览——依赖 Qt callback 注入，headless 无意义。
- 消息批量操作（forward / pin / reaction / mark-read）—— `BatchService` 虽有，但先做单条 CRUD；批量留作 2.1.x 增量。
- 守护进程化（daemon / systemd unit 文件随包分发）—— 只提供用法文档，`monitor` 保持前台进程由外部 supervisor 管理。

---

## 2. 硬性架构约束（不可违反）

这些是项目既有铁律，违反会直接触发守卫测试失败：

1. **CLI 路径不加载 Qt**：`cli/` 下任何模块禁止 import `PySide6` / `qasync` / `tgmonitor.ui.*` / `tgmonitor.app`。守门测试 `tests/test_cli.py::test_cli_does_not_import_qt_or_qasync` 用子进程扫描 `sys.modules`。
2. **复用组合根**：需要后端能力的命令走 `core.runtime.bootstrap()` / `shutdown()`，禁止在 cli 层自行装配 storage / client / objectstore。
3. **只调域服务**：UI 直调 `app.auth/channels/messages/media/batch/channel_sync`，CLI 同理；**不得给 AppService 新增转发方法**（allowlist 锁死）。
4. **跨线程/异步桥**：CLI 是纯 asyncio 环境（`asyncio.run`），不走 `ui/async_bridge.py`，但**必须** await EventBus 的异步订阅者（`bus.subscribe_all` 要求 async，`await bus.flush()`）。
5. **配置路径单源**：`.env` 路径一律取 `_user_data_dir()/".env"`（由 `Settings.model_config` 与 `runtime.bootstrap` 解析），禁止 cwd 相对路径。
6. **凭据与 token**：二维码 URL 含登录 token，**只写 stderr / 终端二维码，不进日志、不进 `LoginStateChanged.detail`、不写活动记录**。
7. **破坏性操作必须显式确认**：delete / reconcile --apply / config set 一律要求 `--yes`，并提供 `--dry-run`（reconcile 默认 dry-run）。

---

## 3. 命令体系总览

```
tgmonitor                              # 默认 → 桌面 GUI（不变）
tgmonitor gui                          # 显式 GUI（不变）
tgmonitor --help | -h                  # 顶层帮助
tgmonitor <cmd> --help                 # 子命令帮助

会话与状态
  status [--json]                      # 登录/连接/暂停状态 + 统计 + 后端
  login  [--show-url] [--no-save-creds]# headless 二维码登录
  logout [--yes]                       # 登出（清 session）

频道
  channels list  [--source joined|monitored|subscribed] [--json]
  channels refresh [--json]            # 从 TG 拉取已加入频道并落库
  channels add   <id>... [--yes]       # 加入监听白名单
  channels remove <id>... [--yes]      # 移出监听白名单

监听控制
  monitor [--no-resume]                # 前台监听（现有，SIGINT/SIGTERM 干净退出）
  pause   [--json]                     # 暂停监听（写 TG_PAUSED）
  resume  [--json]                     # 恢复监听（写 TG_PAUSED）

同步
  sync <id>... [--no-metadata] [--no-history] [--resume]
              [--media-policy metadata|thumbnail|full]
              [--chat-delay-ms N] [--page-delay-ms N]   # 现有

消息
  messages list     [--channel ID] [--search TEXT] [--from DATE] [--to DATE]
                    [--limit N] [--favorite] [--tag TAG] [--pinned]
                    [--include-unmonitored] [--json]
  messages get      <channel> <msgid> [--json]
  messages favorite <channel> <msgid> [--off]
  messages tag      <channel> <msgid> --tags a,b,c
  messages note     <channel> <msgid> --text "..."
  messages delete   <channel> <msgid>... [--yes]

媒体
  media list         [--channel ID] [--status S] [--type T] [--search TEXT]
                     [--undownloaded] [--sort date|size|status] [--dir asc|desc]
                     [--limit N] [--offset N] [--json]
  media download     <channel> <msgid> <idx>
  media download-all [--channel ID] [--type T] [--yes]
  media retry        <channel> <msgid> <idx>
  media delete       <channel> <msgid> <idx> [--yes]
  media reconcile    [--apply] [--yes]     # 默认 dry-run

导出
  export messages --format json|csv|markdown|html|zip -o PATH
                  [--channel ID...] [--from DATE] [--to DATE]
                  [--include-thumbnails] [--no-metadata] [--single MSGID]
  export media    --format csv|zip -o PATH [--channel ID] [--status S]
                  [--type T] [--search TEXT] [--include-thumbnails]

配置
  config show      [--json]             # 有效配置，敏感项打码
  config get       <KEY>
  config set       <KEY> <VALUE> [--no-validate] [--yes]
  config validate                       # 校验存储/对象存储后端可达
```

---

## 4. 全局约定

### 4.1 退出码（统一）

| 码 | 语义 | 适用 |
|---|---|---|
| 0 | 成功；monitor 收到 SIGINT/SIGTERM 后干净退出 | 全部 |
| 1 | 业务错误：未登录/未 ready、校验失败、后端不可达、操作失败、FLOOD_WAIT 退避 | 全部 |
| 2 | 命令行用法错误（argparse 默认行为） | argparse |
| 130 | 用户主动中断（KeyboardInterrupt），**monitor 例外**（其信号处理器走 0） | 除 monitor 外全部 |

> 注：现状 `cli/main.py` docstring 宣称 130 但无测试覆盖，monitor 的 SIGINT 走 0。本期保持 monitor=0 的既成行为，其余命令的 KeyboardInterrupt 统一交给 `__main__.main()` 返回 130，并补测试。

### 4.2 输出契约（stdout / stderr / --json）

| 通道 | 内容 |
|---|---|
| **stdout** | **只有数据**：命令结果（文本表格或 JSON）。可被 `> out.json` / 管道安全消费。 |
| **stderr** | 进度事件、错误详情、安全提示、二维码 URL。 |
| `--json` | 所有查询/状态类命令统一支持，输出**稳定的 JSON schema**（见 §5 各命令）。写入类命令输出 `{"ok": true, ...}`。 |

规则：

- 进度事件（ChannelSyncProgress / BatchProgress / ExportProgress / MediaDownloadProgress）**一律 stderr**，格式 `[progress] ...`，不污染 stdout。
- 错误一律 stderr，格式 `error: <可读原因>`；退出码 1。
- JSON 序列化统一入口：DTO 用 `dataclasses.asdict()`，datetime 用 `isoformat`，枚举取 `.value`；由 `cli/_common.py` 的 `emit_json()` 集中处理，避免各命令各写一套。

### 4.3 配置与路径

- `.env` 路径 = `_user_data_dir()/".env"`（macOS `~/Library/Application Support/tgmonitor/.env`）。
- `config` 命令的 KEY 用 **Settings 字段名**（`api_id`、`media_policy`、`db_backend`…），不用 `TG_` 前缀；帮助里列全量 key。
- 写盘链路见 §6.2；`config set` 不直接改 `.env`。

### 4.4 上下文分层（实现基础设施）

新增 `cli/_common.py` 提供统一模板，消除现有 sync/monitor 里的重复 boilerplate：

```python
# 概念示意，非最终代码
@asynccontextmanager
async def cli_context(*, need_backend: bool = True):
    """need_backend=True 走 bootstrap+ready 检查+shutdown；False 只读 Settings/.env。"""
    ...

def emit(data, *, as_json: bool) -> None: ...       # stdout 统一出口
def progress(msg: str) -> None: ...                 # stderr 统一出口
class CliError(Exception): ...                      # 业务错误，main 捕获→stderr+rc1
```

- `need_backend=False` 用于 `config show/get`、`--help` 等纯配置读取：调 `Settings.load_tolerant()`，**不建 storage/client**，启动更快且不受后端故障影响。
- `need_backend=True` 用于其余全部命令：`runtime.bootstrap()` → `app.bootstrap()` 检查 `state == "ready"`，否则 `CliError("请先执行 tgmonitor login")` → `finally: shutdown()`。
- 现有 `sync.py` / `monitor.py` 的 `app= / monitor=` DI 入参保留（测试注入 fakes），`cli_context` 兼容该注入。

---

## 5. 命令详细设计

### 5.1 status

**数据源**：`app.auth.get_login_state()`、`app.is_paused`、`app.channels.list_monitored_channels()`、`app.storage.count_tg_subscribed()`、`count_messages()` / `count_media()`、`app.settings`。

**文本输出**（分组块）：

```
登录: ready
监听: 运行中 (3/5 频道已监听)
后端: sqlite (~/Library/Application Support/tgmonitor/tgmonitor.db)
对象存储: folder (~/Library/Application Support/tgmonitor/media)
统计: 消息 12,480 · 媒体 3,012 (已下载 2,887)
```

**JSON schema**：
```json
{
  "login_state": "ready",
  "paused": false,
  "monitor": {"monitored": 3, "subscribed": 5},
  "backends": {"storage": "sqlite", "objectstore": "folder"},
  "stats": {"messages": 12480, "media": 3012, "media_done": 2887}
}
```
- 未 ready 时**不报错**，仍输出状态（登录态为实际值），退出码 0——status 是诊断命令。
- 统计计数走 `count_messages(channel_id)` 逐频道累加（无全局 count），频道多时可接受；`aggregate_per_channel` 可复用于 per-channel 明细（可选 `--channels` 展开）。

### 5.2 login / logout

**login 流程**（核心，headless）：

1. 读 `api_id` / `api_hash`（Settings）；缺失 → `error: 请先 config set api_id / api_hash` → rc 1。
2. 若用户在命令行传了 `--api-id/--api-hash`，**必须在 bootstrap 之前** `update_env_credentials()` 只写这 2 个 key（**不覆盖设置页其它未保存字段**）——否则组合根会用缺凭据构造 `UnconfiguredTelegramClient`，`start_qr_login()` 必返回 error。与 GUI 登录按钮语义一致，但时序更早。
3. `state, qr_url = await app.auth.start_qr_login()`。
   - `state != "qr_required"` 或 `qr_url` 为空 → `error: <detail>` → rc 1。
4. 渲染二维码到 **stderr**：默认 `segno.make(qr_url, error="m").terminal(out=sys.stderr)`（终端 Unicode 二维码，无需图像/Qt）；`--show-url` 时追加打印 URL 并给出安全提示。
5. `await app.auth.wait_qr_login()` 阻塞等待扫码（Telethon 内部按二维码有效期自动截止）。
   - 注册 SIGINT handler：Ctrl+C → 取消等待 → rc 130。
   - 超时返回 `("error", "二维码已过期…")` → 提示重新执行 `login` → rc 1（本期不做自动刷新，降低复杂度）。
6. `state == "ready"` → `ok: 登录成功` → rc 0；否则 rc 1。

**风险提示**：二维码 URL 是一次性登录 token，等价于临时凭据。`--show-url` 仅在确认终端不被录制/转发时使用。

**logout**：`await app.auth.logout()` → 清 session → rc 0。要求 `--yes`（不可逆）。登出后状态回到 `phone_required`。

### 5.3 channels

| 子命令 | 实现链路 | 输出 |
|---|---|---|
| `list --source joined` | `app.channels.list_joined_channels()`（连 TG，未 ready 返回 `[]`） | 表格：`ID / 标题 / username / 成员数 / 已监听` |
| `list --source monitored`（默认） | `app.channels.list_monitored_channels()`（storage） | 同上，`已监听=是` |
| `list --source subscribed` | `app.channels.list_tg_subscribed_channels()`（storage，不连 TG） | 同上 |
| `refresh` | `app.channels.refresh_joined_channels()` | 新增/更新/退订数量汇总；发 `JoinedChannelsRefreshed` |
| `add <id>...` | 对每个 id：`storage.get_channel(id)` → `app.channels.monitor_channel(ch)`；不存在则报错列出 | `added=N, skipped=M` |
| `remove <id>...` | `app.channels.unmonitor_channel(id)`（幂等） | `removed=N` |

- `add/remove` 要求 `--yes`；任一 id 失败不回滚（逐条处理并汇总），退出码 1。
- `list` 支持 `--json`，schema 为 `ChannelDTO` 数组（字段见 `core/dto.py:18`）。

### 5.4 monitor / pause / resume

- `monitor`：沿用现有 `run_monitor()`；从 storage 重载白名单；`--no-resume` 忽略 `TG_PAUSED`。
- `pause`：`await app.pause_monitor(source="cli")` → monitor.stop + client.stop + 写 `TG_PAUSED=true` + 发 `MonitoringPaused`。
- `resume`：`await app.resume_monitor(source="cli")` → client.start + monitor.start + 写 `TG_PAUSED=false` + 发 `MonitoringResumed`；client.start 失败则 rc 1 且**不改 .env**（与 GUI 一致）。
- 三者都输出 `{"paused": bool}` / 文本状态，`pause/resume` 支持 `--json`。
- **部署**：monitor 是前台进程，由 systemd / supervisord / Docker 托管；本期不随包分发 unit 文件，仅在文档给出示例（§10）。

### 5.5 sync

保持现状（已有完整测试覆盖）。仅做两点统一：进度输出改走 `cli/_common.py::progress()`；FLOOD_WAIT 警告仍 rc 1。

### 5.6 messages

| 子命令 | 实现链路 | 备注 |
|---|---|---|
| `list` | `app.messages.list_messages(...)`（参数 1:1 映射） | `--from/--to` 解析 `YYYY-MM-DD` → UTC datetime；`--favorite/--tag/--pinned` 映射对应 only 参数 |
| `get <c> <m>` | `app.storage.get_message(c, m)` | 输出完整 MessageDTO（含 media 列表），`--json` |
| `favorite <c> <m> [--off]` | `app.messages.set_favorite(c, m, not off)` | — |
| `tag <c> <m> --tags a,b,c` | `app.messages.set_tags(c, m, [...])` | 空串 `""` 清空 |
| `note <c> <m> --text "..."` | `app.messages.set_notes(c, m, text)` | — |
| `delete <c> <m>... [--yes]` | `app.messages.delete_message(c, m)` 逐条（需 monitor 非 None） | 要求 `--yes` |

- `list --json` schema：`MessageDTO` 数组；文本模式表格列 `频道 / 时间 / ID / 类型 / 摘要`，支持 `--limit`（默认 200）。
- `messages delete` 的 `<msgid>...` 为同一频道下多条消息，`<c>` 只写一次。

### 5.7 media

| 子命令 | 实现链路 | 备注 |
|---|---|---|
| `list` | `app.media.list_media(...)` | 1:1 映射查询参数；返回 `(rows, total)`，文本输出附 `共 N 条`；`--json` 输出 `{items, total}` |
| `download <c> <m> <i>` | `app.media.download_media(c, m, i)`（force=False） | — |
| `download-all` | `app.media.download_all_pending(...)` | 返回发起数；要求 `--yes` |
| `retry <c> <m> <i>` | `app.media.retry_media(c, m, i)`（force=True） | — |
| `delete <c> <m> <i> [--yes]` | `app.media.delete_media(c, m, i)` | — |
| `reconcile [--apply] [--yes]` | `app.media.reconcile_orphans(dry_run=not apply)` | **默认 dry-run**，输出预览；`--apply` 要求 `--yes` |

- `--status` 取值 `downloading|done|failed`；`--undownloaded` 映射 `undownloaded=True`（UI 用 `UNDOWNLOADED_FILTER` 的那个语义）。
- 媒体行的三元组 `(channel_id, telegram_msg_id, media_idx)` 从 `media list --json` 输出中可直接取到（输出含这三个字段）。

### 5.8 export

- `export messages`：构造 `ExportRequest`（channel_ids / date_from / date_to / format / out_path / include_thumbnails / include_metadata / selected_messages / single_message_id）→ `async for _ in app.export(req)` 驱动进度 → 订阅 `ExportDone` 取结果。
  - 输出路径 `-o PATH` 必填；无后缀按格式补（`json/csv/md/html/zip`）。
  - `markdown`/`html`/`csv` 默认 `include_metadata=True`，`--no-metadata` 关闭；`zip`/`html` 支持 `--include-thumbnails`。
- `export media`：
  - `--format csv` → `MediaExportRequest` → `app.export_media_list(req)`（走同一 `export` 生成器）。
  - `--format zip` → `ExportRequest(format=ZIP, channel_ids=[...], include_thumbnails=...)`。
- 成功输出 `{"out_path": ..., "message_count": N, "bytes_written": B}`，rc 0；进度走 stderr。

### 5.9 config

| 子命令 | 行为 |
|---|---|
| `show [--json]` | `Settings.load_tolerant()` → 输出全量字段；`api_hash` / objectstore 密钥打码为 `****`；附 warnings（load_tolerant 收集的中文告警） |
| `get <KEY>` | 输出单值（原样，敏感项打码）；key 不存在 → rc 1 + 列出可用 key |
| `set <KEY> <VALUE> [--no-validate] [--yes]` | 见 §6.2；要求 `--yes` |
| `validate` | `app.validate_backends(new_settings=当前 Settings)` → 逐后端 `connect+init_schema+close` / `connect+close`，输出可达性；失败 rc 1 |

> `config set` 的 KEY 支持全部 Settings 字段；`paused` **不在**此列（用 `pause`/`resume`），`db_root` 为遗留字段不接受设置。

---

## 6. 关键流程

### 6.1 headless 二维码登录时序

```
用户          cli/login.py        AuthService      TelethonClient      Telegram
 │                │                   │                  │                │
 │─login──────────▶│                   │                  │                │
 │                │─start_qr_login()─▶│                  │                │
 │                │                   │─start_qr_login()▶│                │
 │                │                   │                  │─qr_login()────▶│
 │                │◀──(qr_required, url)─────────────────│◀───url──────────│
 │◀─二维码(stderr)─┤  segno.terminal(url)                 │                │
 │                │─wait_qr_login()───▶│                  │                │
 │  手机扫码授权 ────────────────────────────────────────────────────────▶│
 │                │                   │◀─(ready,...)──────│◀──UpdateLoginToken─│
 │◀─ok rc0────────┤                   │                  │                │
```

要点：`qr_login()` 必须 await（历史坑）；`await qr.wait()` 由 Telethon 内部按过期时间截止；token 只在返回值 `detail` 里，不进事件/日志。

### 6.2 配置写入链路（config set）

```
读当前 Settings
   → EditableSettings.from_settings()
   → 改字段 → validate()              # api_id>0 / api_hash≥16 / 枚举 / delay 范围 / proxy 仅 socks5
   → validate_backends(new)           # 除 --no-validate；失败即中止，不写盘
   → reconfigure(new)                 # 热重载（diff 无变化则提前 return）
   → update_env_with_settings(env_path, new)   # 保形写 .env（.part + rename）
   → emit({"ok": true, "key":..., "value":...})
```

与 GUI 设置页**完全同一链路**（`settings_page.py:883` 先 reconfigure 成功才写盘）。绝不直接写 `.env` 绕过 reconfigure，否则运行时与配置不一致。`needs_relogin=True`（api_id/api_hash 变更）时输出提示「请重新 login」。

### 6.3 进度与事件

统一在 `cli/_common.py` 提供 `attach_progress(bus, *, show: bool)`：订阅 `ChannelSyncProgress` / `ExportProgress` / `BatchProgress` / `MediaDownloadProgress` 的 async 回调 → `progress()` 写 stderr；命令结束 `await bus.flush()`。各命令不再自己写订阅样板。

---

## 7. 模块与文件结构

```
src/tgmonitor/cli/
  __init__.py           # 保持
  main.py               # build_parser 扩展全部子命令；CLI_SUBCOMMANDS 单一真源
  _common.py            # 新增：cli_context / emit / emit_json / progress / CliError / attach_progress / 日期解析
  sync.py               # 现有，接入 _common
  monitor.py            # 现有，接入 _common
  status.py             # 新增
  auth.py               # 新增：login / logout
  channels.py           # 新增
  messages.py           # 新增
  media.py              # 新增
  export.py             # 新增
  config.py             # 新增
src/tgmonitor/__main__.py   # 分流改为从 cli.main 读 CLI_SUBCOMMANDS，消除白名单双写
tests/
  test_cli.py           # 扩展：全子命令 --help / 不加载 Qt / 分发路由 / 退出码
  test_cli_status.py    # 新增
  test_cli_auth.py      # 新增：headless QR（fake client + segno 打桩）
  test_cli_channels.py  # 新增
  test_cli_messages.py  # 新增
  test_cli_media.py     # 新增
  test_cli_export.py    # 新增
  test_cli_config.py    # 新增：临时 .env，绝不碰用户真实配置
```

每个命令文件遵循现有 DI 约定：`async def run_xxx(args, *, app=None, monitor=None) -> int`，便于测试注入 fakes。

---

## 8. 分阶段落地计划

| 阶段 | 内容 | 交付 | 依赖 |
|---|---|---|---|
| **P0 基础设施** | `cli/_common.py`、退出码统一、`--json` 框架、`__main__` 单一注册表、sync/monitor 接入 | 1 个 PR | 无 |
| **P1 会话** | `status` / `login`（headless QR）/ `logout` | 1 个 PR | P0 |
| **P2 频道与监听** | `channels` / `pause` / `resume` | 1 个 PR | P0 |
| **P3 消息与媒体** | `messages` / `media` | 1–2 个 PR | P0 |
| **P4 导出与配置** | `export` / `config` | 1 个 PR | P0 |
| **P5 文档与发版** | README CLI 章节、CHANGELOG、版本号 6 处 bump、tag | 1 个 PR | P0–P4 |

- 每个 PR 独立跑全套 pytest + ruff check + `ruff format --check src tests` + mypy，CI 8/8 全绿后 squash merge。
- 生产行为 / 依赖大版本 / 发版 / 删数据，按项目约定先与用户确认；低风险改动走完整链路自主完成。
- `main` 禁直推；分支 `<type>/<kebab>`；提交 `type(scope): subject`；`.workbuddy/` 不提交。

---

## 9. 测试策略

1. **守门**：`test_cli.py::test_cli_does_not_import_qt_or_qasync` 的扫描列表扩展到所有新 cli 模块（子进程 import 全部命令模块，断言无 `PySide6*` / `qasync` / `tgmonitor.app` / `tgmonitor.ui.*`）。
2. **路由**：`CLI_SUBCOMMANDS` 单一真源后，参数化遍历所有子命令的 `--help`（rc=0，stdout 含命令名）。
3. **DI 注入**：每个命令用 `run_xxx(args, app=app, monitor=monitor)` 注入 conftest 的 fakes，不真连后端。
4. **退出码**：参数化断言 0/1/2/130（130 用 KeyboardInterrupt 打桩）。
5. **JSON 契约**：每个 `--json` 命令断言可 `json.loads` 且关键字段存在、类型稳定。
6. **登录**：fake client 的 `start_qr_login` 返回固定 URL，`segno` 生成打桩，断言 URL 只到 stderr、不进日志、rc=0/1 分支。
7. **配置**：`config set` 用 `tmp_path/".env"`，断言落盘值 + reconfigure 被调用；**绝不读写用户真实配置**；打码断言敏感字段不出现在 stdout。
8. **危险操作**：delete / reconcile --apply / config set 无 `--yes` 时拒绝执行（rc=2 或明确提示）。

---

## 10. 风险提示与部署建议

### 10.1 风险提示

- **token 泄露**：登录二维码 URL 是一次性 token。`--show-url` 会把它打到终端——只在本地可信终端使用，不要重定向到日志文件或共享屏幕录制。
- **配置写坏**：`config set` 会热重载并写盘。错误的 `db_backend` / `objectstore_*` 可能导致后续命令不可用；`config validate` 可在写入前检查后端可达，`--no-validate` 需谨慎。
- **未登录阻塞**：除 `status` / `config` / `login` 外，所有命令未 ready 时 rc=1 并提示 `tgmonitor login`。cron 场景需先确保 session 有效。
- **SIGINT 语义差异**：monitor 的 SIGINT=0（daemon 约定），其它命令=130。supervisor 脚本按进程用途判断。
- **FLOOD_WAIT**：sync 遇限流会退避并 rc=1，属预期保护，不要增大 `--chat-delay-ms` 之外的并发。

### 10.2 部署建议（dev / prod）

| 维度 | dev（本地开发） | prod（服务器长驻） |
|---|---|---|
| 配置隔离 | `TG_DATA_ROOT` 指向临时目录，`Settings.for_test()` 思路 | 独立数据目录，`.env` 权限 `600` |
| 会话 | 用 fake client / `for_test`，不真登录 | 先在可信终端 `login` 一次，session 落盘后由 daemon 复用 |
| 监听 | `tgmonitor monitor` 前台，Ctrl+C 即停 | systemd / supervisord 托管，见下方 unit 示例 |
| 风险操作 | `--dry-run` 默认，`--yes` 显式 | 所有 delete / reconcile 审计 stdout/stderr |

**systemd unit 示例**（生产环境，供参考，不随包分发）：

```ini
[Unit]
Description=tgmonitor CLI monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=TG_DATA_ROOT=/var/lib/tgmonitor
ExecStart=/usr/local/bin/tgmonitor monitor
Restart=on-failure
RestartSec=10
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

日志：`journalctl -u tgmonitor -f`；进度事件在 stderr，自动进 journald。

---

## 11. 待评审决策点

请在设计评审时确认以下取舍，确认后进入 P0 开发：

1. **P1 的 `login` 是否加入 `--api-id/--api-hash` 命令行传参**（简化首次部署），还是强制先 `config set`？（建议：加入，但只写这 2 个 key）
2. **批量消息操作**（forward/pin/reaction/mark-read）是否提前到 2.1.0？（建议：不，留 2.1.x）
3. **`status` 的 per-channel 明细**是否默认展开（用 `aggregate_per_channel`），还是仅汇总？（建议：仅汇总，`--channels` 可选展开）
4. **QR 过期自动刷新一次**是否本期做？（建议：不做，提示重跑 `login`，降低复杂度）
5. **退出码 130 的 monitor 例外**是否接受？（建议：接受，保持 daemon 约定）
