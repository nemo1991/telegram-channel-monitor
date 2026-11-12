# 🏗️ Architecture

本文档面向**想深入了解 / 二次开发**的读者。如果你只想用,看 [README.md](../README.md) 即可。

> 本文档描述 **v2.0.0**:Telegram 后端为纯 Python 的 **Telethon**,默认存储为 **SQLite**,
> 门面已拆为**按域服务**。v1.x 的 TDLib / MongoDB / god-facade 设计已废弃。

## 1. 设计目标

1. **UI 与 core 彻底分离** — 可以重写 UI(比如换成 Tauri / web)而 core 不动一行
2. **可替换** — DB / 对象存储 / Telegram 客户端 / 导出格式都是 ABC/Protocol
3. **可测试** — core 100% 离线可测,不依赖 Telegram / 真实 DB / S3
4. **类型安全** — 跨边界全部 DTO,无 `Any` 泄漏
5. **小依赖** — 默认 SQLite + Folder 就能跑,零原生编译、零外部服务

## 2. 分层

```
┌─────────────────────────────────────────────────────────────┐
│ UI (PySide6)                                                │
│   widgets/  viewmodels/  main_window.py                     │
│                                                             │
│   ↓ 只调 AppService 组合根 + 按域服务 + 订阅 EventBus + DTO │
├─────────────────────────────────────────────────────────────┤
│ Composition root (AppService)                               │
│   生命周期: bootstrap / start_monitor / stop_monitor /      │
│             pause / resume / shutdown                       │
│   热重载: reconfigure / validate_backends                   │
│   导出: export / export_media_list                          │
│   域服务: auth / channels / messages / media / batch /      │
│           channel_sync                                      │
├─────────────────────────────────────────────────────────────┤
│ Domain services                                              │
│   AuthService  ChannelService  MessageService  MediaService │
│   BatchService  ChannelSyncService  MonitorService          │
│   ExportService                                             │
├─────────────────────────────────────────────────────────────┤
│ Interfaces (ABC / Protocol)                                 │
│   StorageRepository · ObjectStore · TelegramClient          │
│   Exporter + EXPORTERS registry                             │
├─────────────────────────────────────────────────────────────┤
│ Implementations                                             │
│   SqliteRepository  PostgresRepository                      │
│   LocalObjectStore  FolderObjectStore  S3ObjectStore        │
│   TelethonTelegramClient  FakeTelegramClient                │
│   JsonExporter  CsvExporter  MarkdownExporter  HtmlExporter │
├─────────────────────────────────────────────────────────────┤
│ Infrastructure                                               │
│   Settings (pydantic-settings)  EventBus  DTO  Logging      │
└─────────────────────────────────────────────────────────────┘
```

## 3. 关键模块

### 3.1 `core/dto.py` — 跨边界数据对象

| DTO | 字段(节选) | 用途 |
|---|---|---|
| `ChannelDTO` | id, title, username, kind, member_count, created_at | 频道元信息 |
| `MediaDTO` | type, mime_type, file_name, file_size, w/h/duration, telegram_file_id, object_key, object_backend, thumb_key, thumb_backend | 媒体元数据 + 对象存储引用 |
| `MessageDTO` | id, channel_id, telegram_msg_id, author, date, text, views, forwards, reply_to_msg_id, edited, media[], reactions[], raw | 消息 |
| `ExportRequest` / `MediaExportRequest` | channel_ids[], date_from, date_to, format, out_path, … | 导出请求 |
| `ExportResult` | out_path, message_count, bytes_written | 导出结果 |
| `SyncOptions` / `SyncResult` / `ChannelSyncResult` | 节流 / 续拉 / 进度统计 | 全量同步 |

**约定**:DB 与 ObjectStore 都不在 DTO 内部;DTO 持有 **引用**(`object_key` / `thumb_key`)。

### 3.2 `core/events.py` — EventBus

领域事件继承 `Event`,全 `async` pub/sub(订阅者异常被吞 + 日志,不崩主流程):

| 事件 | 触发时机 |
|---|---|
| `LoginStateChanged` / `ConnectionStateChanged` | 登录状态机 / 连接状态变化 |
| `ChannelDiscovered` / `ChannelSubscribed` / `ChannelUnsubscribed` | 频道枚举 / 订阅 / 退订 |
| `ChannelTitleChanged` / `ChannelPhotoChanged` / `ChannelMetadataChanged` | 频道元信息变更 |
| `MessageReceived` / `MessageEdited` / `MessageDeleted` | 消息落库 / 编辑 / 撤回 |
| `MessagePinChanged` / `MessageInteractionsChanged` | 钉选 / 互动(views/reactions)变更 |
| `MediaDownloaded` / `MediaDownloadProgress` / `MediaDeleted` / `MediaRetried` / `MediaReconcileFinished` | 媒体下载 / 进度 / 删除 / 重试 / 对账 |
| `ExportProgress` / `ExportDone` | 导出进度 / 完成 |
| `BatchProgress` / `BatchDone` | 批量操作进度 / 完成 |
| `ChannelSyncProgress` / `ChannelSyncDone` | 全量同步进度 / 完成 |
| `MonitoringPaused` / `MonitoringResumed` | 监听暂停 / 恢复 |
| `SettingsChanged` | 设置热重载 |
| `ErrorOccurred` / `AuthErrorOccurred` | 子系统错误 / 鉴权错误 |
| `NotificationRequested` / `QuitRequested` | 桌面通知 / 退出请求 |

