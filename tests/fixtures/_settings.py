"""`settings` fixture — 2026-08-31 v1.5.0 PR #A6。

原 tests/conftest.py::settings(行 397-409),隔离到独立子模块。
"""

from __future__ import annotations

import pytest

from tgmonitor.core.config import MediaPolicy, Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    """构造一份测试用 Settings — tmp_path 注入,无 .env 依赖。"""
    s = Settings(  # type: ignore[call-arg]
        api_id=1,
        api_hash="x" * 32,
        phone="+10000000000",
        session_dir=tmp_path / "session",
        objectstore_root=tmp_path / "media",
        data_root=tmp_path,
        media_policy=MediaPolicy.METADATA,
    )
    s.ensure_dirs()
    return s
