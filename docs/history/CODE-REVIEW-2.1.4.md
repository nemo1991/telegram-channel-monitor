# 2.1.4 重度代码审核报告

> **历史文档**(点时间快照,2026-11-26 自 `docs/` 归档到 `docs/history/`)。描述的是当时的设计与
> 决策,其中的命令 / 路径 / 模块可能已随版本变更;现状以 `AGENTS.md`、`README.md`、
> `docs/ARCHITECTURE.md` 为准。索引见 `docs/README.md`。

> 审核日期：2026-11-25。范围：v2.1.3（main `ea61ad2`）全量生产代码，分三路并行深审——
> ① core 领域服务层（app_service / monitor / channel_sync / media_service / batch / auth / events）；② 存储与基础设施层（storage 双后端 / objectstore 三后端 / telegram 封装 / export）；③ UI 与 CLI 层（ui/ 全部 / cli/ 13 子命令 / 入口）。
> 全程只读，未修改任何生产代码。本文为三路报告合并去重后的结论。

## 总体结论

**无 P0**。分层纪律（core 零 UI 依赖）、双后端对称、防重入与自愈设计、QR token 安全（不进日志/stdout/EventBus）、退出码 0/1/2/130 主链路、closeEvent/aboutToQuit 双路径清理幂等性均验证通过。

风险集中在四类模式：

1. **域路径对已落库行的"整条覆盖"语义**（P1-1/P1-2/P1-12）——monitor 落库、Telegram 编辑推送等路径用重映射 DTO 整体替换，抹掉用户标注与下载状态；
2. **多后端 swap 的原子性**（P1-3）——reconfigure 中途失败留下"旧库已 close、monitor 持旧引用"的半完成态；
3. **语义与契约的静默偏离**（P1-4/P1-5/P1-8）——续拉方向与文档相反、S3 违反 KeyError 契约、PG list_media 与 SQLite 行为分叉；
4. **资源与订阅生命周期**（P1-6）——EventBus 闭包订阅永不退订。

## P1（12 项，建议优先修复）

### P1-1 编辑消息覆盖式写回，抹掉媒体下载状态【数据损坏，最优先】
- 位置：`core/monitor/service.py:530-543` → `_messages_sqlite.py:104-117` / `_messages_pg.py:178-193`
- 触发：已下载完成的消息收到 Telegram 编辑推送。
- 影响：`dataclasses.replace(existed, ..., media=msg.media)` 把 media 数组整体替换为重新映射的 DTO（object_key/download_status 全 None），随后 update_message 走 save_message 的"先清后插"（`DELETE FROM media WHERE message_id=?`）。结果：对象文件成孤儿、媒体页全部回到"未下载"、ZIP 导出跳过、去重失效。**双后端均受影响**。
- 修复：按 `telegram_file_id` 做 upsert 合并（保留已有 object_key/download_status），或 `_handle_edited` 改字段级 UPDATE 不含 media。

### P1-2 save_message upsert 覆盖 is_favorite/tags/notes，用户标注静默清零
- 位置：`_messages_sqlite.py:57`（ON CONFLICT DO UPDATE 列表）/ `_messages_pg.py:138`
- 触发：monitor 对已落库消息的重复推送/更新路径。
- 影响：用户加的收藏/标签/备注被 excluded 恒默认值覆盖清零。
- 修复：ON CONFLICT UPDATE 列表去掉这三列；与 P1-1 共享"落库前与 existed 合并"机制，建议同一 patch 序列处理。

### P1-3 reconfigure 半完成态：旧库已 close，monitor / channel_sync 仍持旧引用
- 位置：`core/app_service/_reconfigure.py:89-116`；`_backend_sync.py:51-56`（set_storage 不传播 channel_sync）
- 触发：`_rebuild_storage` 成功（旧 storage 已 close、已赋新）后 `_rebuild_objects` 校验失败上抛（S3 端点/凭据错即可触发，且为无条件执行）。
- 影响：MonitorService / ChannelSyncService 持已关闭的旧 storage → 实时消息写不进、每条报错；`app.settings` 未提交，下次 reconfigure 的 diff 基线错乱。无回滚路径。
- 修复：两段式提交——先全部构建新后端（不 close 不赋值），全部成功后同步段统一 close 旧 + 赋新 + 传播；失败即丢弃新对象。