订阅示例:

```python
async def on_message(e: MessageReceived) -> None:
    print("新消息:", e.message.text)

bus.subscribe(MessageReceived, on_message)
await bus.publish(MessageReceived(message=m))
```

### 3.3 `core/app_service/` — 组合根 + 生命周期(不再是 god facade)

v2.0.0 把 745 行 god facade 拆为**按域服务**。`AppService` 只保留三类职责:

1. **生命周期**(跨子系统协调):`bootstrap` / `start_monitor` / `stop_monitor` /
   `pause_monitor` / `resume_monitor` / `shutdown`
2. **热重载**(全体服务 backend swap):`reconfigure` / `validate_backends`
3. **导出**(每次新建轻量 ExportService):`export` / `export_media_list`

域操作 UI **直调域服务**(`app.<domain>.<method>`),不再经 facade 转发:

| 域服务 | 职责 |
|---|---|
| `app.auth` `AuthService` | 登录鉴权(`get_login_state` / `submit_phone/code/password/email/…` / `logout`) |
| `app.channels` `ChannelService` | 订阅 / 退订 / 白名单 |
| `app.messages` `MessageService` | 消息查询 / 收藏 / 标签 / 备注 / 单删 |
| `app.media` `MediaService` | 媒体列表 / CRUD / 打开 / retry / reconcile |
| `app.batch` `BatchService` | 7 批量 op + 取消(自持 cancel event) |
| `app.channel_sync` `ChannelSyncService` | 手动全量同步(节流 + 续拉) |

```python
# UI 侧使用
state = await app.auth.submit_phone("+86...")
state = await app.auth.submit_code("12345")
await app.channels.subscribe(channel)
async for _ in app.export(req):
    ...  # 进度心跳
```

**表面守护**:`tests/test_app_service_surface.py` 用 allowlist 锁死 `AppService`
的公开方法 / property / 实例属性 — 新增域转发会被守护测试拒绝(域操作应加在域服务上)。
后端持有(`client` / `storage` / `objects` / `downloader` / `monitor`)通过 property
setter 传播给各域服务。

### 3.4 `core/telegram/` — Telethon 集成边界

唯一接触 Telegram 后端的目录。`client.py` 定义 `TelegramClient` Protocol
(`@runtime_checkable`);`telethon_client.py` `TelethonTelegramClient` 是真实实现,
`fake_client.py` 是测试桩,`unconfigured.py` 是凭据缺失时的占位实现。

`TelegramClient` 接口(节选):

```
鉴权: start() / submit_phone / submit_code / submit_password /
      submit_email / submit_email_code / submit_registration / logout / stop / close
状态: state(property) / me(property)
频道: list_joined_channels() / join_channel(identifier) / get_channel_metadata(id)
历史: iter_chat_history(channel_id, before_msg_id=…, limit=…)  (只向旧方向翻页)
媒体: download_file(file_id, progress_callback=…)  (失败返 None,不抛)
流:   subscribe_updates() → UpdateStream  (实时更新,caller 必须 aclose())
批量: mark_messages_read / forward_messages / pin_messages / unpin_messages /
      add_reaction / remove_reaction
```

**封装原则**:Telethon 的 `Message` / `Peer` / `Photo` / `Document` 等类型**绝不出本目录**;
`telethon_messages.py` 把它们归一化为 `MessageDTO` / `MediaDTO`。

**代理(SOCKS5)**:`Settings.proxy`(`TG_PROXY`)由 `proxy.py:parse_socks5_proxy` 转成
python-socks 代理 tuple `(ProxyType.SOCKS5, host, port, rdns=True, user, pass)` 传入
Telethon `proxy=`;`probe_proxy` 真做 SOCKS5 握手(启动 preflight / UI「测试连接」)。
见 [CONTRIBUTING.md § 代理与 Telethon 调试](../CONTRIBUTING.md)。

**session**:落在 `session/tgmonitor.session`(Telethon SQLite 格式),与 v1.x TDLib
session 不兼容 — 升级需重新登录。

