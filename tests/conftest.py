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

注意:**不要**在 conftest.py 里再定义 fixture — 全部放 `tests.fixtures.*`,
否则 pytest 行为不一致(同一 fixture 两份定义会冲突)。
"""

from __future__ import annotations

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

# ---- i18n locale fixture(PR #D3) --------------------------------------


@pytest.fixture(autouse=True)
def force_zh_cn_locale() -> None:
    """2026-09-03 v1.5.3 PR #D3:强制 zh_CN locale,防 CI 切 LANG 撞英文。

    zh_CN 默认翻译 = 原文,现有 30+ 处 `widget.text() == "中文"` 断言
    在本 fixture 保护下保持兼容。`autouse=True` 自动 apply 到所有 test。
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
