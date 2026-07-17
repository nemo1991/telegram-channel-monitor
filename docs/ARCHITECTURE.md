# 🏗️ Architecture

本文档面向**想深入了解 / 二次开发**的读者。如果你只想用,看 [README.md](../README.md) 即可。

## 1. 设计目标

1. **UI 与 core 彻底分离** — 可以重写 UI(比如换成 Tauri / web)而 core 不动一行
2. **可替换** — DB / 对象存储 / Telegram 客户端 / 导出格式都是 ABC/Protocol
3. **可测试** — core 100% 离线可测,不依赖 Telegram / 真实 DB / S3
4. **类型安全** — 跨边界全部 DTO,无 `Any` 泄漏
5. **小依赖** — 默认 Postgres + Local 就能跑

## 2. 分层

```
┌─────────────────────────────────────────────────────────────┐
│ UI (PySide6)                                                │
│   widgets/  viewmodels/  main_window.py                     │
│                                                             │
│   ↓ 只调 AppService + 订阅 EventBus + 用 DTO                │
├─────────────────────────────────────────────────────────────┤
│ Facade (AppService)                                         │
│   login / submit_code / subscribe_channel / list_messages   │
│   export / start_monitor / stop_monitor / shutdown          │
├─────────────────────────────────────────────────────────────┤
│ Services                                                     │
│   MonitorService  ExportService                             │
├─────────────────────────────────────────────────────────────┤
│ Interfaces (ABC / Protocol)                                 │
│   StorageRepository · ObjectStore · TelegramClient          │
│   Exporter + EXPORTERS registry                             │
├─────────────────────────────────────────────────────────────┤
│ Implementations                                             │
│   PostgresRepository  MongoRepository                       │
│   S3ObjectStore  LocalObjectStore                           │
│   TdlibTelegramClient  FakeTelegramClient                   │
│   JsonExporter  CsvExporter  MarkdownExporter  HtmlExporter │
├─────────────────────────────────────────────────────────────┤
│ Infrastructure                                               │
│   Settings (pydantic-settings)  EventBus  DTO  Logging      │
└─────────────────────────────────────────────────────────────┘
```

## 3. 关键模块

### 3.1 `core/dto.py` — 跨边界数据对象

| DTO | 字段 | 用途 |
|---|---|---|
| `ChannelDTO` | id, title, username, kind, member_count, created_at | 频道元信息 |
| `MediaDTO` | type, mime_type, file_name, file_size, w/h/duration, telegram_file_id, object_key, object_backend, thumb_key, thumb_backend | 媒体元数据 + 对象存储引用 |
| `MessageDTO` | id, channel_id, telegram_msg_id, author, date, text, views, forwards, reply_to_msg_id, edited, media[], raw | 消息 |
| `ExportRequest` | channel_ids[], date_from, date_to, format, out_path, include_media_meta, include_thumbnails | 导出请求 |
| `ExportResult` | out_path, message_count, bytes_written | 导出结果 |

**约定**:DB 与 ObjectStore 都不在 DTO 内部;DTO 持有 **引用**(`object_key` / `thumb_key`)。

### 3.2 `core/events.py` — EventBus

领域事件继承 `Event`,全 `async` pub/sub:

| 事件 | 触发时机 |
|---|---|
| `LoginStateChanged` | 登录状态机变化 |
| `ChannelDiscovered` | TelegramClient 枚举到新频道 |
| `ChannelSubscribed` | 用户加入监听白名单 |
| `ChannelUnsubscribed` | 移除监听 |
| `MessageReceived` | 一条消息已落库 |
| `MessageDeleted` | 消息撤回 |
| `ExportProgress` | 导出进度心跳 |
| `ExportDone` | 导出完成 / 失败 |
| `ErrorOccurred` | 任何子系统错误 |

订阅示例:

```python
async def on_message(e: MessageReceived) -> None:
    print("新消息:", e.message.text)

bus.subscribe(MessageReceived, on_message)
await bus.publish(MessageReceived(message=m))
```

### 3.3 `core/app_service.py` — Facade

UI 唯一入口。所有方法 `async`,返回 DTO,失败抛错并发 `ErrorOccurred` 事件。

```python
# UI 侧使用
state = await app.login("+86...")
state = await app.submit_code("12345")
await app.subscribe_channel(channel)
async for _ in app.export(req):
    ...  # 进度心跳
```

### 3.4 `core/telegram/` — TDLib 集成边界

唯一接触 TDLib 的目录。`client.py` 定义 `TelegramClient` Protocol;`tdlib_client.py` 是真实实现,`fake_client.py` 是测试桩。

`TelegramClient` 接口:

```
鉴权: login(phone) → submit_code(code) → submit_password(pwd) → ready
频道: list_joined_channels() / join_channel(identifier)
消息: iter_messages(channel_id, ...)  (历史回放,异步迭代器)
流:   subscribe_updates() → UpdateStream  (实时更新)
```

**封装原则**:TDLib 的 `Update` / `Message` / `Photo` / `Document` 等类型**绝不出本目录**;`tdlib_client.py` 内部用 `mapping.py` 归一化为 `MessageDTO` / `MediaDTO`。

**代理(SOCKS5)**:`Settings.proxy` + `TD_PROXY` 环境变量,由 `TgProxy` 适配进 aiotdlib 的 `proxy_settings`;见 [CONTRIBUTING.md § 代理与 aiotdlib 调试](../CONTRIBUTING.md)。

