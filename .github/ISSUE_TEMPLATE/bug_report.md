---
name: 🐛 Bug Report
about: 报告一个 bug,帮助我们改进
title: "[Bug] "
labels: bug
assignees: ""
---

## 描述

清晰简洁地描述这个 bug。

## 复现步骤

1. ...
2. ...
3. ...

## 预期行为

应该发生什么。

## 实际行为

实际发生了什么(包括完整 traceback 与日志)。

## 环境

- **OS**: (e.g. macOS 14.5, Ubuntu 24.04, Windows 11)
- **Python**: (e.g. 3.12.3)
- **版本**: (e.g. 0.1.0, commit SHA)
- **数据库后端**: postgres / mongo
- **对象存储后端**: local / s3 (S3 兼容:AWS/MinIO/OSS)
- **安装方式**: pip / uv / 源码

## 配置(去掉敏感信息)

```env
TG_DB_BACKEND=...
TG_OBJECTSTORE_BACKEND=...
TG_MEDIA_POLICY=...
# 不要贴 API_ID / API_HASH / PHONE / 验证码 / session
```

## 截图 / 录屏

如果适用,加截图或录屏帮助解释问题。

## 补充

任何其他相关信息。
