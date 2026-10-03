"""Qt callback 实现 — 2026-10-04 plan 阶段 2.2 落地。

`core/media_service.py` 把所有 Qt 调用(`QDesktopServices.openUrl` /
`QStandardPaths.writableLocation`)抽到 module-level callback。本模块
是 callback 的真实现 — UI 层把 Qt 类型(`QUrl`)转成纯 Python 字符串给
core 用。

边界:本模块顶层 + 函数体**仅**在这里 import PySide6,不在 core /
其它 UI 模块传播。core 完全不知道 `QDesktopServices` 长什么样。

调用方:仅 `tgmonitor.app.run()` bootstrap 完成后调一次
`register()`;teardown 阶段不需还原(进程退出自动清)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from tgmonitor.core.media_service import register_qt_callbacks

log = logging.getLogger(__name__)


def _qt_open_url(path: str) -> bool:
    """`QDesktopServices.openUrl(QUrl.fromLocalFile(path))` 的 Python 包装。

    返回 `QDesktopServices.openUrl(...)` 的 bool — True = 系统成功唤起
    默认应用打开文件;False = 没关联应用或调用失败。

    2026-09-26 fix(open-media-hang):openUrl 在 macOS 上同步走
    `[NSWorkspace openURL:]` AppleEvent → LaunchServices,会卡主线程
    100ms~数秒;调用方(`open_media_with_result`)走 `asyncio.to_thread`
    把这个函数推到 worker thread,UI 不冻。
    """
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(path)))


def _qt_tmp_root() -> Path:
    """`QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation)`
    的 Python 包装。

    macOS = per-user `$TMPDIR`(典型 `/var/folders/.../T/`);Linux = `/tmp`;
    Windows = `%TEMP%`(用户级)。

    返回 `Path` 而不是 `str` — 调用方(`_stage_s3_to_tmp`)需要 path 操作
    (`mkdir / write_bytes`),直接给 Path 避免再次转换。
    """
    from PySide6.QtCore import QStandardPaths

    tmp_dir_str = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.TempLocation,
    )
    return Path(tmp_dir_str) if tmp_dir_str else Path.home() / ".cache" / "tgmonitor"


def register() -> None:
    """在 GUI bootstrap 完成后调一次,把 Qt callback 注入 core。

    调用方:`tgmonitor.app.run()` 内,`_bootstrap(...)` 后立即调。
    后续所有 `MediaService.open_media_with_result` / `_stage_s3_to_tmp`
    调用都走 Qt 实现。

    CLI 路径(`tgmonitor sync` / `tgmonitor monitor`)**不**调本函数 —
    CLI 不调 `open_media`,core 模块级 callback 保持 None,直接返失败
    DTO(等价 `objects=None` 失败路径)。
    """
    register_qt_callbacks(
        open_url=_qt_open_url,
        tmp_root=_qt_tmp_root,
    )
    log.debug("UI Qt callbacks registered (open_url + tmp_root)")


def register_with_test_fakes(
    *,
    open_url: Callable[[str], bool] | None = None,
    tmp_root: Callable[[], Path] | None = None,
) -> None:
    """测试入口:用 fake callback 覆盖真 Qt 实现。

    一般测试不需要本函数 — `tests/conftest.py` 的 autouse fixture
    每 test 重置 callback 到 None,需要 Qt 行为的 test 自己 monkeypatch
    `core.media_service._module_open_url_cb` / `_module_tmp_root_cb`。

    本函数用于「跑真 GUI 但要绕过 Qt 真调 openUrl」(如 UI 截图测试 /
    CI offscreen 不能弹外部应用)。
    """
    register_qt_callbacks(open_url=open_url, tmp_root=tmp_root)
