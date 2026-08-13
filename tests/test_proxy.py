"""代理支持 — proxy URL 解析 + Settings/store 往返 + TdlibClient 跳过 None。"""
from __future__ import annotations

from pathlib import Path

import pytest

from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend, Settings
from tgmonitor.core.settings_store import (
    EditableSettings,
    _validate_proxy_url,
    parse_env_file,
    settings_to_pairs,
    update_env_with_settings,
)
from tgmonitor.core.telegram.tdlib_proxy import parse_socks5_proxy

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
        assert out.host == "127.0.0.1"
        assert out.port == 1080
        # Socks5Proxy 的 username/password 是严格 str — 无凭据时内部统一用空串
        assert out.username == ""
        assert out.password == ""
        # SOCKS5 是默认值

    def test_with_user_pass(self) -> None:
        out = parse_socks5_proxy("socks5://alice:s3cr3t@10.0.0.1:9050")
        assert out is not None
        assert out.host == "10.0.0.1"
        assert out.port == 9050
        assert out.username == "alice"
        assert out.password == "s3cr3t"

    def test_with_user_only(self) -> None:
        out = parse_socks5_proxy("socks5://bob@1.2.3.4:1080")
        assert out is not None
        assert out.username == "bob"
        assert out.password == ""

    def test_uppercase_scheme(self) -> None:
        out = parse_socks5_proxy("SOCKS5://h:1")
        assert out is not None
        assert out.host == "h" and out.port == 1

    @pytest.mark.parametrize(
        "bad",
        [
            "http://1.2.3.4:1080",       # 协议不支持
            "socks5://host",             # 缺 port
            "socks5://host:abc",         # port 非数字
            "socks5://:1080",            # host 空
        ],
    )
    def test_invalid_raises(self, bad: str) -> None:
        with pytest.raises((ValueError, RuntimeError)):
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
        s = Settings(proxy="socks5://u:p@1.2.3.4:1080")  # type: ignore[call-arg]
        assert s.proxy == "socks5://u:p@1.2.3.4:1080"

    def test_settings_proxy_default_none(self) -> None:
        # 用 _env_file=None 避免被本地 .env 影响
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.proxy is None

    def test_settings_to_pairs(self) -> None:
        s = Settings(_env_file=None, proxy="socks5://1.1.1.1:1080")  # type: ignore[call-arg]
        pairs = settings_to_pairs(s)
        assert pairs["TG_PROXY"] == "socks5://1.1.1.1:1080"

    def test_settings_to_pairs_empty_when_none(self) -> None:
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings_to_pairs(s)["TG_PROXY"] == ""

    def test_editable_round_trip(self) -> None:
        s = Settings(proxy="socks5://abc@host:9050")  # type: ignore[call-arg]
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
        s = Settings(  # type: ignore[call-arg]
            api_id=12345,
            api_hash="x" * 32,
            phone="+10000000000",
            proxy="socks5://u:p@127.0.0.1:1080",
            db_backend=DBBackend.JSONL,
            objectstore_backend=ObjectStoreBackend.FOLDER,
            media_policy=MediaPolicy.METADATA,
        )
        update_env_with_settings(env_path, s)
        env = parse_env_file(env_path)
        assert env.pairs["TG_PROXY"] == "socks5://u:p@127.0.0.1:1080"
        # 注释保留
        assert any(line.startswith("#") for line in env.raw_lines)


# ---- TdlibClient wiring:parse_socks5_proxy → TdlibJsonClient(proxy=...) ----
# 注:TdlibTelegramClient 现在内部持 tdlib_json.TdlibJsonClient(自编译 libtdjson
# 的 ctypes 绑定),proxy 以 Socks5Proxy dataclass 传构造。下面是纯函数 /
# kwargs 形状 的单元测试 + 真实解析的端到端校验。

def test_parse_socks5_proxy_returns_socks5_proxy() -> None:
    """parse_socks5_proxy() 必须产 TdlibJsonClient 构造能吃的 Socks5Proxy。

    Socks5Proxy 是普通 dataclass,断言 host/port/username/password 即可。
    """
    ps = parse_socks5_proxy("socks5://u:p@127.0.0.1:1080")
    assert ps.host == "127.0.0.1"
    assert ps.port == 1080
    assert ps.username == "u"
    assert ps.password == "p"


def test_parse_socks5_proxy_no_creds_uses_empty_strings() -> None:
    """回归测试:无凭据 (`socks5://host:port`) 时 username/password 必须是 "" 而非 None。

    之前 bug: parse_socks5_proxy 把 username/password 设为 None,
    Socks5Proxy 的严格 str 字段校验失败,跑到 start() 时崩溃。
    """
    ps = parse_socks5_proxy("socks5://127.0.0.1:1080")
    assert ps is not None
    # 字段类型必须是 str(不是 None),可被 TdlibJsonClient 接受
    assert isinstance(ps.username, str)
    assert isinstance(ps.password, str)
    assert ps.username == ""
    assert ps.password == ""


