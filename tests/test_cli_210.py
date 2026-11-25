"""2.1.0 完备 CLI 子命令功能测试 — 全 offline,DI 注入 fakes。

覆盖 status / login / logout / channels / pause / resume / messages /
media / export / config,以及统一输出(--json)/ 退出码 / 破坏性操作确认。

架构守门(test_cli.py)与 sync/monitor Logic(test_cli_sync.py)在别处,
本文件只测 2.1.0 新增子命令的行为。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tgmonitor.core.dto import ChannelDTO, MessageDTO
from tgmonitor.core.telegram.fake_client import FakeTelegramClient


def _args(cmd: str, **overrides) -> SimpleNamespace:
    """构造命令参数命名空间。"""
    base = dict(cmd=cmd, json=False)
    base.update(overrides)
    return SimpleNamespace(**base)


def _out(capsys) -> tuple[str, str]:
    """返回 (stdout, stderr)。"""
    c = capsys.readouterr()
    return c.out, c.err


# ============================================================
# status
# ============================================================


async def test_status_ready_json(app, monitor, capsys) -> None:
    """status 输出 JSON 含全部字段,始终 rc 0。"""
    from tgmonitor.cli.status import run_status

    client: FakeTelegramClient = app.client
    client._state = "ready"
    await app.storage.upsert_channel(
        ChannelDTO(id=100, title="Ch", is_monitored=True, is_tg_subscribed=True)
    )
    rc = await run_status(_args("status", json=True, channels=False), app=app, monitor=monitor)
    assert rc == 0
    out, _ = _out(capsys)
    data = json.loads(out)
    assert data["login_state"] == "ready"
    assert data["monitor"]["monitored"] >= 1
    assert "stats" in data and "messages" in data["stats"]


async def test_status_not_ready_still_zero(app, monitor, client, capsys) -> None:
    """未登录时 status 仍输出状态、rc 0(诊断命令)。"""
    from tgmonitor.cli.status import run_status

    client._state = "phone_required"
    rc = await run_status(_args("status", json=False, channels=False), app=app, monitor=monitor)
    assert rc == 0
    out, _ = _out(capsys)
    assert "phone_required" in out


# ============================================================
# login / logout(headless QR)
# ============================================================


async def test_login_headless_qr_ready(app, monitor, client, capsys, monkeypatch) -> None:
    """登录:二维码渲染到 stderr、token 不进 stdout、rc 0。"""
    from tgmonitor.cli import auth

    # 打桩二维码渲染,避免真的画 ASCII 到 stderr(本测试只关心通道)
    rendered: list[str] = []

    def fake_render(url: str, *, show_url: bool) -> None:
        rendered.append(url)

    monkeypatch.setattr(auth, "_render_qr", fake_render)
    client._state = "phone_required"

    args = _args("login", api_id=None, api_hash=None, show_url=False)
    rc = await auth.run_login(args, app=app, monitor=monitor)
    assert rc == 0
    out, err = _out(capsys)
    assert "fake-test-token" not in out  # token 绝不进 stdout
    assert len(rendered) == 1
    assert "fake-test-token" in rendered[0]
    assert client.state == "ready"


async def test_login_writes_credentials_before_bootstrap(monkeypatch, tmp_path) -> None:
    """生产路径(app=None):--api-id/--api-hash 在 bootstrap 之前写 .env。"""
    from contextlib import asynccontextmanager

    from tgmonitor.cli import _common, auth
    from tgmonitor.core import config as config_mod

    env_seen: list[str] = []

    @asynccontextmanager
    async def fake_context(*_a, **_kw):
        # bootstrap 此刻应已读到写入的凭据
        content = (tmp_path / ".env").read_text(encoding="utf-8")
        env_seen.append(content)
        yield (None, None, None)

    monkeypatch.setattr(_common, "cli_context", fake_context)
    monkeypatch.setattr(config_mod, "_user_data_dir", lambda: tmp_path)

    args = _args("login", api_id=123, api_hash="h" * 32, show_url=False)
    try:
        await auth.run_login(args)  # app=None → 生产路径
    except Exception:  # noqa: BLE001 — fake_context 的 None ctx 会在后续崩,只关心写盘已发生
        pass
    assert len(env_seen) == 1
    assert "TG_API_ID=123" in env_seen[0]
    assert "h" * 32 in env_seen[0]


async def test_login_requires_credentials(app, monitor, client) -> None:
    """凭据缺失时抛 CliError(→ rc 1)。"""
    from tgmonitor.cli._common import CliError
    from tgmonitor.cli.auth import run_login

    app.settings.api_id = 0
    client._state = "phone_required"
    args = _args("login", api_id=None, api_hash=None, show_url=False)
    with pytest.raises(CliError, match="未配置"):
        await run_login(args, app=app, monitor=monitor)


async def test_logout_requires_yes(app, monitor) -> None:
    """logout 无 --yes → CliError。"""
    from tgmonitor.cli._common import CliError
    from tgmonitor.cli.auth import run_logout

    with pytest.raises(CliError, match="--yes"):
        await run_logout(_args("logout", yes=False), app=app, monitor=monitor)


async def test_logout_with_yes(app, monitor, client) -> None:
    """logout --yes → 清 session,rc 0。"""
    from tgmonitor.cli.auth import run_logout

    client._state = "ready"
    rc = await run_logout(_args("logout", yes=True), app=app, monitor=monitor)
    assert rc == 0
    assert client.state == "phone_required"


# ============================================================
# channels
# ============================================================


async def test_channels_list_json(app, monitor, capsys) -> None:
    """channels list --json 输出频道列表。"""
    from tgmonitor.cli.channels import run_channels

    await app.storage.upsert_channel(
        ChannelDTO(id=100, title="Ch", is_monitored=True, is_tg_subscribed=True)
    )
    args = _args("channels", channel_cmd="list", source="monitored", json=True)
    rc = await run_channels(args, app=app, monitor=monitor)
    assert rc == 0
    out, _ = _out(capsys)
    data = json.loads(out)
    assert any(c["id"] == 100 for c in data)


async def test_channels_add_requires_yes(app, monitor) -> None:
    """channels add 无 --yes → CliError。"""
    from tgmonitor.cli._common import CliError
    from tgmonitor.cli.channels import run_channels

    args = _args("channels", channel_cmd="add", channel_ids=[100], yes=False)
    with pytest.raises(CliError, match="--yes"):
        await run_channels(args, app=app, monitor=monitor)


async def test_channels_refresh_requires_yes(app, monitor, monkeypatch) -> None:
    """channels refresh 无 --yes → CliError(2.1.6 P2 #17:refresh 会级联停掉
    已在 TG 退出频道的监听,属写操作,与 add/remove 同一确认边界)。"""
    from tgmonitor.cli._common import CliError
    from tgmonitor.cli.channels import run_channels

    called: list[int] = []

    async def _spy() -> list:
        called.append(1)
        return []

    monkeypatch.setattr(app.channels, "refresh_joined_channels", _spy)
    args = _args("channels", channel_cmd="refresh", yes=False)
    with pytest.raises(CliError, match="--yes"):
        await run_channels(args, app=app, monitor=monitor)
    assert not called, "无 --yes 时不得执行 refresh_joined_channels"


async def test_channels_refresh_with_yes(app, monitor, monkeypatch, capsys) -> None:
    """channels refresh --yes → 执行 refresh_joined_channels,rc 0。"""
    from tgmonitor.cli.channels import run_channels

    called: list[int] = []

    async def _fake() -> list:
        called.append(1)
        return [ChannelDTO(id=100, title="Ch")]

    monkeypatch.setattr(app.channels, "refresh_joined_channels", _fake)
    args = _args("channels", channel_cmd="refresh", yes=True)
    rc = await run_channels(args, app=app, monitor=monitor)
    assert rc == 0
    assert called == [1]


async def test_channels_add_and_remove(app, monitor) -> None:
    """channels add/remove 走白名单,rc 0。"""
    from tgmonitor.cli.channels import run_channels

    app.client._state = "ready"
    await app.storage.upsert_channel(ChannelDTO(id=100, title="Ch", is_tg_subscribed=True))
    rc = await run_channels(
        _args("channels", channel_cmd="add", channel_ids=[100], yes=True),
        app=app,
        monitor=monitor,
    )
    assert rc == 0
    # monitor_channel 写 storage monitored 标志(白名单同步由事件订阅负责)
    ch = await app.storage.get_channel(100)
    assert ch is not None and ch.is_monitored is True

    rc = await run_channels(
        _args("channels", channel_cmd="remove", channel_ids=[100], yes=True),
        app=app,
        monitor=monitor,
    )
    assert rc == 0
    ch = await app.storage.get_channel(100)
    assert ch is not None and ch.is_monitored is False


# ============================================================
# pause / resume
# ============================================================


async def test_pause_resume(app, monitor, capsys) -> None:
    """pause/resume 切 is_paused 并输出 JSON。"""
    from tgmonitor.cli.control import run_pause, run_resume

    client: FakeTelegramClient = app.client
    client._state = "ready"
    rc = await run_pause(_args("pause", json=True), app=app, monitor=monitor)
    assert rc == 0
    assert app.is_paused is True

    rc = await run_resume(_args("resume", json=True), app=app, monitor=monitor)
    assert rc == 0
    assert app.is_paused is False


# ============================================================
# messages
# ============================================================


async def test_messages_list_json(app, monitor, capsys) -> None:
    """messages list --json 输出消息数组。"""
    from tgmonitor.cli.messages import run_messages

    await app.storage.upsert_channel(ChannelDTO(id=100, title="Ch", is_monitored=True))
    msg = MessageDTO(id=1, channel_id=100, telegram_msg_id=5, text="hello")
    await app.storage.save_message(msg)

    args = _args(
        "messages",
        message_cmd="list",
        channel=100,
        date_from=None,
        date_to=None,
        limit=200,
        search="",
        favorite=False,
        tag=None,
        pinned=False,
        include_unmonitored=True,
        json=True,
    )
    rc = await run_messages(args, app=app, monitor=monitor)
    assert rc == 0
    out, _ = _out(capsys)
    data = json.loads(out)
    assert any(m["msg_id"] == 5 for m in data)


async def test_messages_tag_note_favorite(app, monitor) -> None:
    """单条 tag/note/favorite 操作走 MessageService。"""
    from tgmonitor.cli.messages import run_messages

    await app.storage.upsert_channel(ChannelDTO(id=100, title="Ch", is_monitored=True))
    await app.storage.save_message(MessageDTO(id=1, channel_id=100, telegram_msg_id=5, text="x"))
    for cmd, extra in [
        ("favorite", {"off": False}),
        ("tag", {"tags": "a,b"}),
        ("note", {"text": "memo"}),
    ]:
        rc = await run_messages(
            _args("messages", message_cmd=cmd, channel_id=100, msg_id=5, **extra),
            app=app,
            monitor=monitor,
        )
        assert rc == 0
    msg = await app.storage.get_message(100, 5)
    assert msg is not None
    assert msg.is_favorite is True
    assert "a" in msg.tags
    assert msg.notes == "memo"


async def test_messages_delete_requires_yes(app, monitor) -> None:
    """messages delete 无 --yes → CliError。"""
    from tgmonitor.cli._common import CliError
    from tgmonitor.cli.messages import run_messages

    args = _args("messages", message_cmd="delete", channel_id=100, msg_ids=[5], yes=False)
    with pytest.raises(CliError, match="--yes"):
        await run_messages(args, app=app, monitor=monitor)


# ============================================================
# media
# ============================================================


async def test_media_list_json(app, monitor, capsys) -> None:
    """media list --json 输出 {items, total}。"""
    from tgmonitor.cli.media import run_media

    args = _args(
        "media",
        media_cmd="list",
        channel=None,
        status=None,
        type=None,
        search="",
        undownloaded=False,
        limit=1000,
        offset=0,
        sort="date",
        sort_dir="desc",
        json=True,
    )
    rc = await run_media(args, app=app, monitor=monitor)
    assert rc == 0
    out, _ = _out(capsys)
    data = json.loads(out)
    assert "items" in data and "total" in data


async def test_media_reconcile_default_dry_run(app, monitor) -> None:
    """media reconcile 默认 dry-run,不删。"""
    from tgmonitor.cli.media import run_media

    rc = await run_media(
        _args("media", media_cmd="reconcile", apply=False, yes=False),
        app=app,
        monitor=monitor,
    )
    assert rc == 0


async def test_media_reconcile_apply_requires_yes(app, monitor) -> None:
    """media reconcile --apply 无 --yes → CliError。"""
    from tgmonitor.cli._common import CliError
    from tgmonitor.cli.media import run_media

    with pytest.raises(CliError, match="--yes"):
        await run_media(
            _args("media", media_cmd="reconcile", apply=True, yes=False),
            app=app,
            monitor=monitor,
        )


# ============================================================
# export
# ============================================================


async def test_export_messages_json(app, monitor, capsys, tmp_path) -> None:
    """export messages 走 async for + ExportDone,输出 JSON 摘要。"""
    from tgmonitor.cli.export import run_export

    out = tmp_path / "out.json"
    args = _args(
        "export",
        export_cmd="messages",
        format="json",
        out=str(out),
        channel_ids=None,
        date_from=None,
        date_to=None,
        single_id=None,
        no_metadata=False,
        include_thumbnails=False,
    )
    rc = await run_export(args, app=app, monitor=monitor)
    assert rc == 0
    stdout, _ = _out(capsys)
    data = json.loads(stdout)
    assert data["out_path"] == str(out)


# ============================================================
# config(临时 .env,绝不碰真实配置)
# ============================================================


async def test_config_get_masked(monkeypatch, capsys) -> None:
    """config get api_hash 打码。"""

    from tgmonitor.core.config import Settings

    fake = Settings(_env_file=None, api_id=1, api_hash="x" * 32)  # type: ignore[call-arg]
    monkeypatch.setattr(Settings, "load_tolerant", staticmethod(lambda: (fake, [])))

    from tgmonitor.cli.config import run_config

    args = _args("config", config_cmd="get", key="api_hash", json=True)
    rc = await run_config(args)
    assert rc == 0
    out, _ = _out(capsys)
    data = json.loads(out)
    assert data["value"] == "****"


async def test_config_set_requires_yes(app, monitor) -> None:
    """config set 无 --yes → CliError。"""
    from tgmonitor.cli._common import CliError
    from tgmonitor.cli.config import run_config

    args = _args("config", config_cmd="set", key="sync_chat_delay_ms", value="500", yes=False)
    with pytest.raises(CliError, match="--yes"):
        await run_config(args, app=app, monitor=monitor)


async def test_config_set_unknown_key_cli_error(app, monitor) -> None:
    """config set 未知键 → CliError(2.1.6 P2 #16:旧实现裸 ValueError
    traceback 直出;走 CliError 后 main() 统一 stderr + 退出码 1)。"""
    from tgmonitor.cli._common import CliError
    from tgmonitor.cli.config import run_config

    args = _args("config", config_cmd="set", key="bogus_key", value="1", yes=True)
    with pytest.raises(CliError, match="未知配置键"):
        await run_config(args, app=app, monitor=monitor)


async def test_config_set_persists(app, monitor, monkeypatch, tmp_path, capsys) -> None:
    """config set 校验→reconfigure→写盘,用临时 .env。"""
    from tgmonitor.cli import config as cfg_mod

    env_path = tmp_path / ".env"
    env_path.write_text("TG_API_ID=1\n", encoding="utf-8")
    reconfigured: list = []

    async def fake_reconfigure(new):
        reconfigured.append(new)

    async def fake_validate(new):
        return None

    monkeypatch.setattr(app, "reconfigure", fake_reconfigure)
    monkeypatch.setattr(app, "validate_backends", fake_validate)
    monkeypatch.setattr(app, "env_path", env_path)
    monkeypatch.setattr(cfg_mod, "_user_data_dir", lambda: tmp_path)

    args = _args(
        "config",
        config_cmd="set",
        key="sync_chat_delay_ms",
        value="750",
        yes=True,
        no_validate=False,
        json=True,
    )
    rc = await cfg_mod.run_config(args, app=app, monitor=monitor)
    assert rc == 0
    assert len(reconfigured) == 1
    assert reconfigured[0].sync_chat_delay_ms == 750
    assert "TG_SYNC_CHAT_DELAY_MS=750" in env_path.read_text(encoding="utf-8")


# ---- P2-14(2026-11-25):messages list --tag 过滤下推 ----


async def test_messages_list_tag_filter_pushdown(app, monitor, capsys) -> None:
    """P2-14 回归:--tag 过滤必须在 limit 截断之前生效(SQL 下推)。

    种 3 条消息:最新未打标;次新带 tag="other";最旧带 tag="keep"。
    limit=1 + --tag keep 时必须返回最旧的 keep 消息 — 旧实现(tag_only
    截断后再按 tag 值内存过滤)在 tag_only 集合 {other, keep} 里截到
    other,keep 被挤掉,输出空。
    """
    from datetime import UTC, datetime

    from tgmonitor.cli.messages import run_messages

    await app.storage.upsert_channel(ChannelDTO(id=100, title="Ch", is_monitored=True))
    await app.storage.save_message(
        MessageDTO(
            id=1,
            channel_id=100,
            telegram_msg_id=10,
            text="oldest keep",
            date=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    await app.storage.save_message(
        MessageDTO(
            id=2,
            channel_id=100,
            telegram_msg_id=9,
            text="middle other",
            date=datetime(2026, 1, 2, tzinfo=UTC),
        )
    )
    await app.storage.save_message(
        MessageDTO(
            id=3,
            channel_id=100,
            telegram_msg_id=11,
            text="newest untagged",
            date=datetime(2026, 1, 3, tzinfo=UTC),
        )
    )
    await app.storage.set_tags(100, 10, ["keep"])
    await app.storage.set_tags(100, 9, ["other"])

    args = _args(
        "messages",
        message_cmd="list",
        channel=100,
        date_from=None,
        date_to=None,
        limit=1,
        search="",
        favorite=False,
        tag="keep",
        pinned=False,
        include_unmonitored=False,
        json=True,
    )
    rc = await run_messages(args, app=app, monitor=monitor)
    assert rc == 0
    out, _ = _out(capsys)
    data = json.loads(out)
    assert [m["msg_id"] for m in data] == [10], (
        f"--tag 命中的旧消息被 limit 截断漏掉: {[m['msg_id'] for m in data]}"
    )
