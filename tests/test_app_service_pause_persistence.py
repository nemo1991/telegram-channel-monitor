"""AppService pause/resume .env 持久化单测 — 2026-09-04 v1.6.6。

覆盖:
- Settings.paused 字段读 .env(TG_PAUSED=true|false)
- AppService._is_paused 从 settings.paused 初始化
- AppService.bootstrap() 防御性 guard(paused → 不连 TDLib)
- pause_monitor() 写 TG_PAUSED=true 到 .env
- resume_monitor() 写 TG_PAUSED=false 到 .env(resume 失败时不写)
- update_env_paused() 单 key 写不覆盖 .env 其它字段
"""

from __future__ import annotations

# ruff: noqa: ASYNC240
# 测试 setup / teardown 阶段写 / 读 .env(同步 pathlib),仅 await 期间的
# pause/resume_monitor() 是真异步;ASYNC240 在本文件不适用。
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tgmonitor.core.app_service import AppService
from tgmonitor.core.config import Settings
from tgmonitor.core.events import EventBus
from tgmonitor.core.settings_store import update_env_paused
from tgmonitor.core.telegram.client import TelegramClient


def _make_app(
    *,
    settings: Settings,
    env_path: Path | None = None,
    client: MagicMock | None = None,
    monitor: MagicMock | None = None,
) -> AppService:
    """构造 AppService stub — 2026-09-04 v1.6.6 接收 env_path,settings 用真 Settings。"""
    bus = EventBus()
    storage = MagicMock()
    objects = MagicMock()
    if client is None:
        client = MagicMock(spec=TelegramClient)
        client.stop = AsyncMock()
        client.start = AsyncMock()
    if monitor is None:
        monitor = MagicMock()
        monitor.stop = AsyncMock()
        monitor.start = AsyncMock()
    return AppService(
        bus=bus,
        client=client,  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
        objects=objects,  # type: ignore[arg-type]
        settings=settings,
        monitor=monitor,  # type: ignore[arg-type]
        env_path=env_path,
    )


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    """独立 env_path — 避免与同 session 共享 fixture 串污染。"""
    return tmp_path / ".env"


# ---- Settings.paused 字段 ----


def test_settings_paused_default_false() -> None:
    """2026-09-04 v1.6.6:Settings.paused 默认 False — 旧 .env 无 TG_PAUSED → 启动恢复监听。"""
    s = Settings(api_id=1, api_hash="x" * 32)
    assert s.paused is False


def test_settings_paused_reads_from_env_file(env_path: Path) -> None:
    """pydantic-settings 自动从 TG_PAUSED=true 解析到 settings.paused。"""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("TG_API_ID=1\nTG_PAUSED=true\n", encoding="utf-8")
    s = Settings(_env_file=str(env_path))  # type: ignore[call-arg]
    assert s.paused is True


def test_settings_paused_reads_false_from_env_file(env_path: Path) -> None:
    """TG_PAUSED=false 也被 pydantic-settings 正确解析。"""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("TG_PAUSED=false\n", encoding="utf-8")
    s = Settings(_env_file=str(env_path))  # type: ignore[call-arg]
    assert s.paused is False


# ---- AppService init 从 settings.paused 读 ----


def test_paused_init_from_settings_true() -> None:
    """Settings(paused=True) → AppService.is_paused = True。"""
    s = Settings(api_id=1, api_hash="x" * 32, paused=True)
    app = _make_app(settings=s)
    assert app.is_paused is True


def test_paused_init_from_settings_false() -> None:
    """Settings(paused=False) → AppService.is_paused = False。"""
    s = Settings(api_id=1, api_hash="x" * 32, paused=False)
    app = _make_app(settings=s)
    assert app.is_paused is False


# ---- AppService.bootstrap() 防御性 guard ----


async def test_bootstrap_returns_ready_when_paused() -> None:
    """2026-09-04 v1.6.6:bootstrap() 在 _is_paused=True 时直接返 ('ready', None),
    不调 client.start()。正常路径由 app.py gate skip,这是 belt-and-suspenders。
    """
    s = Settings(api_id=1, api_hash="x" * 32, paused=True)
    client = MagicMock(spec=TelegramClient)
    client.start = AsyncMock()
    app = _make_app(settings=s, client=client)

    state, detail = await app.bootstrap()

    assert (state, detail) == ("ready", None)
    client.start.assert_not_awaited()


async def test_bootstrap_normal_path_unaffected_when_resumed() -> None:
    """回归测试:_is_paused=False 时 bootstrap() 行为不变,正常调 client.start()。"""
    s = Settings(api_id=1, api_hash="x" * 32, paused=False)
    client = MagicMock(spec=TelegramClient)
    client.start = AsyncMock(return_value=("ready", None))
    app = _make_app(settings=s, client=client)

    state, detail = await app.bootstrap()

    assert (state, detail) == ("ready", None)
    client.start.assert_awaited_once()


