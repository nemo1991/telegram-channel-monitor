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
- ✋ 跨边界**必须**传 DTO,不允许 TDLib 原生对象或 ORM 行对象
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

## 🌐 代理与 TDLib 调试

国内 / 受限网络下,Telegram 服务器常需要经 SOCKS5 代理。本节说怎么
**启用** 与 **调试** 这条路,不解释 Telegram 协议本身(那是 TDLib 文档的事)。

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

### 编译 libtdjson

- TDLib 引擎 `libtdjson` 由 `scripts/build_libtdjson.sh` 自编译(锁定 TDLib
  1.8.46),产物放进 `packages/tdlib_json/src/tdlib_json/tdlib/`。dylib 被
  gitignore,**首次 clone 后必须先跑一次**(见「常用命令」)
- 工具链:macOS `brew install cmake gperf openssl@3`;Linux `apt install
  cmake gperf libssl-dev zlib1g-dev build-essential`
- 编译完可跑 `PYTHONPATH=src uv run pytest tests/test_tdlib_client.py` 验证
- 凭据缺失时 `core/telegram/factory.py` 返回 `UnconfiguredTelegramClient`
  占位实现,开发 / CI 无凭据也能启动应用

### 看 TDLib 日志

`tdlib_client.py` 启动时把 TDLib verbosity 设为 0(静默)。需要调试时,临时把
`_do_start_inner` 里的 `setLogVerbosityLevel` 调高(如 4),TDLib 会在 stderr
吐原始 `updateAuthorizationState` / `updateNewMessage` 事件流 —— 这些是
`core/telegram/tdlib_client.py` 订阅的源头,改它之前先确认这里的事件字段确实有变化。

---

## 🪟 Windows 原生编译

Windows 用 vcpkg 编译 libtdjson(TDLib 官方推荐的 Windows 构建方式),产物按
Linux / macOS 相同命名规则放进包内 `tdlib/` 目录,ctypes 绑定自动识别:

```powershell
# 需 vcpkg + Visual Studio C++ 工具链(CI 的 windows-latest 自带)
uv sync --all-extras
.\scripts\build_libtdjson.ps1        # vcpkg install tdlib + 拷贝产物
PYTHONPATH=src uv run pytest tests/test_tdlib_client.py   # 验证
```

脚本把 `vcpkg install tdlib` 的产物 `tdjson.dll`(重命名为
`libtdjson_windows_amd64.dll`)连同依赖 dll(openssl / zlib)一并拷到
`packages/tdlib_json/src/tdlib_json/tdlib/`;ctypes 加载时经
`os.add_dll_directory` 解析同目录依赖。

> **为什么 Windows 现在进了 CI?** 测试本身不加载真实 libtdjson(conftest 的
> `stub_tdlib_init` 全拦截),windows-latest 无需编译引擎即可跑 pytest;打包侧
> build.yml 已加 windows-latest,vcpkg 编译引擎 + PyInstaller 产出 onedir zip。
> 视觉回归(`tests/test_visual_regression.py`)2026-08-12 起不进任何 CI(golden
> 跨机渲染不稳,只在本地跑)。

### Windows + WSL2(推荐)

Windows 11 用户装 WSL2,体验跟 Linux 完全一致:

```powershell
wsl --install -d Ubuntu-24.04   # 一次性,需要重启
# 在 Ubuntu 终端里:
git clone https://github.com/nemo1991/telegram-channel-monitor.git
cd tgmonitor
uv sync --all-extras
uv run python -m tgmonitor   # GUI 走 WSLg
```

`libtdjson` 由 `scripts/build_libtdjson.sh` 编译,代理 / 路径 / shell 行为跟 CI 一致。

---

## 📦 发布

打 tag 即发布,CI 全自动:

```bash
# 1. 本地过全部测试、CI 全绿(见上方「PR 流程」)
# 2. 打版本 tag 并推送
git tag v1.0.8
git push origin v1.0.8
```

- `build.yml`(tag `v*`):三平台矩阵(Linux/macOS/Windows)先编译 libtdjson →
  PyInstaller 打包 → Linux AppImage + macOS .app zip + Windows onedir zip →
  `release` job 自动建 GitHub Release 并附 `SHA256SUMS`。
- **缓存预热**:GitHub 缓存规则是不同 tag 的 run 之间不能互相恢复,只有默认
  分支(main)写入的缓存任何 ref 都能命中。因此 push main 时 `warm-cache` job
  只编译 libtdjson、不打包,把三平台产物写回 main scope;之后打 tag 发布用
  同一 key 直接命中,Windows vcpkg 冷编译约 2h → 命中后仅几分钟。
- 缓存 key 为 `libtdjson-${{ runner.os }}-${{ runner.arch }}-v1`,7 天未访问会
  被 GitHub 自动清除;需要作废时把 `v1` 改成 `v2` 即可。
- 重复打同一个 tag / `workflow_dispatch` 手动重跑也会命中缓存,产物不变时编译一步秒过。

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
