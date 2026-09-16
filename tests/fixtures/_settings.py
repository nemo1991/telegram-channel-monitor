"""`settings` fixture — 2026-08-31 v1.5.0 PR #A6。

原 tests/conftest.py::settings(行 397-409),隔离到独立子模块。

**PR 1a**: 加 `make_test_settings(**overrides)` 工厂函数,消除 43 处
`Settings(...)  # type: ignore[call-arg]` 噪音(因 pydantic 的 `_env_file=None`
不在类型 stub 里)。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tgmonitor.core.config import MediaPolicy, Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    """构造一份测试用 Settings — tmp_path 注入,无 .env 依赖。"""
    return make_test_settings(
        session_dir=tmp_path / "session",
        objectstore_root=tmp_path / "media",
        data_root=tmp_path,
    )


def make_test_settings(**overrides) -> Settings:
    """PR 1a:测试用 Settings 工厂 — 消 43 处 `# type: ignore[call-arg]`。

    默认值:
    - api_id=1, api_hash="x"*32, phone="+10000000000"
    - session_dir / objectstore_root / data_root 用 `tempfile.mkdtemp` 分配
      (caller 不需要 pytest `tmp_path` 注入)
    - media_policy=METADATA

    `overrides` 优先级最高,直接传 `_env_file=None` 也 OK。

    与 `settings` fixture 的区别:这个是工厂函数(非 fixture),test body 内调用
    即可,不需要依赖 `tmp_path` fixture。`settings` fixture 现在是此工厂的 thin wrapper。
    """
    base = Path(tempfile.mkdtemp(prefix="tgmon-test-"))
    defaults = dict(
        _env_file=None,  # noqa: SLF001 — pydantic private kwarg,跳过 .env
        api_id=1,
        api_hash="x" * 32,
        phone="+10000000000",
        session_dir=base / "session",
        objectstore_root=base / "media",
        data_root=base,
        media_policy=MediaPolicy.METADATA,
    )
    defaults.update(overrides)
    s = Settings(**defaults)
    s.ensure_dirs()
    return s
