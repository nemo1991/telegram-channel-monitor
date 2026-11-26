"""插件配置文件 — `enabled.json` / `config.json` 的读写。

目录约定(`<user_data_dir>/plugins/`):
    enabled.json   {"enabled": ["id1", "id2"]}  — 显式启用清单(发现 ≠ 启用)
    config.json    {"id1": {"key": value}}       — 按插件 id 隔离的配置命名空间

**不复用 `Settings` / 主 `.env`**:插件配置不进主配置真源,避免污染
`settings_to_pairs → EditableSettings → SettingsDiff` 那条已很敏感的链(见 ADR-001)。

写盘沿用 `core/settings_store.py` 的原子手法:临时文件以 0600 创建 →
写 → `replace` → 对最终路径再 chmod 0600。这样并发读不会看到半截 JSON,
且配置文件不像 .env 那样含明文凭据(仍收紧权限以防插件配置日后放 token)。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ENABLED_FILE = "enabled.json"
CONFIG_FILE = "config.json"


def _atomic_write_json(path: Path, payload: object) -> None:
    """原子写一个 JSON 文件:0600 临时文件 → replace → 最终路径 chmod。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)
    os.chmod(path, 0o600)


def _read_json(path: Path, default: object) -> object:
    """读 JSON;不存在 /  corrupt / 类型不符一律回退 default,不抛。

    配置文件是用户可编辑的,损坏不应阻断应用启动。日志留痕便于排查。
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("插件配置文件损坏,已忽略: %s", path)
        return default


def read_enabled(plugins_dir: Path) -> frozenset[str]:
    """读已启用插件 id 集合。文件缺失 = 无插件启用(默认安全)。"""
    data = _read_json(plugins_dir / ENABLED_FILE, {})
    if not isinstance(data, dict):
        return frozenset()
    ids = data.get("enabled", ())
    if not isinstance(ids, list):
        return frozenset()
    return frozenset(str(i) for i in ids)


def write_enabled(plugins_dir: Path, enabled: frozenset[str]) -> None:
    """原子写启用清单 — id 排序保证 diff 稳定。"""
    _atomic_write_json(plugins_dir / ENABLED_FILE, {"enabled": sorted(enabled)})


def read_all_config(plugins_dir: Path) -> dict[str, dict[str, Any]]:
    """读全部插件配置;结构不符时回退空 dict。"""
    data = _read_json(plugins_dir / CONFIG_FILE, {})
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            result[str(key)] = value
    return result


def write_all_config(plugins_dir: Path, config: dict[str, dict[str, Any]]) -> None:
    """原子写全部插件配置。"""
    _atomic_write_json(plugins_dir / CONFIG_FILE, config)


class PluginConfig:
    """单个插件的命名空间配置视图 — 只读写自己 id 下的子树。

    写操作立即 `replace` 整个 config.json(读-改-写);插件配置体量很小,
    没有并发多写者(同一事件循环内),这个粒度足够且实现最简单。
    """

    def __init__(self, plugins_dir: Path, plugin_id: str) -> None:
        self._dir = plugins_dir
        self._id = plugin_id

    def get(self, key: str, default: Any = None) -> Any:
        """读自己命名空间下的值;不存在返回 default。"""
        return read_all_config(self._dir).get(self._id, {}).get(key, default)

    def all(self) -> dict[str, Any]:
        """读自己命名空间的全部键值(返回副本)。"""
        return dict(read_all_config(self._dir).get(self._id, {}))

    def set(self, key: str, value: Any) -> None:
        """写一个键值 — JSON 可序列化的值。写失败上抛,由插件自己决定降级。"""
        all_config = read_all_config(self._dir)
        namespace = dict(all_config.get(self._id, {}))
        namespace[key] = value
        all_config[self._id] = namespace
        write_all_config(self._dir, all_config)