### P1-4 全量同步续拉方向错误：新消息永远补不到，且每次白翻全库
- 位置：`core/channel_sync/service.py:311-337`；契约 `telegram/client.py:92-103`（`before_msg_id` = 向更早方向）
- 影响：`offset_id=max_id` 从已存最新处向旧方向翻，已存历史每条都要 skip（几万条 = 几万次查询 + 全库 API 翻页，放大 FLOOD_WAIT）；monitor 停止期间的新消息（id > max_id）永远不在拉取范围。
- 修复：resume 改为从最新拉、循环内 `msg_id <= max_id` break；或给 iter_chat_history 增加 min_id 语义。

### P1-5 `reconcile_orphans` 引用收集上限 100,000，超限误删真实媒体【数据丢失】
- 位置：`core/media_service/_media_reconcile.py:85-88`
- 触发：库内消息总数 > 10 万（limit 语义取最近 N 条，更旧被截断）。
- 影响：referenced_keys 不含被截断消息的 object_key → 判为孤儿，"Prune Orphans"（dry_run=False）真删已下载文件。
- 修复：分页收集，或直接 `list_media(status=DONE)` 收集 object_key。

### P1-6 QR 登录弹窗 EventBus 订阅泄漏：闭包永不退订，关闭后幽灵回调
- 位置：`ui/widgets/qr_login_dialog.py:129-134`（订阅）；closeEvent `:224` 只取消 future 不退订。
- 影响：每次打开新增一条订阅且闭包强引用 dialog（实例泄漏）；关闭后每次 `LoginStateChanged(ready)` 对已关闭对象 emit → accept() 重复执行，多次开关后行为不可预测。
- 修复：handler 存实例属性，closeEvent 中 `unsubscribe(LoginStateChanged, self._on_state)`（须同一函数对象）。

### P1-7 `cli sync` 进度写 stdout，违反「stdout 只数据 / stderr 进度」契约
- 位置：`cli/sync.py:57-64`（进度行）；`cli/monitor.py:85-88` 同病。
- 影响：重定向/管道消费被污染；与 `cli/_common.py:progress()`（stderr）自相矛盾。
- 修复：改 `print(..., file=sys.stderr)`。

### P1-8 `get_channel_metadata` 失败返回 stub DTO，sync 用 `#<id>` 覆盖真实频道标题
- 位置：`core/telegram/telethon_client.py:643-649`；消费方 `channel_sync/service.py:278-280`
- 触发：get_entity 对未缓存 id 抛 ValueError（常态）。
- 影响：`title="#123"` stub 经覆盖式 upsert 静默写入，username/member_count 清空，左栏显示损坏；调用方无法区分成败。
- 修复：失败抛异常让 sync 跳过；或 stub 不落库。

### P1-9 带认证的 SOCKS5 代理被 preflight 一票否决，无法启动
- 位置：`core/telegram/proxy.py:92-101`；拦截点 `telethon_client.py:314-318`
- 触发：`socks5://user:pass@host:port`。
- 影响：greeting 只声明 no-auth，认证代理回 `0x05 0x02` 被判失败，probe 失败直接 error，Telethon 本可正常完成 RFC 1929 认证。
- 修复：greeting 声明两种 method 并完成子协商；或探针失败降级 warning 放行。

### P1-10 S3 `get()/stream_read()` 违反 KeyError 契约，单对象缺失毁掉整次 ZIP 导出
- 位置：`core/objectstore/s3_store.py:125-130`；契约 `objectstore/base.py:63`；受害点 `zip_exporter.py:144-170`
- 影响：缺失抛 ClientError(NoSuchKey) 而非 KeyError；ZipExporter 只兜 KeyError → S3 后端一个缺失对象令整次导出作废并留半残 zip 文件。Local/Folder 正常跳过。
- 修复：S3 捕获 `Error.Code in ("NoSuchKey","NotFound")` 转抛 KeyError(key)。