### 3.5 `core/storage/` — 消息持久化

v2.0.0 默认 **SQLite**(aiosqlite,WAL),**PostgreSQL** 可选(asyncpg);MongoDB 已删。
`StorageRepository` 由 **7 个按域 ABC** 组合而成,两套实现共享同一组方法签名,
查询语义对齐(按 `date ASC, id ASC` 排序):

```
SchemaLifecycle · ChannelStore · MetaStore · MessageStore ·
MediaStore · ThumbnailStore · UserMetaStore  →  StorageRepository
```

| 概念 | SQLite(默认) | PostgreSQL(可选) |
|---|---|---|
| 频道 | `channels` 表 | `channels` 表 |
| 消息 | `messages` 表 | `messages` 表 |
| 媒体 | `media` 表(FK message) | `media` 表(FK message) |
| 唯一键 | `UNIQUE(channel_id, telegram_msg_id)` | `UNIQUE(channel_id, telegram_msg_id)` |
| 索引 | `(channel_id, date)` / `date` | `(channel_id, date)` / `date` + `pg_trgm` GIN |
| 部署 | 单文件 `tgmonitor.db`,零配置 | 需 PG 服务 + `TG_DB_DSN` |

**media 一律只存引用**(`object_key` + `object_backend` + `thumb_key` + `thumb_backend`),不存 BLOB。
`factory.py:build_storage` 按 `Settings.db_backend` 分发;旧 `.env` 的 `mongo` / `jsonl` 等非法值由 `Settings.load_tolerant()` 启动时回退 SQLITE 并提示,不阻断启动。

### 3.6 `core/objectstore/` — 对象存储

接口: `put / get / exists / delete / stat`(全 async)。

| Backend | 用途 | 协议 |
|---|---|---|
| `LocalObjectStore` | 开发 / CI / 单机 | 本地 FS,平铺 |
| `FolderObjectStore`(默认) | 大量文件 | 本地 FS,两级分片 |
| `S3ObjectStore` | 生产 | S3 协议(AWS S3 / MinIO / 阿里 OSS) |

**Local 安全**:`put` 拒绝 `..` 与绝对路径;原子写(`.part` → `rename`)。

**S3**:自动建 bucket(若不存在);endpoint_url 兼容第三方实现。

### 3.7 `core/export/` — 导出

`Exporter` ABC + `@exporter(ExportFormat.X)` 注册表。**新增格式 = 加一个文件**,UI 自动出现。

```python
# core/export/yaml_exporter.py
@exporter(ExportFormat.YAML)
class YamlExporter(Exporter):
    async def render(self, out_path, channels, messages, **kwargs):
        ...
```

