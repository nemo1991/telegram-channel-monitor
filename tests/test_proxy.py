"""代理支持 — proxy URL 解析 + Settings/store 往返 + Telethon 工厂 wiring + 握手探测。

v2.0.0:parse_socks5_proxy 产 python-socks tuple(Telethon `proxy=` 入参),
不再是 TDLib Socks5Proxy dataclass;新增 probe_proxy 真 SOCKS5 握手测试
(本地 fake server,离线)。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from python_socks import ProxyType

from tgmonitor.core.config import ObjectStoreBackend, Settings
from tgmonitor.core.settings_store import (
    EditableSettings,
    _validate_proxy_url,
    parse_env_file,
    settings_to_pairs,
    update_env_with_settings,
)
from tgmonitor.core.telegram.proxy import parse_socks5_proxy, probe_proxy

# ---- parse_socks5_proxy 边界 ----


class TestParseSocks5Proxy:
    def test_none_returns_none(self) -> None:
        assert parse_socks5_proxy(None) is None

    def test_empty_returns_none(self) -> None:
        assert parse_socks5_proxy("") is None

    def test_strip_whitespace(self) -> None:
        assert parse_socks5_proxy("  ") is None

    def test_basic(self) -> None:
        out = parse_socks5_proxy("socks5://127.0.0.1:1080")
        assert out is not None
        assert out[0] is ProxyType.SOCKS5
        assert out[1] == "127.0.0.1"
        assert out[2] == 1080
        assert out[3] is True  # rdns:域名解析交给代理端
        # python-socks 接受 None = 无认证
        assert out[4] is None
        assert out[5] is None

    def test_with_user_pass(self) -> None:
        out = parse_socks5_proxy("socks5://alice:s3cr3t@10.0.0.1:9050")
        assert out is not None
        assert out[1] == "10.0.0.1"
        assert out[2] == 9050
        assert out[4] == "alice"
        assert out[5] == "s3cr3t"

    def test_with_user_only(self) -> None:
        out = parse_socks5_proxy("socks5://bob@1.2.3.4:1080")
        assert out is not None
        assert out[4] == "bob"
        assert out[5] is None

    def test_uppercase_scheme(self) -> None:
        out = parse_socks5_proxy("SOCKS5://h:1")
        assert out is not None
        assert out[1] == "h" and out[2] == 1

    @pytest.mark.parametrize(
        "bad",
        [
            "http://1.2.3.4:1080",  # 协议不支持
            "socks5://host",  # 缺 port
            "socks5://host:abc",  # port 非数字
            "socks5://:1080",  # host 空
        ],
    )
    def test_invalid_raises(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_socks5_proxy(bad)


# ---- _validate_proxy_url(给 SettingsDialog 用) ----


class TestValidateProxyUrl:
    def test_empty_ok(self) -> None:
        assert _validate_proxy_url("") is None
        assert _validate_proxy_url("   ") is None

    def test_socks5_ok(self) -> None:
        assert _validate_proxy_url("socks5://u:p@1.1.1.1:1080") is None
        assert _validate_proxy_url("SOCKS5://h:1") is None

    @pytest.mark.parametrize("bad", ["http://x.com", "socks5://noport", "ftp://x.com"])
    def test_reject(self, bad: str) -> None:
        err = _validate_proxy_url(bad)
        assert err is not None
        assert "TG_PROXY" in err


# ---- Settings + store 往返 ----


class TestSettingsProxyRoundTrip:
    def test_settings_accepts_proxy(self) -> None:
        s = Settings.for_test(proxy="socks5://u:p@1.2.3.4:1080")
        assert s.proxy == "socks5://u:p@1.2.3.4:1080"

    def test_settings_proxy_default_none(self) -> None:
        s = Settings.for_test()
        assert s.proxy is None

    def test_settings_to_pairs(self) -> None:
        s = Settings.for_test(proxy="socks5://1.1.1.1:1080")
        pairs = settings_to_pairs(s)
        assert pairs["TG_PROXY"] == "socks5://1.1.1.1:1080"

    def test_settings_to_pairs_empty_when_none(self) -> None:
        s = Settings.for_test()
        assert settings_to_pairs(s)["TG_PROXY"] == ""

    def test_editable_round_trip(self) -> None:
        s = Settings.for_test(proxy="socks5://abc@host:9050")
        e = EditableSettings.from_settings(s)
        assert e.proxy == "socks5://abc@host:9050"

    def test_editable_to_settings_strips_empty(self) -> None:
        e = EditableSettings(api_id=1, api_hash="x" * 16, phone="+100", proxy="  ")
        out = e.to_settings()
        assert out.proxy is None

    def test_full_env_round_trip(self, tmp_path: Path) -> None:
        """写到 .env 再 parse 出来,proxy 必须还原。"""
        env_path = tmp_path / ".env"
        env_path.write_text("# placeholder\nTG_DATA_ROOT=./data\n", encoding="utf-8")
        s = Settings.for_test(
            api_id=12345,
            phone="+10000000000",
            proxy="socks5://u:p@127.0.0.1:1080",
            objectstore_backend=ObjectStoreBackend.FOLDER,
        )
        update_env_with_settings(env_path, s)
        env = parse_env_file(env_path)
        assert env.pairs["TG_PROXY"] == "socks5://u:p@127.0.0.1:1080"
        # 注释保留
        assert any(line.startswith("#") for line in env.raw_lines)


# ---- Telethon 工厂 wiring:parse_socks5_proxy → TelegramClient(proxy=...) ----


def test_proxy_tuple_passed_to_telethon_via_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工厂构造 TelethonTelegramClient,proxy tuple 透传给 Telethon 构造。

    monkeypatch 掉 telethon_client 模块里的 TelegramClient 符号,只记录
    kwargs,不触发真 session 文件 / 网络连接。client 实例延迟到 start()
    才构造,这里直接调 `_build_client()` 触发构造路径。
    """
    from unittest.mock import MagicMock

    from tgmonitor.core.telegram import telethon_client as tc
    from tgmonitor.core.telegram.factory import build_telegram_client

    captured: dict = {}

    def _fake_ctor(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        m = MagicMock()
        m.add_event_handler = MagicMock()
        return m

    monkeypatch.setattr(tc, "TelegramClient", _fake_ctor)
    s = Settings.for_test(
        phone="+100", proxy="socks5://u:p@127.0.0.1:1080", session_dir=tmp_path / "session"
    )
    client = build_telegram_client(s, use_fake=False, event_bus=None)
    assert isinstance(client, tc.TelethonTelegramClient)
    client._build_client()
    proxy = captured["proxy"]
    assert proxy[0] is ProxyType.SOCKS5
    assert proxy[1] == "127.0.0.1"
    assert proxy[2] == 1080
    assert proxy[4] == "u"
    assert proxy[5] == "p"
    # session path / api_id / api_hash 是位置参数
    assert captured["args"][1] == 1
    assert captured["flood_sleep_threshold"] == 0


def test_factory_no_proxy_passes_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未配代理 → Telethon 构造 proxy=None(直连)。"""
    from unittest.mock import MagicMock

    from tgmonitor.core.telegram import telethon_client as tc
    from tgmonitor.core.telegram.factory import build_telegram_client

    captured: dict = {}

    def _fake_ctor(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        m = MagicMock()
        m.add_event_handler = MagicMock()
        return m

    monkeypatch.setattr(tc, "TelegramClient", _fake_ctor)
    s = Settings.for_test(phone="+10000000000", proxy=None, session_dir=tmp_path / "session")
    client = build_telegram_client(s, use_fake=False, event_bus=None)
    client._build_client()
    assert captured["proxy"] is None


# ---- probe_proxy:真 SOCKS5 握手(本地 fake server,离线) ----


async def test_probe_proxy_empty_url() -> None:
    ok, msg = await probe_proxy("")
    assert ok is False
    assert "未配置" in msg


async def test_probe_proxy_malformed_url() -> None:
    ok, msg = await probe_proxy("socks5://no-port")
    assert ok is False
    assert "格式错" in msg


async def test_probe_proxy_tcp_unreachable() -> None:
    # 127.0.0.1:1 几乎不可能有服务 listen
    ok, msg = await probe_proxy("socks5://127.0.0.1:1", timeout=1.0)
    assert ok is False
    assert "TCP 不通" in msg


async def _start_fake_socks5(handler) -> tuple[asyncio.AbstractServer, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def test_probe_proxy_handshake_ok() -> None:
    """fake SOCKS5 server:greeting 选 no-auth + CONNECT 成功 → ok=True。"""

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        greeting = await reader.readexactly(3)
        assert greeting == bytes([0x05, 0x01, 0x00])
        writer.write(bytes([0x05, 0x00]))
        await writer.drain()
        header = await reader.readexactly(4)
        assert header == bytes([0x05, 0x01, 0x00, 0x01])
        addr_len = (await reader.readexactly(1))[0]
        await reader.readexactly(addr_len + 2)  # addr + port
        writer.write(bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
        await writer.drain()
        writer.close()

    server, port = await _start_fake_socks5(_handler)
    try:
        ok, msg = await probe_proxy(f"socks5://127.0.0.1:{port}", timeout=2.0)
        assert ok is True
        assert "OK" in msg
    finally:
        server.close()
        await server.wait_closed()


async def test_probe_proxy_bad_greeting() -> None:
    """端口开但不是 SOCKS5(回错 greeting)→ ok=False,不误报。"""

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readexactly(3)
        writer.write(bytes([0x04, 0x00]))  # 错版本
        await writer.drain()
        writer.close()

    server, port = await _start_fake_socks5(_handler)
    try:
        ok, msg = await probe_proxy(f"socks5://127.0.0.1:{port}", timeout=2.0)
        assert ok is False
        assert "不是 SOCKS5" in msg
    finally:
        server.close()
        await server.wait_closed()


async def test_probe_proxy_connect_refused() -> None:
    """greeting 正常但 CONNECT 被拒(rep=0x01)→ ok=False。"""

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readexactly(3)
        writer.write(bytes([0x05, 0x00]))
        await writer.drain()
        await reader.readexactly(4)
        addr_len = (await reader.readexactly(1))[0]
        await reader.readexactly(addr_len + 2)
        writer.write(bytes([0x05, 0x01, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
        await writer.drain()
        writer.close()

    server, port = await _start_fake_socks5(_handler)
    try:
        ok, msg = await probe_proxy(f"socks5://127.0.0.1:{port}", timeout=2.0)
        assert ok is False
        assert "拒绝 CONNECT" in msg
    finally:
        server.close()
        await server.wait_closed()