### 3.5 `core/storage/` — 消息持久化

两套实现共享同一组方法签名,查询语义对齐(按 `date ASC, id ASC` 排序)。

| 概念 | PostgreSQL | MongoDB |
|---|---|---|
| 频道 | `channels` 表 | `channels` 集合 |
| 消息 | `messages` 表 | `messages` 集合 |
| 媒体 | `media` 表(FK message) | 嵌入 `messages` 文档的 `media[]` |
| 唯一键 | `UNIQUE(channel_id, telegram_msg_id)` | `{channel_id, telegram_msg_id}` unique index |
| 索引 | `(channel_id, date)` / `date` | `(channel_id, date)` / `date` |

**media 一律只存引用**(`object_key` + `object_backend` + `thumb_key` + `thumb_backend`),不存 BLOB。

### 3.6 `core/objectstore/` — 对象存储

接口: `put / get / exists / delete / stat`(全 async)。

| Backend | 用途 | 协议 |
|---|---|---|
| `LocalObjectStore` | 开发 / CI / 单机 | 本地 FS,内容寻址 |
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

### 3.8 `core/monitor/service.py` — 监听服务

主循环:

```
async for msg in client.subscribe_updates():
    if msg.channel_id not in whitelist: continue
    if media and policy != METADATA: store_thumb
    storage.save_message(msg)              # 幂等
    bus.publish(MessageReceived(message=msg))
```

遇错进入退避重连(1s → 2s → 4s → ... → 30s),保持长连接。

## 4. 事件流时序

### 监听新消息

```
TDLib update
  │  (aiotdlib callback)
  ▼
TdlibTelegramClient.on_update(update)
  │  mapping → MessageDTO
  ▼
UpdateStream.push(dto)  ── fan-out ──►  MonitorService._handle
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
                              │ ExporterRegistry.get(fmt).render(...)
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
- **UI**:PySide6 + qasync 共享同一事件循环
- **跨线程**:UI 调 core 用 `asyncio.run_coroutine_threadsafe(coro, loop)`(在 `MonitorViewModel`)
- **取消**:`MonitorService._stop` Event 优雅取消
- **重连**:退避后重订阅 update stream

## 6. 错误处理

- **订阅者抛错被吞**:`EventBus.publish` 在每个订阅者外层 try/except + 日志
- **导出失败**:`ExportService` 捕获后发 `ExportDone(error=...)` 事件
- **Monitor 循环崩溃**:自动重订阅,UI 通过 `ErrorOccurred` 看到告警
- **UI 弹错误**:`QMessageBox.critical` / `statusBar` 红字

## 7. 扩展点

| 想做什么 | 改哪里 |
|---|---|
| 加新 DB 后端 | `core/storage/xxx_repo.py` + `factory.py` + `config.DBBackend` |
| 加新对象存储 | `core/objectstore/xxx_store.py` + `factory.py` + `config.ObjectStoreBackend` |
| 加新导出格式 | `core/export/xxx_exporter.py` + `dto.ExportFormat`(UI 自动出现) |
| 加新消息事件 | `core/events.py` 加 dataclass + 在 `MonitorService` publish |
| 换 UI 框架 | 只重写 `ui/` 目录,core 不动 |
| 接入 webhook / 远程 | 在 `core/` 加新 `EventBus` 桥接器(本地 `EventBus` 仍保留) |

## 8. 设计取舍

- **不引入 ORM**(SQLAlchemy 之类):手写 SQL 更清晰,跨 PG/Mongo 的语义对齐也得自己控
- **媒体嵌入 messages 文档(Mongo)**:方便一次 `find()` 拿到全部,大消息时再考虑分离
- **HTML 缩略图 base64 内嵌**:打开文件即可看图,代价是文件变大;CSV/JSON/MD 不内嵌
- **不做增量同步 / 游标**:`list_messages` 一次拉完,适合中小数据量;大数据需扩展分页游标
- **aiotdlib 而不是裸 ctypes 调 td_json**:`aiotdlib` 维护成本低,接口 Pythonic;如需极致控制可换
- **DB 唯一键用 `(channel_id, telegram_msg_id)`**:跨频道不冲突;删除频道用 `ON DELETE CASCADE` 级联

## 9. 测试策略

- `InMemoryRepository` / `LocalObjectStore` / `FakeTelegramClient` 全在 `tests/conftest.py`
- core 单测**不**起 Postgres/Mongo/S3/TDLib 真实服务
- UI 测试(QMainWindow / QListWidget / MessageView)在 `QT_QPA_PLATFORM=offscreen`
  下运行 —— 见 `.github/workflows/ci.yml`(`Install Qt system dependencies (Ubuntu)`
  + `pytest` step 的 `QT_QPA_PLATFORM: offscreen` env)。CI 默认 offscreen,本地
  装 dev 依赖后直接 `pytest` 即可
- 真实集成(可选)用 `testcontainers`:
  - PostgresRepo + testcontainers/postgres
  - MongoRepo + testcontainers/mongodb
  - S3Store + testcontainers/minio

## 10. 进一步阅读

- [TDLib docs](https://core.telegram.org/tdlib)
- [aiotdlib](https://github.com/pylakey/aiotdlib)
- [PySide6 docs](https://doc.qt.io/qtforpython-6/)
- [qasync](https://github.com/CabbageDevelopment/qasync)
