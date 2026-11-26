# 2.1.7 发版前全面审核报告

> 审核日期：2026-11-26。范围：v2.1.6（main `e72ec0a`，tag `v2.1.6`）全量代码与文档。
> 维度：架构 / 编码 / 单元测试 / 文档 / 工程化门禁，四路并行只读深审 + 本地全量测试带覆盖率实跑。
> 全程未修改任何生产代码。上一轮（`history/CODE-REVIEW-2.1.4.md`）的 56 项不重复报告，只做修复质量回看（见第四节）。
>
> **标注约定**：【复核】= 报告人亲自读码或跑复现确认；【取证】= 子审计脚本静态分析结果，未逐条二次复核。
> 复现与分析脚本在 `/tmp/tgmon-audit/`（临时目录，不入库）。

## 〇、处理状态（2026-11-26 回填）

下文各节是**审核当时**的快照，保持原样不逐条改写；处理结果以本节为准。

| PR | 内容 | 对应条目 |
|---|---|---|
| [#165](https://github.com/nemo1991/telegram-channel-monitor/pull/165) PR-A 止血 | 「打开媒体」扩展名白名单；事件订阅一次性化；关流哨兵改 `put_nowait` + `aclose` 加超时；日期预设 / 导出日期转 aware；`config show/get` 对 URL 类字段脱敏；README 三处失效；`.part` 唯一临时名 + Windows `os.replace` / `mkdir` 竞态重试；`.env` 以 `0600` 创建。新增 `tests/test_review_217_fixes.py` 61 项 | P1-1 / 2 / 3 / 6 / 7 / 8；P2-1 / 2 / 5；2.1.4 的 P2-38 |
| [#166](https://github.com/nemo1991/telegram-channel-monitor/pull/166) A2 | 删除失效的 `legacy` marker hook 与 `--include-legacy` | P1-4 |
| [#167](https://github.com/nemo1991/telegram-channel-monitor/pull/167) PR-B 门禁加固 | coverage 去掉 `continue-on-error` 并加 `--fail-under=70`；integration 输出 junit 并断言零 skip；`seeded_repo` / `meta_seeded_repo` 加第三后端 `pg`（42 项）；三份手写清单守卫改目录自动发现；删空转的 `windows-paint-probe`；ruff 版本取自 `uv.lock` + `uv lock --check`；RUF100 进门禁，清 256 条失效 noqa（74 个文件 AST 等价） | P1-5 / 10；P2-9 / 21 / 28；P2-23 的 CLI 部分；P2-41；P2-42 的 CI 侧 |
| PR-C 文档校准 | AGENTS.md 重写（只写现状，6.12 归因史迁 `history/`）；SECURITY.md 重写；`.env.example` 补全到全部 22 个有效字段；CHANGELOG 2.1.6 数字勘误 + `[Unreleased]` 基点；10 份点时间文档迁 `docs/history/` 并加状态头 + `docs/README.md` 索引；陈旧文案清理（7 处 CLI 用法提示 + 2 处登录提示指向已删的 `tgmonitor` 命令、docstring / 模块头 / issue 模板坏链） | P2-30 / 31 / 32 / 34 / 35；P2-33 的勘误与链接 |

**回填后的基线**：默认门禁收集 1177 项（1175 passed / 2 skipped），integration 标记 105 项；分支覆盖率 71%（13,533 语句），CI 阈值 70；CI 7 个 job，全部阻塞合并。

**新门禁首跑的发现**：三后端参数化在真 PG 上第一次执行即命中 1 项 —— `aggregate_per_channel.last_date` 在 PG 是 aware UTC、SQLite / InMemory 是 naive。判定为断言未按「aware UTC 规范形、naive 视为 UTC」的契约比较，修在测试侧，未动 `src/`。**遗留**：三个后端读回的 datetime 形状不统一，目前靠各消费点兜底；在仓储边界统一是设计决定，留到后续。

**未处理，留到 2.1.8+**（需设计，见第七节）：P1-9 `AppService.start()` 与 reconfigure 阶段 2 原子化；P2-3 / 4 / 6 / 7 / 8 / 10 / 11（编码）；P2-12 ~ 20（架构）；P2-22 / 24 / 25 / 26 / 27（测试）；P2-33 的 CHANGELOG 归档；P2-36 用户文档缺口；P2-37 ~ 40 / 43（CI）；P2-42 的 pre-commit 侧（`.pre-commit-config.yaml` 的 ruff 仍是 `v0.6.9`，需在有网络的环境下跑通钩子后再对齐）。

## 一、总体结论

1. **架构骨架是健康的**：`core/` 对 PySide6 / qasync 零依赖、四套抽象边界清楚、三层签名一致、注入防护齐全。问题不在"层与层之间"，而在**层内部**：拆文件后耦合没跟着拆、生命周期门面是空壳、异常没有统一出口。
2. **2.1.6 "P2×44 全量修复"的成色需要打折**：抽查 13 项，8 项真修好，5 项是补丁式；其中 **2 项修复引入了新缺陷**（队列反压 → 满队列关闭挂死；批量取消改 per-op → 并发时取消丢失）。
3. **测试体系的真正风险不是用例少，而是"守卫失守"**：legacy hook 误伤 23 项在用测试、PG 门禁可零执行仍绿、覆盖率 71% 且不门禁、组合根 `app.py::run()` 零行为测试。
4. **文档漂移面很广**，其中 3 处直接误导新用户（README 的 `channels refresh` 示例现在会失败）。

## 二、基线数据

| 指标 | 数值 |
|---|---|
| 源码 | 164 个 .py / 36,029 行 |
| 测试 | 156 个 .py / 28,821 行；默认门禁收集 1114 项（实跑 1113 passed / 1 skipped，退出码 0），integration 63 项 |
| 分支覆盖率 | **71%**（13,412 语句，3,513 未覆盖）；CI 不设阈值 |
| ruff 门禁 | check + format 全绿（0.17.6） |
| 类型注解覆盖 | 99.4%（1568 个 def 仅 10 个无注解）【取证】 |
| 版本落点 | 6 处全部为 2.1.6 |

## 三、P1（发版前应修，10 项）

### P1-1 「打开媒体」的扩展名由远端频道控制【安全】【复核】
- 位置：`core/media_service/_media_os_actions.py:389-396`（取扩展名）、`:409`（落盘）、`:217-234`（系统默认程序打开）。
- 问题：扩展名直接取自频道发来的 `file_name` / `mime_type`，落盘到临时目录后用系统默认程序打开。恶意频道发 `x.bat` / `.hta` / `.jse`，用户点"打开"即执行。
- 建议：扩展名白名单；非白名单类型强制 `.bin` 或改为"打开所在文件夹"；在 DTO 映射层一并收口。

### P1-2 暂停/继续导致事件订阅累积【复核·代码路径】
- 位置：`core/monitor/orchestrator.py:63-116`（每次 `start()` 重新 `bus.subscribe` 7 个 handler）、`:130-167`（`stop()` 不退订）；`core/app_service/_lifecycle.py:114-116,167-169`（pause/resume 直接 stop/start）。
- 问题：`monitor/` 与 `app_service/` 内 `unsubscribe` 零调用。每轮暂停/继续，同一 handler 多登记一份，之后的消息被重复处理（落库、下载、发事件）。
- 建议：订阅移到一次性初始化，`start/stop` 只管 task；或在 `stop()` 成对退订。补一条"两次 pause/resume 后 handler 计数不变"的测试。

### P1-3 队列满时暂停/退出永久挂死【复现】（2.1.6 P2-29 引入）
- 位置：`core/telegram/telethon_client.py:111-115`（`_closed=True` 后 `await put(None)`）；`core/monitor/orchestrator.py:149-153`（先 `aclose()` 后才轮到 2s 超时）。
- 复现：队列 2000/2000 时 `close()` 挂死（0.5s 超时未返回）；队列 100 时正常。
- 问题：积压达上限（代码自己警告的"消费端跟不上"场景）时，`rt.stop` 已置位、消费者不再取数，`put(None)` 永远等不到位置；后面的 2s 硬取消永远到不了。`_lifecycle.py:116` 注释"内部已有 2s 超时"不属实。
- 建议：哨兵用 `put_nowait`，满了直接丢弃（`__anext__` 已检查 `_closed`）；`aclose` 外包 `wait_for`。

### P1-4 legacy hook 误伤 23 项在用测试，"默认 skip"是死代码【复核】
- 位置：`tests/conftest.py:95-101`（子串 `"_pr" in stem` 命中 `test_proxy.py` 16 项、`test_media_progress_signal.py` 3 项等共 23 项）、`:106`（skip 分支在 pytest 9.2.0 下恒不触发）；`pyproject.toml:108-112`（注释声称 `not legacy` 与 `--include-legacy`，实际 addopts 只有 `not integration`）。
- 问题：现状靠"两个 bug 对冲"——标记打上了但 skip 不生效，所以这 23 项照常跑。任何人把 hook"修对"，`test_proxy.py` 全部 16 项会静默退出 CI。hook 本意的目标文件 `test_*_prN.py` 早已删除。
- 建议：删除 `conftest.py:76-117` 与 `legacy` marker，修正 pyproject 注释；`tests/perf/` 改为显式路径单独跑。

### P1-5 PG 门禁可"零执行仍绿"，15 个方法从未在真 PG 上执行【复核 skip 路径；15 项为取证】
- 位置：`tests/integration/conftest.py:53-54`（Docker 任何异常 → `pytest.skip`）；`.github/workflows/ci.yml:224-263`（integration job 无"skipped 必须为 0"断言）；`tests/test_storage_backends.py:156,170`（对等套件只参数化 `inmemory` / `sqlite`）。
- 问题：容器起不来时 63 项全部 skip，job 仍是绿的。52 个 PG 公开方法里 15 个在 integration 测试里零调用，`_thumbnails_pg.py`、`_activity_pg.py` 整文件无真 PG 执行。非 integration 侧的 PG 覆盖只有截获 SQL 字符串做子串断言，不执行 SQL。
- 建议：integration 步骤输出 junitxml 并断言 skipped 为 0；把 `test_storage_backends.py` 的用例参数化到三个后端（PG 那半打 `integration`）；用参数化骨架强制每个 PG 公开方法至少一条用例。

### P1-6 消息页日期预设时区错位【复现】
- 位置：`ui/widgets/message_manager_widget.py:302-316`（"今天 / 7 天 / 30 天"生成 naive 本地时间）；`core/storage/_sqlite_common.py:264-270`（naive 原样比较，aware 转 UTC）。
- 复现：本地 07:30 的消息，"今天"筛选命中 0 条（应为 1）。
- 影响：东八区下每天 00:00–08:00 的消息被"今天 / 7 天 / 30 天"全部漏掉。
- 建议：预设生成时转 aware；或存储层统一归一。

### P1-7 README 上手三处失效【复核】
- `README.md:87`：`tgmon-cli channels refresh` 缺 `--yes`，照抄会退出码 1（2.1.6 P2-17 加的确认，README 没跟）。
- `README.md:41`：`cp .env.example .env` 放仓库根不会被读取——`core/config.py:65-67` 只读平台数据目录的 `.env`。
- `README.md:52-54`：上手步骤仍写"左栏「频道」双击订阅"，实际是 4 页导航、频道管理在设置页。
- 附：`README.md:28` 写媒体策略"无需重启"，`:151` 写"重启生效"，自相矛盾。

### P1-8 `config show / get` 漏打码 `db_dsn` 与 `proxy`【复核】
- 位置：`cli/config.py:23`（`_SENSITIVE` 只有 `api_hash` 与两个对象存储密钥）。
- 影响：带密码的 PG DSN、带账号密码的代理 URL 明文进 stdout。
- 建议：加入集合；顺带处理 `core/telegram/proxy.py:35-51` 解析失败的 `ValueError` 携带完整代理 URL 的问题【取证】。

### P1-9 生命周期门面是空壳，启动序列三处手写【复核】
- 位置：`core/app_service/_lifecycle.py:80-95`（`start_monitor` 只翻 `_running` 标志）；`app.py:337-370`、`cli/monitor.py`、`cli/sync.py` 各自手写"白名单 → `monitor.start()` → `bootstrap()"。
- 问题：`start_monitor` / `subscribe_updates` 生产代码零调用；`_reconfiguring` 只写不读；`core/runtime.py:219-226` 靠 `isinstance` 分别关 monitor 和 app。名义上 UI 只依赖 AppService，实际启动顺序由三个调用方各自保证。
- 建议：AppService 增加 `async start()` 吸收这三步，三处调用方只调它；删除空壳成员并同步 surface allowlist。

### P1-10 覆盖率 71% 且不门禁【复核】
- CI coverage job `continue-on-error`、无阈值、产物无人消费，覆盖率下降不会变红。
- 本地实测 0% 模块：`ui/widgets/export_dialog.py`（106 语句）、`ui/widgets/error_log_dialog.py`（48）、`ui/_qt_callbacks.py`（19）、`ui/widgets/clear_channel_preview_dialog.py`（34）。`ui/controllers/export_controller.py` 43 语句缺 34。
- `app.py::run()` 是全仓唯一圈复杂度豁免点，它的 20 项测试全部是 `inspect.getsource` 读源码文本，零行为测试。
- 注意：`cli/main.py` 在覆盖率里是 0/172，但 `test_cli.py` 有 19 处走 subprocess，覆盖率测不到——CLI 这组数字是低估，真实值未知。
- 建议：先加 `--fail-under` 棘轮（当前 71%，先设 70 只防跌），再谈提高。

## 四、上一轮（2.1.4）修复质量回看【取证】

抽查 13 项：

| 结论 | 项 |
|---|---|
| 真修好 | P1-2 upsert 不清标注、P1-9 代理认证、P1-10 S3 KeyError、P1-12 PG 元数据、P2-3 迁移 one-shot |
| 真修好但有取舍或遗留 | P1-1 编辑合并（`_seen_ids` 逐出后仍走 DELETE+重插）、P1-4 续拉方向（首次同步中断后的空洞不补）、P1-5 孤儿收集（每条消息查一次缩略图，N+1）、P1-11 流式读（只修读侧，ZIP 写仍同步） |
| 补丁式 | P1-3 两段式提交（阶段 2 非原子：monitor 先切，之后抛错则新后端不关、settings 不提交；仅改凭据也会无条件重建对象存储）、P2-19 S3 multipart（`put()` 不回填 sha256，与 `base.py:64-65` 契约矛盾）、P2-27 下载进内存（`exists()` 会抛了，`downloader.py:270` 没包）、P2-38 .env 权限（`.part` 先按 umask 写密文再 chmod，有 0644 窗口） |
| 引入新缺陷 | P2-29 → P1-3；P2-31 → 并发批量取消丢失（见 P2-1） |

CHANGELOG 2.1.6 写"82 文件 +4697"，`git show --stat 1415a2b` 实为 **85 文件 +4926**。

## 五、P2（建议排期）

### 编码 / 运行时

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| 1 | 并发批量取消丢失【复现】 | `core/batch_service.py:126-128` | `_end_op` 无条件置 None，先结束的 op 会清掉仍在跑的 op 的取消信号。UI 有操作门控，实际可达性待确认 |
| 2 | `.part` 临时名固定 | `core/objectstore/_fs_base.py:85,132` | 同 key 并发写互相截断；改 `mkstemp` 同目录唯一名 |
| 3 | S3 打开媒体整文件进内存 | `_media_os_actions.py:411-412` | 用 `get()` 全量读；改 `stream_read` 分块写 |
| 4 | 暂停/恢复丢下载积压 | `core/monitor/orchestrator.py:119,134-141` | 队列被丢，行停在 DOWNLOADING，只有重启才自愈 |
| 5 | `iter_keys` 把 `.part` 当对象 | `local_store.py:68`、`folder_store.py:155,162` | Prune Orphans 可删写入中的半成品 |
| 6 | 启动期 task 未持引用 | `app.py:295,299` | 两处 `create_task` 可被 GC 中途回收 |
| 7 | async 内同步 IO | `export/zip_exporter.py:143-167`、`monitor/downloader.py:122-133`、`s3_store.py:236` | 大文件导出/下载卡 UI |
| 8 | `assert` 作运行时前置条件 | 约 140 处（`_channels_sqlite.py` 18、`_channels_pg.py` 17…） | 无消息，被外层宽泛 except 吞成普通失败；改显式 `raise` |
| 9 | 失效 noqa | 226 条中 129 条（57%） | RUF100 未进门禁 |
| 10 | 静默吞异常 | 16 处 `pass` 无日志，如 `core/monitor/backfill.py:76,128` | 元数据查询失败被完全丢弃 |
| 11 | 魔法数字散落 | 模块级数值常量全仓仅 12 个 | `limit=1000` ×7、延时 500/1000 ×4 组、200MB ×3、超时 2.0 ×4 |

### 架构

| # | 问题 | 证据 | 建议 |
|---|---|---|---|
| 12 | "self 透传"式拆分：文件拆了，耦合没拆 | helper 访问宿主私有属性：SQLite 190 / PG 103 / MediaService 66 处 | helper 接收 `(conn, lock)`，不再接收宿主 |
| 13 | 异常没有统一出口 | 自定义异常 7 个、无公共基类；storage / objectstore 内无一处 `except sqlite3` / `asyncpg`；`cli/main.py:325` 只 catch `CliError` | adapter 边界转 `StorageError` / `ObjectStoreError`，共用 `TgMonitorError`；CLI 顶层兜底 |
| 14 | UI 类型检查基本关闭 | 62 个 ui 文件里 47 个带整文件 `# mypy: disable-error-code`；`type: ignore` 85 处（UI 74） | 先对 `ui/controllers/`、`ui/viewmodels/` 打开 |
| 15 | 查询接口参数爆炸 | `list_messages` 10 参数、`list_media` 9 参数，各贯穿 5 层 | 收成 `MessageQuery` / `MediaQuery` |
| 16 | 头像删除永不落库 | `core/telegram/_telethon_handlers.py:89` 发 `local_path=None`，`monitor/event_handlers.py:254-257` 见 None 直接 return | None 的双重含义，用显式事件表示 |
| 17 | 大文件内聚性差 | `settings_page.py`（28 方法覆盖 7 个区域）、`lightbox_dialog.py`（52 方法，8 个纯转发）、`_mm_render.py:RenderMixin`（595 行） | 按区域拆；`telethon_client.py`、`events.py`、`dto.py` 不值得拆 |
| 18 | 双后端重复实现 | `upsert_channel` 每后端 4 个同骨架变体；`_media_to_row` / `_row_to_media` 两后端逐字相同；文件大小格式化 ×3 且输出不一致 | 共享代码上提（PG 已在从 `_sqlite_common` 取 `_ensure_utc`，位置不对） |
| 19 | UI 内部耦合 | `ui/controllers/_base.py:MainWindowCtx` 17+ 字段被 8 个模块依赖；多处直接写 `main_window._xxx` | 收敛成小接口 |
| 20 | 唯一真实模块级 import 环 | `storage/_messages_pg` ↔ `storage/_media_pg` | 上提共享 SQL 片段 |

### 测试

| # | 问题 | 证据 | 建议 |
|---|---|---|---|
| 21 | `_STORAGE_MODULES` 清单已漂移 | `tests/test_storage_migration_2006.py:19-38` 列 16 个，存储目录实有 22 个 .py（漏 `postgres_repo.py`、`repository.py`、`_media_codec.py` 等），注释写"全部纳入" | 改按目录 glob 自动发现 + 数量下限断言 |
| 22 | 测试直调私有成员 | 70 处 / 19 文件；读私有属性 219 处；`test_reconfigure.py` 13 处打私有模块 | 让 handler 走公开入口 |
| 23 | 源码文本型测试 | `test_app_run.py` 11 项、`test_app_startup.py` 9 项、`test_cli.py:143-150` 硬编码 11 个模块名 | 改成行为测试；遍历 `CLI_SUBCOMMANDS` |
| 24 | UI 控制器裸奔 | `sync_controller.py`、`settings_reaction_controller.py`、`export_controller.py`、`export_dialog.py`、`nav_bar.py`、`_qt_callbacks.py` 类名 0 命中 | 优先补控制器 |
| 25 | 强制等真实超时 | 4 个测试各等 2 秒，每次全量至少 8 秒（`test_app_service_facade.py:154,187,204`、`test_p2_lifecycle_fixes.py:72`） | 超时常量可注入 |
| 26 | 无断言测试 7 项 | 如 `test_monitor_channel_metadata.py:68` 注释写"storage 不被调"但没断言 | 补断言或删除 |
| 27 | property 测试测的是替身 | `@given` 模糊的是 `InMemoryRepository` | 策略复用到 SQLite |
| 28 | 空转 job | `windows-paint-probe` 只跑 `test_status_bar.py`，其环境变量无人读 | 删除或接上 |
| 29 | 同名 fixture 遮蔽 18 处 | `bus` 8、`storage` 4 等 | 收口到 conftest |

### 文档

| # | 问题 | 证据 | 建议 |
|---|---|---|---|
| 30 | SECURITY.md 整篇停在 0.2.x | 支持版本表写 0.2.x；登录方式写"手机号 + 验证码"；session 路径写 `./data/session/` | 重写 |
| 31 | AGENTS.md 漂移且过重 | 22.6KB 中 PySide6 6.12 归因史占 21%；列了不存在的 `ui/theme.py`；"7 批量 op"实为 6；"1604 用例"实为 1114；`mypy>=2.3.0`、`pre-commit>=3.7` 与 pyproject 不符；"提示去 GUI 登录"与 headless 登录不符；CLI 有 7 处文案引用已删的 `tgmonitor xxx` | 历史迁 `docs/history/` 留一行指针，约减 27% |
| 32 | `.env.example` 缺 10 个变量 | 缺 `TG_MEDIA_MAX_BYTES`、`TG_SYNC_*`、`TG_PAUSED`、`TG_CLOSE_ON_EXIT`、`TG_SESSION_DIR`、`TG_DATA_ROOT` 等；`TG_SQLITE_PATH=./data/...` 相对 cwd，与平台数据目录设计冲突；`TG_LOG_LEVEL` 不走 `.env` | 补全并校正 |
| 33 | CHANGELOG 失范 | 307KB / 5045 行 / 85 个版本节；名义 Keep a Changelog；`[Unreleased]` 链接仍指 `v1.8.2`；数字与提交不符 | 旧版本归档到 `CHANGELOG-legacy.md`，修链接，勘误 2.1.6 |
| 34 | 历史文档无状态头 | 7 份 PRD 约 400KB；`PRD-2.1.0-CLI.md` 仍"待评审"且 systemd 示例用已删命令；`REVIEW.md` 是 TDLib 时代；`ATTRIBUTIONS.md` 仍提 `tdlib_json` | 移 `docs/history/`，加 `docs/README.md` 索引 |
| 35 | 陈旧注释约 22 条 | `cli/_common.py:153`、`cli/auth.py:71` 等提示用 `tgmonitor login`；`settings_page.py:282` docstring 仍列 "Phone"；`core/channel_sync/__init__.py:3-4` 称 `AppService.sync_channels`（不存在）；`.pre-commit-config.yaml:52` 引用不存在的 `secret-scanning.yml`；`pyproject.toml:13` 路径写错 | 随相关改动清理 |
| 36 | 用户文档缺口 | 无 GUI 使用说明、"订阅 vs 监听"语义只在一份 PRD 里、日志位置 `logs/tgmonitor.log` 无文档、无卸载说明 | 按此顺序补 |

### 工程化 / CI【取证】

| # | 问题 | 证据 | 建议 |
|---|---|---|---|
| 37 | Action 未固定 SHA | 22 处 `uses:` 全部 `@vN` | dependabot 配合 SHA 固定 |
| 38 | appimagetool 无校验 | `build.yml:51-52` 从可变的 `continuous` 下载，仅 `-k` | 固定版本并校验 sha256 |
| 39 | 无 secret 扫描 / CodeQL | pip-audit 仅周 cron | 补 gitleaks 与 CodeQL |
| 40 | 权限未最小化 | 仅 `build.yml` 声明 `permissions` | 顶层 `permissions: contents: read` |
| 41 | 无 lock 漂移门禁 | 所有 job `uv sync` 无 `--locked` | 加 `uv lock --check` |
| 42 | ruff 版本漂移 | CI `ruff>=0.5` 浮动、pre-commit `v0.6.9`、本地 0.17.6 | 两端钉同一版本 |
| 43 | 安装步骤重复 | `Install uv` ×8、apt 列表 ×3 且已漂移 | 抽 composite action |

## 六、验证健康的部分

- **分层**：`core/` 对 PySide6 / qasync import 为 0，`tests/test_architecture.py` 用 AST 守护（含延迟 import）；UI / CLI 对 storage / objectstore / telegram 实现模块 import 为 0。
- **契约一致**：存储 52 个抽象方法在 ABC / 两个 facade / InMemory 五层签名一致；Telegram Protocol 三个实现 20 个方法形参名一致；`test_app_service_surface.py` allowlist 守护有效。
- **安全基础**：路径穿越无（`resolve()` + `relative_to` + 拒绝 `..`）；CSV 公式注入、Markdown、HTML autoescape、ZIP `_sanitize_arcname` 齐全；临时文件名用 `secrets`；无 `eval` / `pickle`；QR token 不进日志。
- **测试基建**：全会话单后台 loop、per-test `deleteLater`、`sessionfinish` 停线程；`timeout=300` + thread 方式；`patch` 仅 3 处，主路径靠 `FakeTelegramClient` / `InMemoryRepository` / fake S3 走通；存储对等用例在 InMemory 与 SQLite 上参数化。
- **文档基础**：模块 docstring 覆盖 100%；版本 6 处一致；13 份文档的本地链接全部有效；README 的 13 个子命令名全部存在。
- **CI**：所有 job 有 `timeout-minutes`；三平台门禁 + integration + coverage 隔离。

## 七、建议的 2.1.7 范围与顺序

**PR-A 止血（小而确定，CI 可自证）**
P1-1 扩展名白名单 → P1-2 订阅一次性化（补计数测试）→ P1-3 哨兵 `put_nowait` → P1-6 日期预设转 aware（补时区测试）→ P1-8 打码 → P1-7 README 三处 → P2-1 / P2-2。
再单独一个 PR 处理 P1-4（删 legacy hook）——先确认 23 项仍全绿再删。

**PR-B 门禁加固**
P1-10 `--fail-under` 棘轮 → P1-5 integration 断言 skipped=0 + 三后端参数化 → P2-21 glob 发现 → RUF100 进 select 并自动清 129 条 → P2-28 删空转 job。

**PR-C 文档校准**
AGENTS.md 瘦身 → SECURITY.md 重写 → `.env.example` 补全 → CHANGELOG 勘误 → `docs/history/` 归档与索引 → 陈旧注释清理。

**留到 2.1.8+（需要设计，不宜塞进发版前）**
P1-9 `AppService.start()` 与 reconfigure 阶段 2 原子化 → P2-13 异常基类 → P2-15 Query 对象 → `settings_page.py` 拆分 → `app.run()` 冒烟测试 → UI mypy 渐进打开 → CI SHA 固定 / CodeQL / gitleaks / composite action。

## 八、取证方式

- 四路只读并行审计（架构 / 编码 / 测试 / 文档），脚本统一输出到 `/tmp/tgmon-audit/`。
- 全量测试带覆盖率实跑：`coverage run -m pytest`，数据文件在 `/tmp`，未覆盖仓库内的 `.coverage`。
- 三个运行时缺陷由独立复现脚本确认：`repro_stream_close.py`、`repro_batch_cancel2.py`、`repro_date_filter.py`。
- 审计全程 `git status` 保持干净（审核阶段本文件为唯一新增、未提交；之后已随 #165 入库，处理状态见第〇节）。
