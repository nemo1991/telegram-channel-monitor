"""插件发现 — 双通道:entry-points + 本地目录。

两条来源统一产出 `DiscoveredPlugin`(manifest + 实例化工厂 + 来源标签),
宿主 `PluginHost` 只认这个结构,不关心插件从哪来。

**entry-points**(`tgmonitor.plugins` group):
    第三方包在 pyproject 里声明
        [project.entry-points."tgmonitor.plugins"]
        myplugin = "myplugin:MyPlugin"
    入口指向 `Plugin` 的**类**(非实例),工厂 = 类本身。

**本地目录**(`<user_data_dir>/plugins/`):
    `<plugins_dir>/<id>/plugin.toml` + `<id>/plugin.py`,后者暴露
    `Plugin` 类(类名随意,取模块里实现了 manifest/activate/deactivate 的类)。
    用 `importlib.util.spec_from_file_location` 直接加载,不要求装进 site-packages
    — 这是 PyInstaller 冻结包场景下唯一可行的第三方扩展路径。

发现阶段的任何失败(坏 toml / 模块 import 错误 / 契约不符)都记日志并跳过该插件,
绝不让一个坏插件阻断整个发现流程(更不阻断应用启动)。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable, TypeGuard

from tgmonitor.plugins.api import Plugin
from tgmonitor.plugins.manifest import PluginManifest

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "tgmonitor.plugins"
LOCAL_PLUGIN_FILE = "plugin.py"
LOCAL_MANIFEST_FILE = "plugin.toml"


@dataclass(frozen=True)
class DiscoveredPlugin:
    """一个已发现的插件 — host 激活前的唯一输入。"""

    manifest: PluginManifest
    factory: Callable[[], Plugin]
    source: str  # "entry-point" | "local"


def _is_plugin_cls(obj: object) -> TypeGuard[type[Plugin]]:
    """宽松契约检查:有 manifest + activate + deactivate 即可,不强求继承。

    用 `runtime_checkable` Protocol 的 isinstance 对类对象无效(它检查实例),
    所以这里直接查属性,允许第三方不 import tgmonitor.plugins 也能写插件
    (结构化契约,降低耦合)。TypeGuard 让 mypy 在检查后把它当 Plugin 类。

    只检查装配期三件套 — 运行期 `start` / `stop` 是可选钩子,发现阶段不
    要求;宿主调用时用 getattr 容错(兼容阶段 1/2 的老插件)。
    """
    return all(hasattr(obj, attr) for attr in ("manifest", "activate", "deactivate"))


def _plugin_cls_from_module(module: ModuleType) -> Callable[[], Plugin] | None:
    """从模块里挑出第一个符合 Plugin 契约的类,作为工厂返回。"""
    for name in dir(module):
        if name.startswith("_"):
            continue
        obj = getattr(module, name)
        if isinstance(obj, type) and _is_plugin_cls(obj):
            return obj
    return None


def discover_entry_points() -> list[DiscoveredPlugin]:
    """从 `tgmonitor.plugins` entry-point group 发现插件。

    用 `importlib.metadata.entry_points(group=...)` 新 API;该项在装了包的
    环境里工作,未安装 (开发 src 布局) 时返回空列表,不报错。
    """
    from importlib.metadata import EntryPoint, entry_points

    found: list[DiscoveredPlugin] = []
    eps: list[EntryPoint]
    try:
        eps = list(entry_points(group=ENTRY_POINT_GROUP))
    except Exception:
        log.exception("entry-points 发现失败,跳过该通道")
        return found

    for ep in eps:
        try:
            obj = ep.load()
            if not (isinstance(obj, type) and _is_plugin_cls(obj)):
                log.warning("entry-point %s 未指向 Plugin 类,跳过", ep.name)
                continue
            manifest = _coerce_manifest(obj.manifest, fallback_id=ep.name)
            found.append(DiscoveredPlugin(manifest=manifest, factory=obj, source="entry-point"))
        except Exception:
            log.exception("entry-point %s 加载失败,跳过", ep.name)
    return found


def _coerce_manifest(raw: object, *, fallback_id: str) -> PluginManifest:
    """把类属性 `manifest` 收敛成 PluginManifest;dict 也接受。"""
    if isinstance(raw, PluginManifest):
        return raw
    if isinstance(raw, dict):
        data = dict(raw)
        data.setdefault("id", fallback_id)
        return PluginManifest.from_dict(data)
    raise ValueError(f"插件 {fallback_id!r} 的 manifest 类型不支持: {type(raw).__name__}")


def _load_local_module(plugin_id: str, path: Path) -> ModuleType | None:
    """用唯一模块名加载本地插件文件,避免污染 sys.modules 或与包重名。"""
    module_name = f"_tgmonitor_local_plugin_{plugin_id}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            log.warning("本地插件 %s 无法创建 import spec", plugin_id)
            return None
        module = importlib.util.module_from_spec(spec)
        # 放进 sys.modules 再 exec — 插件里 @dataclass / 相对引用需要它。
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        sys.modules.pop(module_name, None)
        log.exception("本地插件 %s 模块加载失败,跳过", plugin_id)
        return None


def discover_local(plugins_dir: Path) -> list[DiscoveredPlugin]:
    """从本地目录发现插件:`<plugins_dir>/<id>/plugin.toml` + `plugin.py`。"""
    found: list[DiscoveredPlugin] = []
    if not plugins_dir.is_dir():
        return found

    for child in sorted(plugins_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        manifest_path = child / LOCAL_MANIFEST_FILE
        module_path = child / LOCAL_PLUGIN_FILE
        if not manifest_path.exists() or not module_path.exists():
            log.debug(
                "本地插件目录 %s 缺 %s / %s,跳过",
                child.name,
                LOCAL_MANIFEST_FILE,
                LOCAL_PLUGIN_FILE,
            )
            continue
        try:
            with manifest_path.open("rb") as f:
                data = tomllib.load(f)
            # 目录名是启用清单 / 配置命名空间的 key,强制以它为准。
            data = dict(data)
            if data.get("id") != child.name:
                log.warning(
                    "本地插件 %s 的 manifest.id=%s 与目录名不符,以目录名为准",
                    child.name,
                    data.get("id"),
                )
            data["id"] = child.name
            manifest = PluginManifest.from_dict(data)
        except (OSError, ValueError):
            log.exception("本地插件 %s 的 plugin.toml 解析失败,跳过", child.name)
            continue

        module = _load_local_module(child.name, module_path)
        if module is None:
            continue
        factory = _plugin_cls_from_module(module)
        if factory is None:
            log.warning("本地插件 %s 的 plugin.py 未找到 Plugin 类,跳过", child.name)
            continue
        found.append(DiscoveredPlugin(manifest=manifest, factory=factory, source="local"))
    return found


def discover_all(plugins_dir: Path) -> list[DiscoveredPlugin]:
    """双通道合并;同 id 时本地目录优先(用户本地覆盖优先于已装包)。"""
    local = discover_local(plugins_dir)
    local_ids = {d.manifest.id for d in local}
    result = [d for d in discover_entry_points() if d.manifest.id not in local_ids]
    result.extend(local)
    return result
