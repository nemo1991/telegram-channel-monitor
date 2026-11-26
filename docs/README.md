# 文档索引

本目录分两类:**现行文档**(描述现状,随代码维护)和 **`history/`**(点时间快照,
只读)。找「现在是怎么做的」看前者;查「当初为什么这么做」看后者。

## 现行文档

| 文档 | 内容 | 读者 |
|---|---|---|
| [`../README.md`](../README.md) | 项目介绍、安装、快速上手、CLI 速查 | 使用者 |
| [`../AGENTS.md`](../AGENTS.md) | 项目指南:架构约束、模块一览、常用命令、测试策略、CI、扩展点(**只写现状**) | 开发者 / AI agent |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | 贡献流程、分支与提交规范、环境搭建 | 贡献者 |
| [`../SECURITY.md`](../SECURITY.md) | 敏感数据清单、漏洞上报渠道、已实施的安全措施 | 所有人 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 架构详解:分层、数据流、各域服务设计 | 开发者 |
| [`../CHANGELOG.md`](../CHANGELOG.md) | 版本变更记录 | 所有人 |
| [`../ATTRIBUTIONS.md`](../ATTRIBUTIONS.md) | 第三方资源与许可证致谢 | 维护者 |
| [`CODE-REVIEW-2.1.7.md`](CODE-REVIEW-2.1.7.md) | 2.1.7 发版前全面审核报告(**本轮进行中**,各条目状态与对应 PR 见文内) | 维护者 |
| [`ADR-001-plugin-system.md`](ADR-001-plugin-system.md) | 3.0.0 插件系统架构决策(已接受;进程内能力插件、双通道发现、窄写门面、五类能力) | 开发者 / 维护者 |
| [`ADR-002-app-startup-lifecycle.md`](ADR-002-app-startup-lifecycle.md) | P1-9 生命周期门面重构决策(已接受;参数化 start、准原子 reconfigure、并发守卫,拆两个 PR) | 开发者 / 维护者 |

## 历史文档(`history/`)

点时间快照,**会过期**。其中的命令、路径、模块可能已随版本变更;现状以
`AGENTS.md` / `README.md` / `ARCHITECTURE.md` 为准。每份文件顶部都有历史标注。

| 文档 | 内容 |
|---|---|
| [`history/pyside6-6.12-unban.md`](history/pyside6-6.12-unban.md) | PySide6 上限从 `<6.12` 放宽到 `<6.13` 的完整归因史(SIGSEGV 根因与反向验证) |
| [`history/CODE-REVIEW-2.1.4.md`](history/CODE-REVIEW-2.1.4.md) | 2.1.4 重度代码审核报告(P1 × 12,已在 2.1.5 修复) |
| [`history/REFACTOR-PLAN-2.1.3.md`](history/REFACTOR-PLAN-2.1.3.md) | 2.1.3 重构计划(契约止血 / 去重 / 结构拆分 / ruff 门禁),已完成 |
| [`history/SUBSCRIBED_DRIFT_ANALYSIS.md`](history/SUBSCRIBED_DRIFT_ANALYSIS.md) | `_subscribed` set 漂移风险专项分析(2026-07-31) |
| [`history/PRD-2.1.0-CLI.md`](history/PRD-2.1.0-CLI.md) | 完备 CLI 工具设计(13 个子命令;其中 `tgmonitor xxx` 形式的示例已过时,现为 `tgmon-cli`) |
| [`history/PRD-2.0.9.md`](history/PRD-2.0.9.md) | UI 测试瘦身 + 媒体页行操作重构 + 媒体按需下载 |
| [`history/PRD-2.0.7.md`](history/PRD-2.0.7.md) | 顶部与设置页减法 + 频道管理图标化 |
| [`history/PRD-2.0.6.md`](history/PRD-2.0.6.md) | 频道管理归位与设置页收敛(`subscribed` → `monitored` 改名) |
| [`history/PRD-2.0.5.md`](history/PRD-2.0.5.md) | 大盘空间释放与视觉统一 |
| [`history/PRD-2.0.4.md`](history/PRD-2.0.4.md) | 操作台整合与活动持久化 |
| [`history/PRD-2.0.2.md`](history/PRD-2.0.2.md) | 实时流只读化 |

## 约定

- **写现状的文档不要堆历史归因。** 归因过程、已完成计划、某次审核的完整报告,写完就放 `history/`,现行文档里最多留一行指针。
- 归档时用 `git mv`(保留历史),并在文件顶部加历史标注。
- 新增文档时在本索引登记一行。
