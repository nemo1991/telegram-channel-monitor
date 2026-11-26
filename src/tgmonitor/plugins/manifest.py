"""插件清单 — 身份、版本、能力声明。

`PluginManifest` 是插件的静态元数据,来源有二:
- entry-points 通道:包作者在 `plugin.toml` / 代码里声明;
- 本地目录通道:`<plugins_dir>/<id>/plugin.toml`。

宿主在激活前按 `min_app_version` 做单向门禁(宿主版本 >= 要求才加载),
避免新插件在旧宿主上因 API 缺失而崩溃。版本号用朴素三段式 tuple 比较,
不引入 packaging 依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 已知能力集合 — 与 ADR-001 的五类能力对齐。未知能力记 warning 但不拒绝加载,
# 因为未来版本可能新增能力名,旧宿主应能跳过而非崩。
KNOWN_CAPABILITIES = frozenset({"events", "export", "cli", "ui", "schedule"})

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(value: str) -> tuple[int, int, int]:
    """解析 `MAJOR.MINOR[.PATCH]` 前缀为三元组;非法输入返回 (0, 0, 0)。

    只取前缀,允许 `3.0.0rc1` / `v3.1` 这类后缀写法(宽松解析),
    但要求以数字段开头。解析失败不抛 — 版本门禁是防御,不是校验入口。
    """
    s = value.strip().lstrip("v")
    m = _VERSION_RE.match(s)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


@dataclass(frozen=True)
class PluginManifest:
    """插件静态元数据 — 发现阶段即可读取,不需要实例化插件。"""

    id: str
    name: str
    version: str = "0.1.0"
    min_app_version: str = "3.0.0"
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("PluginManifest.id 不能为空")
        # id 作为目录名 / enabled.json key / 配置命名空间,必须安全。
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.id):
            raise ValueError(f"PluginManifest.id 含非法字符: {self.id!r}")

    def is_compatible(self, app_version: str) -> bool:
        """宿主版本是否 >= 插件要求的最低版本。"""
        return parse_version(app_version) >= parse_version(self.min_app_version)

    def unknown_capabilities(self) -> tuple[str, ...]:
        """返回宿主不认识的能力名,供 host 记 warning。"""
        return tuple(c for c in self.capabilities if c not in KNOWN_CAPABILITIES)

    @classmethod
    def from_dict(cls, data: object) -> PluginManifest:
        """从 `plugin.toml` 解析出的 dict 构造;缺 id 抛 ValueError。

        `capabilities` 接受 list 或 tuple;其余字段做基础类型收敛,
        不让一个写错类型的字段炸掉整个发现流程。
        """
        if not isinstance(data, dict):
            raise ValueError("plugin manifest 必须是 table")
        caps = data.get("capabilities", ())
        if isinstance(caps, list):
            caps = tuple(str(c) for c in caps)
        elif not isinstance(caps, tuple):
            caps = ()
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            version=str(data.get("version", "0.1.0")),
            min_app_version=str(data.get("min_app_version", "3.0.0")),
            capabilities=caps,
            description=str(data.get("description", "")),
        )
