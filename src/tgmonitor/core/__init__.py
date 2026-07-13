"""core 包 — Telegram 业务逻辑(无 UI 依赖)。

子包:
    telegram     官方 TDLib 集成(唯一接触 TDLib 之处)
    monitor      频道监听服务
    storage      消息数据持久化(PostgreSQL / MongoDB)
    objectstore  媒体二进制对象存储(S3 / Local)
    export       导出服务与各格式 Exporter

边界守则:本包及其子包禁止 import PySide6 / qasync / 任何 UI 框架。
"""
