# 🤝 Contributing to tgmonitor

感谢你考虑为本项目做贡献!🎉

## 📋 行为准则

本项目采用 [Contributor Covenant](https://www.contributor-covenant.org/) 精神:
- 友善、包容、专业
- 尊重不同观点与经验
- 聚焦对社区最有利的事

---

## 🐛 报告 Bug

提交 issue 前请:

1. 搜索现有 issue,避免重复
2. 确认使用最新版
3. 收集:**Python 版本** · **OS** · **后端选择** · **复现步骤** · **完整 traceback** · **相关日志**

模板见 [.github/ISSUE_TEMPLATE/bug_report.md](.github/ISSUE_TEMPLATE/bug_report.md)。

## ✨ 提 Feature Request

清楚描述:
- 想解决什么问题
- 期望行为 vs 实际行为
- 替代方案 / 参考实现

模板见 [.github/ISSUE_TEMPLATE/feature_request.md](.github/ISSUE_TEMPLATE/feature_request.md)。

---

## 🔧 提 Pull Request

### 准备

1. Fork & clone
2. 创建分支:`git checkout -b feat/my-feature` 或 `fix/my-bug`
3. 安装开发环境(uv 一步搞定,跟 CI 一致):
   ```bash
   uv sync --all-extras --group dev
   ```
4. **可选**(强烈推荐)启用 pre-commit 钩子 — 本地 commit 前自动跑 ruff /
   trailing-whitespace / gitleaks,与 CI 门控完全一致,免去「本地过 → CI 红」往返:
   ```bash
   uv run pre-commit install
   uv run pre-commit run --all-files  # 首次手动跑一遍,之后自动
   ```
5. 跑测试确保基线绿:
   ```bash
   PYTHONPATH=src uv run pytest
   uv run ruff check src tests
   uv run ruff format --check src tests
   ```

### 开发

**架构边界守则**(强约束):

- ✋ `core/` 包**禁止** import `PySide6` / `qasync` / 任何 UI 框架
- ✋ UI **只能** import `AppService` / `EventBus` / DTO / 必要的 `core.events` 领域事件类
- ✋ 跨边界**必须**传 DTO,不允许 Telegram 原生对象(Telethon `Message` / `Peer` 等)或 ORM 行对象
- ✋ 新增数据库/对象存储/导出格式 = 加一个实现类 + 在工厂注册;**不要**改 if/elif 链
- ✋ 不要在 `core/` 内 `print()`,用 `logging.getLogger(__name__)`

**代码风格**:

- Python 3.13 特性(`str | None` / `dataclass(slots=True)` / `match` 等 3.10+ 语法;Python 锁死 3.13)
- 全部公共方法用 type hints
- 用 `from __future__ import annotations`
- 用 ruff:`ruff check` 必须 0 警告
- 函数/方法第一行用 docstring 简述;复杂逻辑加行内注释

**测试**:

- 新功能必须带测试(放 `tests/`)
- core 单测**必须**全离线(用 `FakeTelegramClient` + `InMemoryRepository` + `LocalObjectStore`)
- UI 不强制单测(可手动验证)
- 目标覆盖率 ≥ 80% for `core/`

### 提交

- Commit message 风格:
  ```
  type(scope): subject
  
  body (optional)
  
  footer (optional, e.g. Closes #123)
  ```
  `type` ∈ {`feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`}
- 一个 PR 一个主题;避免巨型 diff
- 跑过 `pytest` + `ruff check` + `ruff format --check` 再 push
- pre-commit install 后本地 commit 前自动跑同一套检查

### PR 流程

1. push 到 fork:`git push origin feat/my-feature`
2. 在 GitHub 开 PR,填写 [PR 模板](.github/PULL_REQUEST_TEMPLATE.md)
3. CI 必须绿
4. 等待 review
5. Squash merge

---

## 🧱 添加新的存储 / 对象存储 / 导出后端

### 新增数据库后端

1. 在 `core/storage/` 下新建 `xxx_repo.py`,继承 `StorageRepository` ABC
2. 实现所有 abstract 方法(查询语义必须与现有实现等价)
3. 在 `core/storage/factory.py` 的 `build_storage()` 加分支
4. 在 `core/config.py` 的 `DBBackend` 枚举加值
5. `pyproject.toml` 的 `[project.optional-dependencies]` 加新依赖
6. `README.md` 文档同步
7. 写单测

### 新增对象存储后端

步骤同上,抽象为 `ObjectStore`(在 `core/objectstore/base.py`)。

### 新增导出格式

1. 在 `core/export/` 下新建 `xxx_exporter.py`
2. 类继承 `Exporter` ABC
3. 用 `@exporter(ExportFormat.XXX)` 装饰器注册
4. `core/dto.py` 的 `ExportFormat` 枚举加值
5. `ExportDialog` 会在下拉框自动出现(因 UI 通过 `EXPORTERS.available()` 取)
6. 写单测

---

## 🌐 代理与 Telethon 调试

国内 / 受限网络下,Telegram 服务器常需要经 SOCKS5 代理。本节说怎么
**启用** 与 **调试** 这条路,不解释 Telegram 协议本身(那是 Telethon / MTProto 文档的事)。

### 启用 SOCKS5 代理

1. 起一个 SOCKS5 代理(本地 ss / outline / ssh -D 都可以)
2. 在 `.env` 加一行(用户名密码可省,主机端口必有):
   ```env
   TG_PROXY=socks5://[user:pass@]host:port
   ```
3. 启动 `python -m tgmonitor`,侧栏「账户」状态点会先转「配置中」,代理通了才能继续登录
4. UI 也可改:设置 → 网络代理 → 填同样的 URL → 点「测试连接」→
   保存并应用(走 `SettingsChanged` 事件,无需重启 app)

`Settings.proxy`(pydantic 字段)会校验 URL 格式;非法值会抛 `ValueError`,
UI 弹错误框。

### Telethon 后端(纯 Python,无需编译)

- v2.0.0 起 Telegram 后端是 [Telethon](https://github.com/LonamiWebs/Telethon)
  纯 Python MTProto 实现,**不再需要编译 libtdjson / 任何原生引擎**。`uv sync`
  装好 `telethon` + `cryptg`(加密加速)+ `python-socks`(SOCKS5)即可跑。
- 真实实现 `core/telegram/telethon_client.py` `TelethonTelegramClient`;
  消息归一化在 `telethon_messages.py`(Telethon `Message` → `MessageDTO` / `MediaDTO`)。
- session 落在 `session/tgmonitor.session`(Telethon SQLite 格式);**与 v1.x 的
  TDLib session 不兼容**,升级后需重新登录。
- 凭据缺失时 `core/telegram/factory.py` 返回 `UnconfiguredTelegramClient`
  占位实现,开发 / CI 无凭据也能启动应用。
- 离线测试:`PYTHONPATH=src uv run pytest tests/test_telethon_client.py
  tests/test_telethon_messages.py`(全 fake,不连真实 Telegram)。

### 看 Telethon 日志

`telethon_client.py` 用标准 `logging`。需要调试时,把 `tgmonitor.core.telegram`
logger 调到 `DEBUG`(或临时 `logging.basicConfig(level=logging.DEBUG)`),
即可看到 RPC 调用 / 更新流。Telethon 自身网络层日志在 `telethon` logger 下,
调高它可看原始 MTProto 收发。改 `subscribe_updates` / 消息映射前,先确认
Telethon 事件字段确实有变化。

### 修改 Protocol 签名 — 必做 checklist

`core/telegram/client.py` 的 `TelegramClient` Protocol(`@runtime_checkable`)
是 3 套实现的契约:

- `core/telegram/telethon_client.py` `TelethonTelegramClient`(生产)
- `core/telegram/fake_client.py` `FakeTelegramClient`(测试)
- `core/telegram/unconfigured.py` `UnconfiguredTelegramClient`(占位)

加 / 改 Protocol 任意方法的签名(参数 / kwarg / 返回类型),**必须**同步更新
**所有**实现,否则生产 `MediaDownloader` 等真路径会撞 `TypeError`。

> v2.0.0 起不再有 `inspect.signature` 自动签名守护测试(旧
> `test_protocol_method_signatures_match_tdlib_concrete` 随 TDLib 一起删除)。
> 现在靠 **mypy**(Protocol 结构化类型检查)+ **离线测试套件**
> (`test_telethon_client.py` / `test_telegram_batch_actions.py` 等)+ 本 checklist
> 人工把关。改签名后务必跑全套测试与 mypy。

只要触及 `core/telegram/client.py` Protocol,就**至少**要在以下文件里各改一处:

1. `core/telegram/client.py` Protocol
2. `core/telegram/telethon_client.py`(生产实现)
3. `core/telegram/fake_client.py`
4. `core/telegram/unconfigured.py`

签名漂移自检命令(本地):

```bash
PYTHONPATH=src QT_QPA_PLATFORM=offscreen uv run pytest \
  tests/test_telethon_client.py tests/test_telegram_batch_actions.py -v
PYTHONPATH=src uv run mypy src/tgmonitor
```

---

## 🪟 Windows 原生支持

v2.0.0 起 Telegram 后端是纯 Python(Telethon),**Windows 不再需要 vcpkg /
Visual Studio C++ 工具链编译任何原生引擎**。`uv sync` 装好依赖即可跑测试与打包:

```powershell
uv sync --all-extras --group dev
PYTHONPATH=src uv run pytest          # 全离线测试(fake,不连真实 Telegram)
uv run pyinstaller --clean -y tgmonitor.spec   # 产出 onedir zip
```

> 测试全用 fake(`FakeTelegramClient` / `InMemoryRepository` / `LocalObjectStore`),
> 不加载真实 Telegram 后端,windows-latest 无需任何引擎即可跑 pytest;打包侧
> build.yml 的 windows-latest 直接 PyInstaller 产出 onedir zip。

### Windows + WSL2(推荐)

Windows 11 用户装 WSL2,体验跟 Linux 完全一致:

```powershell
wsl --install -d Ubuntu-24.04   # 一次性,需要重启
# 在 Ubuntu 终端里:
git clone https://github.com/nemo1991/telegram-channel-monitor.git
cd telegram-channel-monitor
uv sync --all-extras
uv run python -m tgmonitor   # GUI 走 WSLg
```

代理 / 路径 / shell 行为跟 CI 一致。

---

## 📦 发布

打 tag 即发布,CI 全自动:

```bash
# 1. 本地过全部测试、CI 全绿(见上方「PR 流程」)
# 2. 打版本 tag 并推送
git tag v2.0.0
git push origin v2.0.0
```

- `build.yml`(tag `v*` 或 `workflow_dispatch`):三平台矩阵(Linux/macOS/Windows)
  `uv sync` → 编译 Qt 翻译(.ts→.qm)→ PyInstaller 打包 → Linux AppImage +
  macOS .app zip + Windows onedir zip → `release` job 自动建 GitHub Release 并附
  `SHA256SUMS`。
- v2.0.0 起后端纯 Python,**无原生库编译**,三平台分钟级完成;不再有
  `warm-cache` 预热 job / vcpkg 缓存 key(那些是 TDLib 时代的产物,已删)。
- `workflow_dispatch` 可手动重跑打包(不建 Release,仅产 artifact)。

---

## 🔐 安全

**请勿**在 issue / PR / commit 中粘贴:

- ❌ `TG_API_ID` / `TG_API_HASH` / `TG_PHONE`
- ❌ 验证码 / 2FA 密码
- ❌ session 文件
- ❌ 个人 Telegram 聊天截图

发现安全漏洞请按 [SECURITY.md](SECURITY.md) 私下报告,不要公开 issue。

---

## 📜 许可证

贡献即同意按 [MIT License](LICENSE) 授权你的代码。
