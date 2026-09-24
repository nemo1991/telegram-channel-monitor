"""SchemaReport — 启动期 introspect 出的 schema drift 报告。

2026-09-23 v1.8.x:`introspect_schema()` 返回这个 dataclass,
`app.py:_bootstrap` 据此决定是否触发 `repair_schema()`(默认仅 log)。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ColumnDrift:
    """单列 drift 详情。"""

    table: str
    column: str
    expected_type: str
    actual_type: str


@dataclass(frozen=True)
class SchemaReport:
    """汇总 schema drift 报告。

    - `missing_tables`:整表缺失(运维严重,repair 拒绝 DROP+CREATE)
    - `missing_columns`:(table, column, expected_type) — 期望列不存在,
      repair 走 `ALTER TABLE ADD COLUMN IF NOT EXISTS`(幂等 / 无锁)
    - `wrong_types`:ColumnDrift 列表 — 类型不符,repair 仅 log,不动
      (类型 ALTER 会重写表,对大表 lock-heavy)
    - `extra_columns`:(table, column, actual_type) — 诊断信息,可能来自
      遗留的手动 schema work;repair 仅 log,不删(backward-compat)

    `ok` 定义为无 `missing_tables` 与 `missing_columns`(drift 影响启动);
    `wrong_types` / `extra_columns` 不影响 ok(它们仍要 log,但不阻塞)。
    """

    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[tuple[str, str, str]] = field(default_factory=list)
    wrong_types: list[ColumnDrift] = field(default_factory=list)
    extra_columns: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing_tables and not self.missing_columns

    def summary(self) -> str:
        parts: list[str] = []
        if self.missing_tables:
            parts.append(f"missing_tables={self.missing_tables}")
        if self.missing_columns:
            parts.append(f"missing_columns={len(self.missing_columns)}")
        if self.wrong_types:
            parts.append(f"wrong_types={len(self.wrong_types)}")
        if self.extra_columns:
            parts.append(f"extra_columns={len(self.extra_columns)}")
        return "; ".join(parts) if parts else "ok"


__all__ = ["ColumnDrift", "SchemaReport"]
