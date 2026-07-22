# 🔐 Security Policy

## 支持的版本

下表说明本项目哪些版本接收安全更新:

| 版本 | 支持 |
|---|---|
| 0.2.x | ✅ |
| < 0.2 | ❌ |

## ⚠️ 重要提示:本应用处理敏感凭据

`tgmonitor` 通过你的 **Telegram 用户账号** 登录(手机号 + 验证码 + 可选 2FA)。
本应用会在本地数据目录生成 Telegram **session 文件**(默认 `./data/session/`),
该文件能完整代表你的账号身份,等同密码级别。

### 请务必

- ✅ 在**可信的本机**运行;不要在公共 / 共享 / 服务器上跑
- ✅ 把 `data/session/` 目录的权限设为仅本人可读(`chmod 700 data/session`)
- ✅ `.gitignore` 已默认排除 `data/` 与 `.env`,不要 force-add
- ✅ 定期检查 `TG_API_ID` / `TG_API_HASH` 是否泄露(my.telegram.org 可重置)
- ✅ 怀疑泄露时,在 Telegram App → 设置 → 设备 → **终止其他会话**

### 请勿

- ❌ 把 `TG_API_ID` / `TG_API_HASH` 提交到 git 或粘贴到 issue
- ❌ 把 `data/session/` 目录复制到云盘 / 邮件 / 聊天记录
- ❌ 在 PR / issue 中粘贴验证码或 2FA 密码
- ❌ 把 `.env` 上传到 CI(使用 CI 的 secrets 功能)

## 🐞 报告漏洞

**请勿**通过公开 issue 报告安全漏洞。

请通过以下任一方式私下联系:

- 📧 Email: **security@forcetone.dev**(占位,实际使用前请替换为你自己的邮箱)
- 或:GitHub [Security Advisories](https://github.com/forcetone/tgmonitor/security/advisories/new)

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

- **本地优先**:session 默认存本地,不上传任何远程
- **DB 不存 BLOB**:媒体二进制全在 ObjectStore,DB 只存 key + 元数据
- **接口/实现分离**:避免凭据跨层泄漏
- **`.gitignore` 完备**:`.env` / `data/` / `*.db` / `__pycache__` 等
- **类型与 DTO 边界**:Telegram 类型不跨 core↔UI 边界
- **CI 无凭据**:GitHub Actions 不访问任何 Telegram 凭据,只跑单测

## 📜 依赖安全

CI 通过 `uv` 锁 `uv.lock`(`requires-python = "~=3.13.0"`),自动解析
PyPI 默认源。如需审计:

```bash
uv tool install pip-audit
pip-audit
```

## 🙏 致谢

负责任地披露漏洞的 researcher 将在修复发布后致谢(除非你要求匿名)。
