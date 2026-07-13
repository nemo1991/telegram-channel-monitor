## 变更说明

<!-- 简述本次 PR 做了什么,为什么 -->

## 变更类型

<!-- 请勾选适用的项 -->

- [ ] 🐛 Bug fix(非破坏性变更,修复 issue)
- [ ] ✨ New feature(非破坏性变更,新增功能)
- [ ] 💥 Breaking change(破坏性变更,会导致现有行为变更)
- [ ] 📝 Documentation(仅文档)
- [ ] ♻️ Refactor(代码重构,无功能变化)
- [ ] ⚡ Performance(性能优化)
- [ ] ✅ Test(仅测试)

## 相关 Issue

<!-- 使用 `Closes #123` / `Fixes #456` / `Refs #789` -->

## 架构边界检查

<!-- 必填项 -->

- [ ] 没有在 `core/` 包内 import PySide6 / qasync / 任何 UI 框架
- [ ] 跨层只传 DTO,没有泄漏 TDLib / ORM 类型
- [ ] 新增功能有对应的测试(`tests/`)
- [ ] 单测全离线(用 Fake / InMemory / Local,不依赖真实 Telegram/DB/S3)

## 改动清单

- 文件 1:简述
- 文件 2:简述

## 测试

```bash
$ PYTHONPATH=src pytest -v
...
$ ruff check src tests
...
```

## 截图(UI 变更必填)

| Before | After |
|---|---|
|  |  |

## Checklist

- [ ] 我的代码遵循项目代码风格(`ruff check` 0 警告)
- [ ] 我添加了必要的单测,且全过
- [ ] 我更新了相关文档(README / CHANGELOG / docs/)
- [ ] 我没有在 commit / PR 中粘贴任何敏感凭据(API_ID / API_HASH / PHONE / 验证码)
- [ ] 我已阅读并遵守 [CONTRIBUTING.md](../CONTRIBUTING.md)
