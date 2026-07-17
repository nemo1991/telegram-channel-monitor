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
3. 安装开发环境:
   ```bash
   pip install -e ".[all,postgres,mongo,objectstore]"
   pip install pytest pytest-asyncio ruff
   ```
4. 跑测试确保基线绿:
   ```bash
   PYTHONPATH=src pytest
   ruff check src tests
   ```

### 开发

**架构边界守则**(强约束):

- ✋ `core/` 包**禁止** import `PySide6` / `qasync` / 任何 UI 框架
- ✋ UI **只能** import `AppService` / `EventBus` / DTO / 必要的 `core.events` 领域事件类
- ✋ 跨边界**必须**传 DTO,不允许 TDLib 原生对象或 ORM 行对象
- ✋ 新增数据库/对象存储/导出格式 = 加一个实现类 + 在工厂注册;**不要**改 if/elif 链
- ✋ 不要在 `core/` 内 `print()`,用 `logging.getLogger(__name__)`

**代码风格**:

- Python 3.11+ 特性(`str | None` / `dataclass(slots=True)` / `match` 等)
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
- 跑过 `pytest` + `ruff check` 再 push

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

## 🌐 代理与 aiotdlib 调试

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

### 开发 aiotdlib 路径

- `aiotdlib>=0.16` 是硬性要求(`pyproject.toml` dependencies)。低于 0.16
  的版本 `ClientSettings` API 不同,会启动失败
- 本项目在 `core/telegram/factory.py` 优先 import 真 `aiotdlib.AiClient`,
  失败回落到 `FakeTelegramClient`(开发 / CI 无凭据时)
- TDLib 二进制由 aiotdlib 在 install 阶段自行下载;如果网络受限,手动
  放到 `aiotdlib/tdlib/` 下

### 看 TDLib 日志

```bash
# 启动时抬高 verbosity
TD_LOG_LEVEL=DEBUG python -m tgmonitor

# 或者只读 session 文件里的事件
TG_SESSION_DIR=./data/session   # 默认
```

TDLib 会在 stderr 里吐原始 `updateAuthorizationState` / `updateNewMessage`
事件流 —— 这些是 `core/telegram/tdlib_client.py` 订阅的源头,改它之前先确认
这里的事件字段确实有变化。

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
