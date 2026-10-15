"""Qt callback 注入桥 — 2026-10-04 plan 阶段 2.2 落地 + 2026-10-15 plan-batch6
抽到独立子 module。

`MediaService.open_media_with_result` 与 `_stage_s3_to_tmp` 原本 lazy
import Qt:
  - `QDesktopServices.openUrl(QUrl.fromLocalFile(path))`
  - `QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation)`

把这两个 Qt 调用抽到本 module 的 module-level callback。UI 层在 bootstrap
时通过 `register_qt_callbacks(...)` 注入真实现(`tgmonitor/ui/_qt_callbacks.py`
内做 Qt 类型转换);CLI 路径不调 open_media,不注册;
测试 fixture 在 `conftest.py` 注入 fake callback。

设计要点:
- 默认值 None,callbacks 未注册时 `open_media_with_result` 立即返失败
  而**不**抛异常 — 让 `app.objects=None` / 未配 callback 的 edge case
  与现有 None 行为一致(失败而不是崩)
- 显式 setter 而非 `__init__` 参数注入 — 保持 MediaService 签名不变,
  bootstrap / reconfigure / CLI 路径都不用改
- 不暴露 fallback Qt 实现 — 模块级 None,纯注入式 contract
- 2026-10-15 plan-batch6:state + 4 个 setter/getter 集中到本 module,
  `media_service.py` 主类内部用 `from . import _qt_bridge` 然后
  `_qt_bridge._module_open_url_cb` 访问;**不**用
  `from ._qt_bridge import _module_open_url_cb` — 那会绑死到本 module
  导出时的 obj,monkeypatch 改 `tgmonitor.core.media_service._module_open_url_cb`
  时失效。`monkeypatch.setattr` 字符串路径改指向本 module:
  `"tgmonitor.core.media_service._qt_bridge._module_open_url_cb"`
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

_module_open_url_cb: Callable[[str], bool] | None = None
_module_tmp_root_cb: Callable[[], Path] | None = None


def register_qt_callbacks(
    *,
    open_url: Callable[[str], bool] | None = None,
    tmp_root: Callable[[], Path] | None = None,
) -> None:
    """UI bootstrap 调用此函数注入 Qt callback。

    任一参数为 None 表示不动现有值(允许单独更新一个)。
    测试 fixture / monkeypatch 也走这个 setter — 保证 core 不 import Qt。
    """
    global _module_open_url_cb, _module_tmp_root_cb
    if open_url is not None:
        _module_open_url_cb = open_url
    if tmp_root is not None:
        _module_tmp_root_cb = tmp_root


def _reset_qt_callbacks_for_testing() -> None:
    """仅供 test fixture(conftest.py)还原 module-level callback。

    实际"清空"语义 — 让两个 callback 回到 None,保证 test 间隔离。
    不在公开 API 范围(test-only helper)。
    """
    global _module_open_url_cb, _module_tmp_root_cb
    _module_open_url_cb = None
    _module_tmp_root_cb = None


__all__ = [
    "register_qt_callbacks",
    "_reset_qt_callbacks_for_testing",
    "_module_open_url_cb",
    "_module_tmp_root_cb",
]
