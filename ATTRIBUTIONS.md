# Third-party Attributions

本项目使用了一些第三方资源,致谢并按其许可声明如下。

## Lucide Icons

`resources/icons/*.svg` 中除 `app_icon.svg` 外的所有图标衍生自
[Lucide](https://lucide.dev/)(ISC license,Copyright (c) 2020, Lucide
Contributors)。

本项目对原始资产做的修改:

- **stroke-width: 2 → 1.75** —— 适配 tgmonitor 工具栏在 16/20/24 px 下
  视觉重量(Lucide 推荐 1.5~2 之间,1.75 是中位)

涉及文件:

| 文件 | 衍生自 |
|---|---|
| `src/tgmonitor/resources/icons/refresh.svg`            | Lucide `refresh-cw` |
| `src/tgmonitor/resources/icons/export.svg`             | Lucide `download`   |
| `src/tgmonitor/resources/icons/settings.svg`           | Lucide `settings`   |
| `src/tgmonitor/resources/icons/kind_channel.svg`       | Lucide `megaphone`  |
| `src/tgmonitor/resources/icons/kind_supergroup.svg`    | Lucide `users`      |
| `src/tgmonitor/resources/icons/kind_group.svg`         | Lucide `user-round` |
| `src/tgmonitor/resources/icons/nav_dashboard.svg`      | Lucide `layout-dashboard` |
| `src/tgmonitor/resources/icons/nav_settings.svg`       | Lucide `settings`   |
| `src/tgmonitor/resources/icons/nav_live.svg`           | Lucide `radio`(2 道大弧 + 中心点) |
| `src/tgmonitor/resources/icons/nav_channels.svg`       | Lucide `list`(3 dot + 3 line) |

`app_icon.svg` 是项目自有设计(信号塔 + 频道条),不属于 Lucide。

## tdlib_json(自编译 libtdjson 的 ctypes 绑定)

`packages/tdlib_json/` 子项目中的 `tdjson.py` 改编自
[aiotdlib](https://github.com/pylakey/aiotdlib)(MIT License,Copyright (c)
pylakey)。`_vendor_ref/aiotdlib/` 是参考源码备份,仅作对照阅读,**不随包分发**。

aiotdlib 仓库已归档,本项目以自编译 libtdjson(锁定 TDLib 1.8.46)+ ctypes
绑定替代,不再依赖 aiotdlib 及其安装期下载的 TDLib 二进制。