### P1-11 Local/Folder `stream_read` 实为全量缓冲，ZIP 大文件导出 O(file_size) 内存
- 位置：`core/objectstore/_fs_base.py:99-127`；虚假声明 `base.py:100-107`、`zip_exporter.py:16-19`
- 影响：`to_thread(list, _chunks())` 先全量读进内存再 yield——桌面默认后端导出大视频时内存峰值=文件大小，叠加 zipfile 同步压缩跑在 qasync 主 loop，UI 冻结 + RAM 尖峰。"流式优化"对默认后端是假流式。
- 修复：逐块 `await asyncio.to_thread(f.read, chunk_size)` 或读/写流水线进 executor。

### P1-12 `list_media` 双后端元数据字段分叉：PG 全部丢成默认值
- 位置：`core/storage/_media_pg.py:224-246` vs `_media_sqlite.py:84-109`
- 影响：PG 只挑 11 个 key，is_pinned/is_favorite/tags/notes/reactions/forward_origin 全默认 → PG 后端 per-media CSV 元数据列恒为空；违背"查询语义双后端对齐"承诺（test_storage_backends 未覆盖该字段组）。
- 修复：PG 直接传整行 Record（删 msg_keys 白名单）。

## P2（44 项合并去重）

| # | 主题 | 位置 | 问题与修复方向 |
|---|------|------|----------------|
| 1 | 搜索 | `_sqlite_common.py:423`/`_media_pg.py:310` | 媒体搜索 LIKE 通配符未转义（`%`/`_`/`\`），与 list_messages 的正确实现不一致；补 replace + ESCAPE |
| 2 | 迁移 | `_lifecycle_pg.py:411-421` | PG init_schema `except Exception` 吞掉核心 DDL 失败，应用带病启动；核心 DDL 应上抛 |
| 3 | 迁移 | `_sqlite_common.py:101`/`_lifecycle_pg.py:151` | 每次启动无条件 `UPDATE media SET download_status='done'`，无 one-shot 标记，可能复活本应重下的行【推测】；迁移进 meta 版本号 |
| 4 | 迁移 | `_lifecycle_sqlite.py:127-150` | repair_schema 缺 subscribed→monitored 改名预检，存在绕过主迁移补出空列的路径【推测】 |
| 5 | 分页 | `_media_sqlite.py:81`/`_media_pg.py:198` | `limit=0` 被当"无界"返回全表；`if limit is not None` 或校验拒绝 |
| 6 | 分页 | `_messages_sqlite.py:255/185` | limit=None 无界查询；SQLite delete IN 超 32766 变量报错而 PG 无此限，批量删超大选区两后端分叉；分批 ≤1000 |
| 7 | 时区 | `_messages_pg.py:160` | naive datetime 进 PG TIMESTAMPTZ 按服务器时区解释，与 SQLite 时间线分叉；写入前统一 UTC |
| 8 | 导出 | `export/service.py:219` | 排序键 `datetime.min`(naive) 与 aware 混比可 TypeError；`str(id)` 字典序 "10"<"9"；改 UTC 化 min + int tie-break |
| 9 | 导出 | `export/service.py:171-217` | "流式分页"实为全量内存累积；offset 从尾翻页在并发写入下重复/漏导；keyset 分页或快照（与 #10 同修） |
| 10 | 导出 | `csv_exporter.py:95`/`media_list_csv_exporter.py:122` | tags 列漏做 CSV 公式注入防护；join 前逐 tag guard |
| 11 | 导出 | `csv_exporter.py:70`/`media_list_csv_exporter.py:87` | CSV 无 BOM，Windows Excel 中文乱码；改 utf-8-sig |
| 12 | 导出 | `json_exporter.py:52`/`zip_exporter.py:117` | 枚举被 `default=str` 序列化为 "MediaType.PHOTO"，与 DB 值及 CSV 不一致；显式转 .value |
| 13 | 导出 | `html_exporter.py:141-165` | 动态属性挂 thumb_data_uri 依赖 dataclass 未开 slots（try/except pass 吞错）；DTO 加 slots 则缩略图静默消失；另缩略图 N+1 查询 |
| 14 | 导出 | `cli/messages.py:46-48` | `--tag` 过滤发生在 limit 截断之后，结果漏项；过滤下推 SQL |
| 15 | CLI | `cli/main.py:117/207`+`channels.py:90` | 子命令组缺动作时 exit 1 而非 2，且先完整 bootstrap 才报用法错；`add_subparsers(required=True)` 或分发前预检 |
| 16 | CLI | `cli/config.py:163` | config set 未知 key 抛裸 ValueError，traceback 直出；改抛 CliError |
| 17 | CLI | `cli/channels.py refresh` 等 | 部分写操作无 --yes（refresh 会级联停监听），与 add/remove/logout 的豁免边界不一致【推测 PRD 有意豁免，需对齐】 |
| 18 | 对象存储 | `s3_store.py:132-156` | S3 exists/stat 吞掉一切异常，网络错误被解读为"对象不存在"→ 重复下载；区分 404 与其它错误 |
| 19 | 对象存储 | `s3_store.py:116-123` | put 单次 PUT 无 multipart（>5GB 失败 + 全量内存），且每操作新建 aiobotocore client；upload_file + 复用 client |
| 20 | 对象存储 | `folder_store.py:95-139` | iter_keys 启发式对多级子目录 key 产错 key → reconcile_orphans 误删【当前 make_key 不触发，迁移场景浮出】；落盘旁车编码原 key |
| 21 | 对象存储 | `local_store.py:57` vs `folder_store.py:75` | put 的 meta.sha256 回填行为分叉，Folder 后端 thumbnails.sha256 恒 NULL；逻辑上提共享基类 |
| 22 | 对象存储 | `_fs_base.py:74-93` | exists/delete/stat 事件循环内同步 IO（get/put 已走 to_thread，这三个没走） |
| 23 | Telegram | `telethon_client.py:707-728` | iter_chat_history 的 limit 参数从未使用，Protocol 文档"最新 N 条"与实现（全量拉）矛盾 |
| 24 | Telegram | `telethon_client.py:848-854` | mark_messages_read 按 max_id 标记（影响范围是 msg_ids 超集，未选中消息被置读）；确认语义或改 ids 列表 |
| 25 | Telegram | `telethon_client.py:505-513` | stop() 绕过 _set_state 直写 `_state`，不发状态事件（违反"唯一写入口"自定原则）；UI 可能停留 ready |
| 26 | Telegram | `telegram/client.py:157` | Protocol 引用不存在的 TelegramAPIError，异常映射文档失实 |
| 27 | Telegram | `telethon_client.py:769-774` | download_file 整文件进内存（file=bytes），数 GB 视频 RAM 峰值=文件大小，S3 put 再持一份；改临时文件流式 |
| 28 | Telegram | `telethon_client.py:384-405` | watchdog 主动 connect 与 Telethon auto_reconnect 竞态【推测，Telethon 有内部锁可能安全】 |
| 29 | Telegram | `telethon_client.py:80-87` | _TelethonUpdateStream 无界队列，高频频道 + 消费慢时内存增长；Queue(maxsize) + 丢弃策略 |
| 30 | Monitor | `monitor/backfill.py:163` | skip_next_tick 设置后无任何消费者，死逻辑；reconnect kick 与 30s tick 双跑 |
| 31 | Batch | `batch_service.py:56` | 单一 cancel_event、op 开头 clear，并发批量互相破坏取消信号；每 op 局部 Event |
| 32 | Batch | `batch_service.py:118-135` | delete_messages_batch 部分失败时 failures=[]，"查看失败详情"拿空列表 |
| 33 | 生命周期 | `app_service/_lifecycle.py:202` | shutdown 中 storage/objects.close 无超时（client 有 2s，后两步可无限挂）；包 wait_for |
| 34 | 生命周期 | `monitor/orchestrator.py:154` | 主 task 超时后 cancel 但不 await，异常无人观察 |
| 35 | 生命周期 | `app_service/_lifecycle.py:161` | resume_monitor：start 失败仍 flip paused=False 发 Resumed，假"监听中" |
| 36 | 写路径 | `monitor/service.py:607`/`_media_os_actions.py:470,594` | 下载 worker/手动下载用 stale DTO 整条回写，下载期间编辑推送被旧文本覆盖；改 media 定向 UPDATE |
| 37 | 服务 | `channel_service.py:97/159` | 频道写路径多步无事务，中途异常留半状态；storage 层提供原子入口 |
| 38 | 设置 | `settings_store.py:86-89` | .env 明文 secret 且权限 0644 未收紧；写后 chmod 0o600 |
| 39 | 设置 | `settings_store.py:327-329` | `a and b or b` 恒等于 b 的死代码；清理 |
| 40 | UI | `ui/widgets/lightbox_dialog.py:550,852` | 视频 tmpfile 仅 closeEvent 清理，Esc/accept/reject 路径不触发 → $TMPDIR 残留大文件；override done() 收口 |
| 41 | UI | `ui/widgets/dashboard_widget.py:491` | activity_list.clear() 不 deleteLater 旧 itemWidget，缓慢泄漏；照抄 :422 已有模式 |
| 42 | UI | `ui/widgets/batch_progress_dialog.py`、`channel_picker_dialog.py` | 死代码，生产无 import（qss 残留无效选择器）；删除或明确预留 |
| 43 | UI | `ui/viewmodels/_media.py` docstring | run_coro 回调线程描述与 qasync 实际（主线程）相反，误导新维护者 |
| 44 | DTO | `core/dto.py` | MediaDTO.size Optional 但跨后端 NULL 排序位置不一致（SQLite LAST / PG ASC）；统一 `size IS NULL, size DESC` |

低优先观察项：`zip_exporter` written 死代码；`telethon_messages.py:425` date 异常伪造 now()；`_activity_sqlite.py:32` occurred_at None 撞 NOT NULL（当前路径不触发）。

## 已验证非问题（安全项 ✅）

1. **QR token 不泄露**：仅经返回值传递，不进日志/stdout/EventBus；CLI 二维码渲染走 stderr。
2. **MessageDTO.date 非空**：default_factory 保证，全部生产构造点非空；疑似 GUI 崩溃点不成立。
3. **退出码 0/1/2/130 主链路正确**；app.py 单 loop `run_forever` + processEvents 泵模式正确，无嵌套 exec / 死等隐患。
4. **`update_views/reactions` 定向 UPDATE、`download_all_pending` 先收齐 keys、删除不回滚决策**等设计均为正确取舍。

## 测试盲区（审核中发现，后续补测优先）

1. 同秒多消息场景（#1 关联）无测试；
2. PG update_message_media 冲突重映射无测试（PG 语义漂移在 deselected 集成层，CI 不可见）；
3. S3/Local/Folder `stat()` 404 异常契约一致性无对照测试；
4. Local vs Folder `has_object` 无对照测试；
5. CSV BOM / tags 注入防护无测试；
6. reconfigure 中途失败回滚路径无测试。

## 修复优先级建议

1. **立即**：P1-1 + P1-2（共享合并机制，一个 patch 序列）→ P1-5（数据丢失）→ P1-3；
2. **随后**：P1-4、P1-8、P1-10（契约正确性）→ P1-6（登录主流程稳定）→ P1-9、P1-12；
3. **顺带**：P1-7 + P2-15/16（CLI 契约一行修）；P2-11 CSV BOM、P2-10 tags 注入（用户可见小修）；
4. P2 其余按主题归组排期；P2-17/24 等【推测】项先验证再修。