def test_proxy_kwargs_passed_to_construct_via_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工厂把 parsed Socks5Proxy 通过 `proxy=` 传给 TdlibJsonClient 构造。

    用 monkeypatch 把 `tdc._AiClient.__init__` 换成探针:只记录 kwargs,
    不触发 native libtdjson 加载 / 文件路径检查。settings 必须带 session_dir
    (tmp_path),否则 `_load_or_create_encryption_key` 写真实 platformdirs。
    """
    from tgmonitor.core.telegram import tdlib_client as tdc
    from tgmonitor.core.telegram.factory import build_telegram_client

    def _safe_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # 拦截真 client 构造,只验证 kwargs 内容;不调 super,避免 native 加载。
        # add_event_handler 依赖 _updates_handlers(TdlibTelegramClient.__init__
        # 在 super() 后要注册 updateNewMessage / "*" 两个 handler)。
        self._updates_handlers = {}
        self._captured = kwargs

    monkeypatch.setattr(tdc._AiClient, "__init__", _safe_init)
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_id=1,
        api_hash="x" * 16,
        phone="+100",
        proxy="socks5://u:p@127.0.0.1:1080",
        session_dir=tmp_path / "session",
    )
    client = build_telegram_client(s, use_fake=False, event_bus=None)
    # 工厂返回真 TdlibTelegramClient(不再 fallback fake)
    assert isinstance(client, tdc.TdlibTelegramClient)
    captured = client._captured  # type: ignore[attr-defined]
    proxy = captured["proxy"]
    assert proxy.host == "127.0.0.1"
    assert proxy.port == 1080
    assert proxy.username == "u"
    assert proxy.password == "p"
    assert captured["parameters"]["api_id"] == 1
    assert captured["parameters"]["phone_number"] == "+100"


def test_aio_event_emit_login_state_changed_via_bus() -> None:
    """验证 TDLib 的 authorizationState* → 我们字符串 映射 `_AUTH_STATE_MAP` 覆盖所有
    关键状态。真正事件桥接需要真 libtdjson 在线跑,只能依赖手动 trigger;此处覆盖字典内容。
    """
    from tgmonitor.core.telegram.tdlib_proxy import _AUTH_STATE_MAP

    expected = {
        # TDLib 已知的关键状态
        "authorizationStateWaitPhoneNumber": "phone_required",
        "authorizationStateWaitCode": "code_required",
        "authorizationStateWaitPassword": "password_required",
        "authorizationStateReady": "ready",
    }
    for tdlib_id, ours in expected.items():
        assert _AUTH_STATE_MAP.get(tdlib_id) == ours, (
            f"期望 {tdlib_id} → {ours!r},实际 {_AUTH_STATE_MAP.get(tdlib_id)!r}"
        )


# ---- _setup_proxy:addProxy / disableProxy 显式请求 ----
# 回归 2026-08-13 Windows 线上 bug:send()(fire-and-forget)的 addProxy 失败
# 响应没有 request_id,被 tdlib_json 静默丢弃 → 代理不生效,只有开 TUN 才通。
# 现在改用 request() 显式等响应,失败直接抛 TdlibError,启动流程转可见错误。

@pytest.mark.asyncio
async def test_setup_proxy_sends_addproxy_when_configured(
    tmp_path: Path, bus, stub_tdlib_init,
) -> None:
    """配了 SOCKS5 代理 → 发 addProxy(enable=True),server/port/凭据逐字段正确。"""
    from tgmonitor.core.telegram import tdlib_client as tdc

    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_id=1,
        api_hash="x" * 32,
        phone="+10000000000",
        proxy="socks5://u:p@127.0.0.1:1080",
        session_dir=tmp_path / "session",
    )
    client = tdc.TdlibTelegramClient(s, event_bus=bus)
    client._running = True  # request() 会校验 running
    captured: list[dict] = []

    async def _fake_request(query: dict, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(query)
        return {"@type": "proxy", "id": 7}

    client.request = _fake_request  # type: ignore[method-assign]
    await client._setup_proxy()
    assert len(captured) == 1
    payload = captured[0]
    assert payload["@type"] == "addProxy"
    assert payload["enable"] is True
    assert payload["server"] == "127.0.0.1"
    assert payload["port"] == 1080
    proxy_type = payload["type"]
    assert proxy_type["@type"] == "proxyTypeSocks5"
    assert proxy_type["username"] == "u"
    assert proxy_type["password"] == "p"


@pytest.mark.asyncio
async def test_setup_proxy_disables_when_no_proxy(
    tmp_path: Path, bus, stub_tdlib_init,
) -> None:
    """未配代理 → 发 disableProxy,防止残留/默认代理生效。

    注:不用 conftest `settings` 直接构造 — Settings 默认读平台数据目录 `.env`,
    用户机器上可能配了 TG_PROXY。显式 `_env_file=None` + `proxy=None` 保证确定。
    """
    from tgmonitor.core.telegram import tdlib_client as tdc

    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_id=1,
        api_hash="x" * 32,
        phone="+10000000000",
        proxy=None,
        session_dir=tmp_path / "session",
    )
    client = tdc.TdlibTelegramClient(s, event_bus=bus)
    client._running = True
    captured: list[dict] = []

    async def _fake_request(query: dict, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(query)
        return {"@type": "ok"}

    client.request = _fake_request  # type: ignore[method-assign]
    await client._setup_proxy()
    assert [q["@type"] for q in captured] == ["disableProxy"]


@pytest.mark.asyncio
async def test_setup_proxy_raises_tdlib_error(
    tmp_path: Path, bus, stub_tdlib_init,
) -> None:
    """addProxy 被 TDLib 拒绝 → 必须抛 TdlibError(启动流程转可见错误)。"""
    from tgmonitor.core.telegram import tdlib_client as tdc

    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_id=1,
        api_hash="x" * 32,
        phone="+10000000000",
        proxy=None,
        session_dir=tmp_path / "session",
    )
    client = tdc.TdlibTelegramClient(s, event_bus=bus)
    client._running = True

    async def _bad_request(query: dict, **kwargs):  # type: ignore[no-untyped-def]
        raise tdc.TdlibError(code=400, message="addProxy failed")

    client.request = _bad_request  # type: ignore[method-assign]
    with pytest.raises(tdc.TdlibError):
        await client._setup_proxy()