# ---- pause_monitor() / resume_monitor() 写 .env ----


async def test_pause_monitor_writes_paused_true_to_env(env_path: Path) -> None:
    """pause_monitor() 后 .env 含 TG_PAUSED=true。"""
    s = Settings(api_id=1, api_hash="x" * 32, paused=False)
    app = _make_app(settings=s, env_path=env_path)
    env_path.parent.mkdir(parents=True, exist_ok=True)

    await app.pause_monitor()

    content = env_path.read_text(encoding="utf-8")
    assert "TG_PAUSED=true" in content


async def test_resume_monitor_writes_paused_false_to_env(env_path: Path) -> None:
    """precondition: _is_paused=True。resume_monitor() 后 .env 含 TG_PAUSED=false。"""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("TG_PAUSED=true\n", encoding="utf-8")
    s = Settings(api_id=1, api_hash="x" * 32, paused=True)
    app = _make_app(settings=s, env_path=env_path)

    await app.resume_monitor()

    content = env_path.read_text(encoding="utf-8")
    assert "TG_PAUSED=false" in content


async def test_resume_monitor_no_env_change_on_start_failure(env_path: Path) -> None:
    """2026-09-04 v1.6.6:resume_monitor() client.start 抛 → 不写 .env,保留 paused 状态
    让用户重试(.env 写 resumed 但 client 没连的不一致更糟)。
    """
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("TG_PAUSED=true\n", encoding="utf-8")
    s = Settings(api_id=1, api_hash="x" * 32, paused=True)
    client = MagicMock(spec=TelegramClient)
    client.start = AsyncMock(side_effect=RuntimeError("tdlib boom"))
    app = _make_app(settings=s, env_path=env_path, client=client)

    await app.resume_monitor()

    content = env_path.read_text(encoding="utf-8")
    # 未改 — 仍是 true,客户端启动失败应保持 paused
    assert "TG_PAUSED=true" in content
    assert "TG_PAUSED=false" not in content
    # in-memory 仍是 True(resume 失败没 flip)
    assert app.is_paused is True


async def test_pause_monitor_no_env_change_when_already_paused(env_path: Path) -> None:
    """幂等:已 paused 时 pause_monitor() no-op,不写 .env(避免覆盖其他切换)。"""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("TG_PAUSED=false\n", encoding="utf-8")
    s = Settings(api_id=1, api_hash="x" * 32, paused=True)
    app = _make_app(settings=s, env_path=env_path)

    await app.pause_monitor()  # already paused, no-op

    # .env 仍是 false(in-memory 已 True,no-op 不动 .env)
    content = env_path.read_text(encoding="utf-8")
    assert "TG_PAUSED=false" in content


async def test_pause_monitor_skips_env_write_when_env_path_none() -> None:
    """env_path=None 时(纯测试 AppService 场景)pause/resume 正常 in-memory toggle,
    无 env_path 不写 .env,不抛错。
    """
    s = Settings(api_id=1, api_hash="x" * 32, paused=False)
    app = _make_app(settings=s, env_path=None)

    await app.pause_monitor()
    assert app.is_paused is True

    await app.resume_monitor()
    assert app.is_paused is False


# ---- update_env_paused() 单 key 写不破坏 .env ----


def test_update_env_paused_preserves_other_keys(tmp_path: Path) -> None:
    """update_env_paused 单 key 写不覆盖 .env 其它 key(与 SettingsPage 协作关键)。"""
    env_path = tmp_path / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        "TG_API_ID=12345\nTG_THEME=dark\n# 注释行\n\nTG_PAUSED=false\n",
        encoding="utf-8",
    )

    update_env_paused(env_path, True)

    content = env_path.read_text(encoding="utf-8")
    assert "TG_API_ID=12345" in content
    assert "TG_THEME=dark" in content
    assert "# 注释行" in content  # 注释保留
    assert "TG_PAUSED=true" in content


def test_update_env_paused_appends_when_no_paused_key(tmp_path: Path) -> None:
    """.env 没 TG_PAUSED key 时 update_env_paused 追加到末尾。"""
    env_path = tmp_path / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("TG_API_ID=1\n", encoding="utf-8")

    update_env_paused(env_path, True)

    content = env_path.read_text(encoding="utf-8")
    assert "TG_API_ID=1" in content
    assert "TG_PAUSED=true" in content


def test_update_env_paused_round_trip(tmp_path: Path) -> None:
    """写完 .env 再用 Settings(_env_file=...)读 — paused 字段正确。"""
    env_path = tmp_path / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("TG_API_ID=1\n", encoding="utf-8")

    update_env_paused(env_path, True)
    s = Settings(_env_file=str(env_path))  # type: ignore[call-arg]
    assert s.paused is True

    update_env_paused(env_path, False)
    s2 = Settings(_env_file=str(env_path))  # type: ignore[call-arg]
    assert s2.paused is False
