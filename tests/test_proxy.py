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
    update_env_with_settings,
)
from tgmonitor.core.telegram.proxy import parse_socks5_proxy, probe_proxy

# ---- parse_socks5_proxy 边界 ----


class TestParseSocks5Proxy:
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
    def test_socks5_ok(self) -> None:
        assert _validate_proxy_url("socks5://u:p@1.1.1.1:1080") is None
        assert _validate_proxy_url("SOCKS5://h:1") is None


# ---- Settings + store 往返 ----


class TestSettingsProxyRoundTrip:
    def test_settings_accepts_proxy(self) -> None:
        s = Settings.for_test(proxy="socks5://u:p@1.2.3.4:1080")
        assert s.proxy == "socks5://u:p@1.2.3.4:1080"

    def test_editable_round_trip(self) -> None:
        s = Settings.for_test(proxy="socks5://abc@host:9050")
        e = EditableSettings.from_settings(s)
        assert e.proxy == "socks5://abc@host:9050"

    def test_full_env_round_trip(self, tmp_path: Path) -> None:
        """写到 .env 再 parse 出来,proxy 必须还原。"""
        env_path = tmp_path / ".env"
        env_path.write_text("# placeholder\nTG_DATA_ROOT=./data\n", encoding="utf-8")
        s = Settings.for_test(
            api_id=12345,
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
    s = Settings.for_test(proxy="socks5://u:p@127.0.0.1:1080", session_dir=tmp_path / "session")
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


# ---- probe_proxy:真 SOCKS5 握手(本地 fake server,离线) ----


async def _start_fake_socks5(handler) -> tuple[asyncio.AbstractServer, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def test_probe_proxy_handshake_ok() -> None:
    """fake SOCKS5 server:greeting 选 no-auth + CONNECT 成功 → ok=True。"""

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        greeting = await reader.readexactly(4)
        assert greeting == bytes([0x05, 0x02, 0x00, 0x02])  # P1-9:声明 no-auth + user/pass
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


async def test_probe_proxy_connect_refused() -> None:
    """greeting 正常但 CONNECT 被拒(rep=0x01)→ ok=False。"""

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readexactly(4)
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


# ---- P1-9:带认证的 SOCKS5(RFC 1929 username/password 子协商) ----

_USER = "alice"
_PASS = "s3cr3t"


async def _start_auth_socks5() -> tuple[asyncio.AbstractServer, int]:
    """要求认证的 fake SOCKS5 server:选 0x02 method + 校验 RFC 1929 凭据。

    旧 probe 只声明 no-auth,对这种 server 一律握手失败(P1-9)。
    """

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        greeting = await reader.readexactly(4)
        assert greeting == bytes([0x05, 0x02, 0x00, 0x02])
        writer.write(bytes([0x05, 0x02]))  # 选 username/password
        await writer.drain()
        # RFC 1929 子协商:ver(1) ulen(1) uname plen(1) passwd
        ver = (await reader.readexactly(1))[0]
        assert ver == 0x01
        ulen = (await reader.readexactly(1))[0]
        uname = await reader.readexactly(ulen)
        plen = (await reader.readexactly(1))[0]
        passwd = await reader.readexactly(plen)
        if uname.decode() != _USER or passwd.decode() != _PASS:
            writer.write(bytes([0x01, 0x01]))  # status≠0 = 认证失败
            await writer.drain()
            writer.close()
            return
        writer.write(bytes([0x01, 0x00]))
        await writer.drain()
        # 认证过了 → CONNECT 阶段
        header = await reader.readexactly(4)
        assert header == bytes([0x05, 0x01, 0x00, 0x01])
        addr_len = (await reader.readexactly(1))[0]
        await reader.readexactly(addr_len + 2)
        writer.write(bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def test_probe_proxy_auth_with_credentials_ok() -> None:
    """带正确凭据的 probe 对认证代理 → 完成 RFC 1929 + CONNECT,ok=True。

    旧代码 greeting 只声明 no-auth,被本 fake server 回的 0x05 0x02
    一票否决 — 本测试在旧代码上必失败。
    """
    server, port = await _start_auth_socks5()
    try:
        ok, msg = await probe_proxy(f"socks5://{_USER}:{_PASS}@127.0.0.1:{port}", timeout=2.0)
        assert ok is True
        assert "OK" in msg
    finally:
        server.close()
        await server.wait_closed()


async def test_probe_proxy_auth_without_credentials_fails() -> None:
    """服务器要求认证但 URL 无凭据 → ok=False,错误信息清晰。"""
    server, port = await _start_auth_socks5()
    try:
        ok, msg = await probe_proxy(f"socks5://127.0.0.1:{port}", timeout=2.0)
        assert ok is False
        assert "认证" in msg
    finally:
        server.close()
        await server.wait_closed()


async def test_probe_proxy_auth_wrong_credentials_fails() -> None:
    """凭据错误 → RFC 1929 status≠0 → ok=False「认证失败」。"""
    server, port = await _start_auth_socks5()
    try:
        ok, msg = await probe_proxy(f"socks5://{_USER}:wrongpass@127.0.0.1:{port}", timeout=2.0)
        assert ok is False
        assert "认证失败" in msg
    finally:
        server.close()
        await server.wait_closed()
