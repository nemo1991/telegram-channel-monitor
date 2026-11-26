# 安全策略(Security Policy)

## 支持的版本

本项目只对**最新的发布线**提供安全修复;旧版本不再维护。

| 版本 | 安全修复 |
|---|---|
| 2.1.x(当前发布线,以 [Releases](https://github.com/nemo1991/telegram-channel-monitor/releases) 为准) | ✅ |
| 更早的 2.0.x / 1.x / 0.x | ❌ 请先升级 |

## ⚠️ 重要提示:本应用处理敏感凭据

`tgmonitor` 用你的 **Telegram 用户账号**登录(二维码扫码,只需 `api_id` / `api_hash`,不涉及手机号、验证码或 2FA 密码)。
登录后,本应用会在本地数据目录生成 Telegram **session 文件**,它能完整代表你的账号身份,**等同密码级别**。

| 内容 | 位置(数据目录见下) |
|---|---|
| session 文件 | `<数据目录>/session/tgmonitor.session` |
| 配置文件(含 `api_id` / `api_hash`、数据库口令、S3 密钥、代理口令) | `<数据目录>/.env` |
| SQLite 数据库(消息、媒体元数据) | `<数据目录>/tgmonitor.db` |
| 下载的媒体与缩略图 | `<数据目录>/media/` |
| 运行日志 | `<数据目录>/logs/tgmonitor.log` |

数据目录由平台决定:

- macOS:`~/Library/Application Support/tgmonitor`
- Linux:`$XDG_DATA_HOME/tgmonitor`(默认 `~/.local/share/tgmonitor`)
- Windows:`%LOCALAPPDATA%\tgmonitor`

### 请务必

- ✅ 在**可信的本机**运行;不要在公共 / 共享的机器上用个人账号登录
- ✅ 备份 / 迁移时,把**整个数据目录**当作密码文件对待(不只是 session)
- ✅ 定期检查 `api_id` / `api_hash` 是否泄露(可在 [my.telegram.org](https://my.telegram.org/apps) 重置)
- ✅ 怀疑泄露时,在 Telegram App → 设置 → 设备 → **终止其他会话**(这会让本地 session 失效,需重新扫码)

### 请勿

- ❌ 把 `.env`、session 文件、`tgmonitor.db` 提交到 git,或粘贴到 issue / 聊天记录
- ❌ 把数据目录复制到云盘、邮件、聊天记录
- ❌ 在 PR / issue 中粘贴真实凭据、`.env` 内容或 session 文件
- ❌ 把真实凭据上传到 CI(测试全部使用 fake,CI 不需要任何 Telegram 凭据)

`.gitignore` 已排除 `.env` / `data/` / `*.db`。session 文件默认在平台数据目录(仓库之外);若用 `TG_SESSION_DIR` 把它指进仓库,请自行确认没被跟踪。

## 🐞 报告漏洞

**请勿**通过公开 issue 报告安全漏洞。

请走 GitHub [Security Advisories](https://github.com/nemo1991/telegram-channel-monitor/security/advisories/new)(私密上报,仅维护者可见)。

请包含:

1. 漏洞描述与影响范围
2. 复现步骤 / PoC
3. 影响的版本
4. (可选)修复建议

### 响应 SLA

- 24 小时内确认收到
- 7 天内评估严重性
- 30 天内修复并发布补丁(或告知时间表)

## 🛡️ 已实施的安全措施

- **本地优先**:消息、媒体、配置全部落在本地数据目录。除 Telegram 本身外,只有在设置页显式配置了 Postgres / S3 后端时才会与其它外部服务通信。
- **凭据不进输出**:`config show` / `config get` 对敏感项打码,URL 类字段(`db_dsn` / `proxy`)单独做凭据脱敏;二维码登录 token 不写日志。
- **「打开媒体」扩展名白名单**:媒体文件名由远端频道控制,本地落盘 / 暂存后只允许用系统默认程序打开图片 / 视频 / 音频 / 文档 / 压缩包等白名单扩展名;`.bat` / `.exe` / `.html` / `.svg` 等可执行或可脚本化的类型一律拒绝。
- **路径安全**:对象存储与导出路径做 `resolve()` + 根目录约束,拒绝 `..` 穿越与越界键;ZIP 导出的条目名经净化。
- **注入防护**:CSV 导出转义公式前缀;HTML 导出模板 autoescape;Markdown 导出转义用户内容。
- **配置文件权限**:`.env` 以 `0600` 创建(临时文件写完后原子替换,不存在其它用户可读的窗口)。
- **DB 不存二进制**:媒体二进制全在对象存储,数据库只存 key + 元数据。
- **不使用危险原语**:无 `eval` / `pickle` / 内置 `exec`;临时文件名用 `secrets` 生成。
- **CI 无凭据**:GitHub Actions 不访问任何 Telegram / 数据库凭据,只跑离线单测与容器化的真 PG 集成测试。
- **依赖锁定与审计**:依赖由 `uv.lock` 锁定,CI 跑 `uv lock --check` 防漂移;`audit.yml` 每周跑 `pip-audit --strict`(含 dev 依赖),任何已知 CVE 即失败;本地可用 pre-commit 的 gitleaks 钩子拦截误提交的密钥。

## 📜 依赖审计(手动)

CI 每周自动跑一次;想本地复现:

```bash
uv tool install pip-audit
uv export --no-emit-project --no-hashes --format requirements-txt -o requirements-audit.txt
pip-audit --strict -r requirements-audit.txt
```

`pip-audit` 不读 `uv.lock`,必须先 `uv export`;直接裸跑 `pip-audit --strict` 审计的是工具自身的环境,不是本项目。

## 🙏 致谢

负责任地披露漏洞的 researcher 将在修复发布后致谢(除非你要求匿名)。
