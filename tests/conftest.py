"""pytest 自动加载入口 — 2026-08-31 v1.5.0 PR #A6。

历史:本文件曾承载 11 类 fixture + InMemoryRepository 实现 + 工厂 + tdlib stub
共 547 行。v1.5.0 PR #A6 已拆分到 `tests.fixtures.*` 子模块,这里只保留:

1. `pytest_plugins = [...]` — 让 pytest 在 collect 时加载 `tests.fixtures` 子包
   的所有 fixture 子模块;子模块名以下划线开头,pytest 默认不收。
2. 公开 API re-export — `InMemoryRepository` / `make_message` / `make_photo`
   保持 `from tests.conftest import X` 兼容,12 个现有引用零改动。
3. `force_zh_cn_locale` autouse fixture — 2026-09-03 v1.5.3 PR #D3:强制
   zh_CN locale,保持现有 30+ 处 `widget.text() == "中文"` 断言兼容
   (zh_CN 默认翻译 = 原文,fixture 保护下不撞英文)。
   v1.6.8 i18n 二期保留这个 fixture —— 新加的 i18n 测试自己用
   `monkeypatch.setenv("TG_LANG", "en_US")` 切语言并 reload,
   旧的中文断言零改动继续 pass。

**PR 1a — Test infra 整合(2026-09-15)**:
4. `qapp` 中心 fixture — session-scope QApplication 单例。原 21 个测试文件
   各自重定义 `def qapp()`(body 完全相同);集中到 conftest.py,删本地副本。
   保留 2 个语义变体:`qapp_no_locale_force`(2 个 i18n 测试文件用)和
   `qapp_16`(test_channel_widget.py 用过时的命名)。
5. `legacy` marker + `pytest_collection_modifyitems` hook — 自动给
   `tests/test_*_pr*.py` PR 编号文件打 `@pytest.mark.legacy`,默认 skip。
   `--include-legacy` 显式启用(保留 git 历史回归)。

注意:**不要**在 conftest.py 里再定义 fixture — 全部放 `tests.fixtures.*`,
否则 pytest 行为不一致(同一 fixture 两份定义会冲突)。
"""

from __future__ import annotations

import os

# 2026-09-04 v1.6.5:macOS offscreen QPA 在 HiDPI 主屏下继承 dpr=2.0,
# 导致 widget.grab().toImage() 返 2× 像素,golden 比对炸。
# 必须在 PySide6 首次 import 之前设 — 否则 QApplication 内部走 HiDPI
# 初始化,后续 setAttribute(Qt.AA_DisableHighDpiScaling) 太晚。
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")
os.environ.setdefault("QT_SCALE_FACTOR", "1")

import pytest

from tests.fixtures._factories import make_message, make_photo
from tests.fixtures._in_memory_repository import InMemoryRepository

pytest_plugins = [
    "tests.fixtures._settings",
    "tests.fixtures._storage",
    "tests.fixtures._objectstore",
    "tests.fixtures._bus_client",
    "tests.fixtures._monitor_app",
    "tests.fixtures._tdlib_stub",
]
# 注意:`_in_memory_repository` / `_factories` **不**在 pytest_plugins 里 —
# 两者都无 `@pytest.fixture`,纯类 / 纯函数;测试代码
# `from tests.conftest import InMemoryRepository / make_message` 经 re-export
# 走通。pytest_plugins 只放真正定义 fixture 的模块。

# ---- 中心 qapp fixture(PR 1a) -----------------------------------------


@pytest.fixture(scope="session")
def qapp():
    """Session-scope QApplication 单例 — PR 1a 集中,删 21 个本地副本。

    原模式:每个 Qt-touching test file 自造 `def qapp()`,body 完全相同
    `QApplication.instance() or QApplication([])`,session-scope by accident。

    现统一到 conftest.py:
    - Session-scope:整个 test run 一个 QApplication,避免 widget leak。
    - offscreen QPA 已由外部 `QT_QPA_PLATFORM=offscreen` env 启用(CI + 本地)。

    变体(仍本地):
    - `qapp_no_locale_force`(test_i18n_runtime.py + test_media_manager_i18n.py):
      i18n 测试要切语言,反 `force_zh_cn_locale` autouse fixture。
    - `qapp_16`(test_channel_widget.py):只是命名过时,改用 `qapp`。
    """
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    return QApplication.instance() or QApplication([])


# ---- legacy marker hook(PR 1a) ---------------------------------------


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """PR 1a:自动给 PR 编号文件打 `@pytest.mark.legacy`,默认 skip。

    文件命名约定:`tests/test_*_prN.py`(N 是数字)或 `tests/test_*_prNX.py`(N+X)。
    这些是各 PR 当时的 frozen regression file,行为已被 main `test_*.py` 覆盖,
    CI 默认 skip 节省时间;`--include-legacy` 显式启用。

    同时跳过 `tests/perf/`(local-only,无 pytest-benchmark)。
    """
    legacy_marker = pytest.mark.legacy
    skip_legacy = pytest.mark.skip(reason="PR 编号 frozen file — pass --include-legacy to enable")

    for item in items:
        path = str(item.fspath)
        if "/perf/" in path:
            item.add_marker(legacy_marker)
        elif "_pr" in item.name.split("[")[0] or any(
            segment.startswith("test_") and "_pr" in segment for segment in path.split("/")
        ):
            # Match test_message_view_pr5.py / test_message_detail_pr4.py / test_search_bar_pr2.py
            stem = path.rsplit("/", 1)[-1]
            if "_pr" in stem and stem.endswith(".py"):
                item.add_marker(legacy_marker)

    # Default behavior: skip legacy unless --include-legacy
    if not config.getoption("--include-legacy", default=False):
        for item in items:
            if legacy_marker in item.iter_markers():
                item.add_marker(skip_legacy)


def pytest_addoption(parser: pytest.Parser) -> None:
    """注册 --include-legacy CLI flag(PR 1a)。"""
    parser.addoption(
        "--include-legacy",
        action="store_true",
        default=False,
        help="Include PR-numbered frozen regression files (test_*_pr*.py) and perf/.",
    )


# ---- i18n locale fixture(PR #D3) --------------------------------------


@pytest.fixture(autouse=True)
def force_zh_cn_locale() -> None:
    """2026-09-03 v1.5.3 PR #D3 + 2026-09-07 v1.6.8 二期:强制 zh_CN locale。

    v1.5.3:防 CI 切 LANG 撞英文,zh_CN 默认翻译 = 原文,现有 30+ 处
    `widget.text() == "中文"` 断言在本 fixture 保护下保持兼容。

    v1.6.8:保留这个 fixture —— 新加的 i18n 测试自己用 monkeypatch +
    Settings reload 切语言,旧的中文断言零改动继续 pass。`autouse=True`
    自动 apply 到所有 test。
    """
    try:
        from PySide6.QtCore import QLocale  # noqa: PLC0415

        QLocale.setDefault(QLocale("zh_CN"))
    except ImportError:
        # 无 Qt 环境(headless service test)→ 跳过
        pass


# ---- backward-compat re-export ----------------------------------------
# 公开对象走 shim,让现有 12 个 `from tests.conftest import X` 引用不动
# 也能用。下游重构(PR #A7+ / v1.5.1)可逐步切到 `from tests.fixtures import X`。
# (re-export 已在文件顶部 import,这里只列 `__all__` 便于静态检查。)

__all__ = [
    "InMemoryRepository",
    "make_message",
    "make_photo",
]