`ExportService.run(request)` 是 async iterator,逐批拉消息 + yield 进度事件 + 调 Exporter 写盘。新增导出格式的流程见 [CONTRIBUTING.md § 添加新的导出格式](../CONTRIBUTING.md#-添加新的存储--对象存储--导出后端)。

### 3.8 `core/monitor/` — 监听服务(显式组合)

v2.0.0 把 5 个 mixin 改为**显式组合** — 状态单一所有权,消除 MRO `type: ignore`。
`MonitorService`(`service.py`)装配以下协作组件:

| 组件 | 职责 |
|---|---|
| `orchestrator.py` | 主循环 + 断线退避重连(1s → 2s → … → 30s) |
| `whitelist.py` | 白名单过滤 |
| `event_handlers.py` | 落库 + 发 `MessageReceived` 等领域事件 |
| `downloader.py` | `MediaDownloader`(单文件 200MB 上限,FULL 策略下 sync 复用) |
| `backfill.py` / `delete.py` | 回填 / 撤回处理 |

主循环语义:

```
async for msg in client.subscribe_updates():
    if msg.channel_id not in whitelist: continue
    if media and policy != METADATA: store_thumb
    storage.save_message(msg)              # 幂等
    bus.publish(MessageReceived(message=msg))
```

## 4. 事件流时序

### 监听新消息

```
Telethon update
  │  (events.NewMessage / EditMessage callback)
  ▼
TelethonTelegramClient._on_update(update)
  │  telethon_messages 归一化 → MessageDTO
  ▼
UpdateStream.push(dto)  ── fan-out ──►  MonitorService(orchestrator)
                                              │
                                              ▼
                            ObjectStore.put(media) │ StorageRepository.save
                                              │
                                              ▼
                              EventBus.publish(MessageReceived)
                                              │
                                              ▼
                              MonitorViewModel.message_received signal
                                              │
                                              ▼
                                   MainWindow / MessageView
```

### 导出

```
ExportDialog 选参数 ──► AppService.export(req)
                                       │
                                       ▼
                              ExportService.run(req)
                              │ 拉分批
                              │ yield 进度
                              │ EXPORTERS.get(fmt).render(...)
                              ▼
                          文件落盘 + ExportDone 事件
                                       │
                                       ▼
                          MonitorViewModel.export_done signal
                                       │
                                       ▼
                               QMessageBox 通知
```

## 5. 异步与线程

- **Core**:全程 `asyncio`,单事件循环
- **UI**:PySide6 + qasync 共享同一事件循环(Qt 主线程即 loop)
- **跨线程**:UI 调 core 一律走 `ui/async_bridge.py:run_coro(loop, coro, *, on_success,
  on_error, error_label)`,不要自己套 `asyncio.run_coroutine_threadsafe` + try/except 模板
- **组合根**:`core/runtime.py:bootstrap()` / `shutdown()` 为 CLI / GUI 共享
- **取消**:`MonitorService` stop Event 优雅取消
- **重连**:退避后重订阅 update stream

## 6. 错误处理

- **订阅者抛错被吞**:`EventBus.publish` 在每个订阅者外层 try/except + 日志
- **导出失败**:`ExportService` 捕获后发 `ExportDone(error=...)` 事件
- **Monitor 循环崩溃**:自动重订阅,UI 通过 `ErrorOccurred` 看到告警
- **鉴权失败**:发 `AuthErrorOccurred`,UI 引导重新登录
- **UI 弹错误**:`QMessageBox.critical` / `statusBar` 红字

## 7. 扩展点

| 想做什么 | 改哪里 |
|---|---|
| 加新 DB 后端 | `core/storage/xxx_repo.py` + `factory.py` + `config.DBBackend` |
| 加新对象存储 | `core/objectstore/xxx_store.py` + `factory.py` + `config.ObjectStoreBackend` |
| 加新导出格式 | `core/export/xxx_exporter.py` + `dto.ExportFormat`(UI 自动出现) |
| 加新 Telegram 后端 | 实现 `core/telegram/client.py` Protocol + `factory.py` 注册 |
| 加新消息事件 | `core/events.py` 加 dataclass + 在 `MonitorService` publish |
| 加新域操作 | 加在对应域服务上(**不要**加回 AppService facade,守护测试会拒) |
| 换 UI 框架 | 只重写 `ui/` 目录,core 不动 |
| 接入 webhook / 远程 | 在 `core/` 加新 `EventBus` 桥接器(本地 `EventBus` 仍保留) |

## 8. 设计取舍

- **纯 Python Telethon 取代 TDLib**:免去自编译 libtdjson 的原生工具链(cmake/gperf/openssl/
  vcpkg),三平台 `uv sync` 即跑;cryptg 加速加密,python-socks 走代理。代价是 session
  格式与 TDLib 不兼容(升级需重新登录),且不再锁定 TDLib 版本
- **SQLite 默认**:单文件零部署,桌面单机场景无需外部 DB;WAL 模式并发读不阻塞。
  PostgreSQL 留给多设备 / 大规模 / 全文搜索(`pg_trgm`)场景
- **不引入 ORM**(SQLAlchemy 之类):手写 SQL 更清晰,跨 SQLite/PG 的语义对齐也得自己控
- **存储接口按域切 7 个 ABC**:每个后端只实现自己需要的域,组合门面保持调用面不变
- **门面拆按域服务**:god facade 难维护、表面膨胀;域服务职责单一,UI 直调,allowlist 守护防回潮
- **HTML 缩略图 base64 内嵌**:打开文件即可看图,代价是文件变大;CSV/JSON/MD 不内嵌
- **DB 唯一键用 `(channel_id, telegram_msg_id)`**:跨频道不冲突;删除频道用 `ON DELETE CASCADE` 级联

## 9. 测试策略

- `InMemoryRepository` / `LocalObjectStore` / `FakeTelegramClient` 全在 `tests/conftest.py`
- core 单测**不**起 Postgres/S3/真实 Telegram 服务(全 fake,离线)
- UI 测试(QMainWindow / QListWidget / MessageView)在 `QT_QPA_PLATFORM=offscreen`
  下运行 —— 见 `.github/workflows/ci.yml`。CI 默认 offscreen,本地装 dev 依赖后直接 `pytest` 即可
- 真实集成(可选,CI-only)用 `testcontainers`:
  - PostgresRepo + testcontainers/postgres(`tests/integration/`)
  - S3Store + moto(mock,离线)

## 10. 进一步阅读

- [Telethon docs](https://docs.telethon.dev/)
- [MTProto 协议](https://core.telegram.org/mtproto)
- [PySide6 docs](https://doc.qt.io/qtforpython-6/)
- [qasync](https://github.com/CabbageDevelopment/qasync)
- [aiosqlite](https://github.com/omnilib/aiosqlite)
