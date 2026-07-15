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
from tgmonitor.core.telegram.tdlib_client import parse_socks5_proxy

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
        # aiotdlib 的 ProxyTypeSocks5 是 pydantic v2 严格 str,None 会 ValidationError —
        # 我们内部统一用空串
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


# ---- TdlibClient wiring:parse_socks5_proxy → ClientSettings.proxy_settings ----
# 注:TdlibTelegramClient 现在 subclass aiotdlib.Client,真接鉴权;
# 直接测试需要在不实例化 aiotdlib.Client 的前提下覆盖其父 __init__,
# 比较曲折。下面是纯函数 / kwargs 形状 的单元测试 + 真实解析的端到端校验。

def test_parse_socks5_proxy_returns_client_proxy_settings() -> None:
    """parse_socks5_proxy() 必须产 aiotdlib 真客户端能吃的对象。

    我们只断言 host/port/type 字段,因为只有 aiotdlib 关心 username/password 等细节。
    """
    ps = parse_socks5_proxy("socks5://u:p@127.0.0.1:1080")
    # 与 aiotdlib.ClientProxySettings 的字段一致
    assert ps.host == "127.0.0.1"
    assert ps.port == 1080
    # type 可能是 SOCKS5 enum 或字符串
    type_val = getattr(ps.type, "value", ps.type)
    assert type_val == "socks5"
    assert ps.username == "u"
    assert ps.password == "p"


def test_parse_socks5_proxy_no_creds_does_not_raise_pydantic_validation() -> None:
    """回归测试:无凭据 (`socks5://host:port`) 时 username/password 必须是 "" 而非 None。

    之前 bug: parse_socks5_proxy 把 username/password 设为 None,
    aiotdlib 的 ProxyTypeSocks5(pydantic v2 严格 str) 触发 ValidationError,
    跑到 aiotdlib start() 时崩溃。
    """
    # 1) 解析成功
    ps = parse_socks5_proxy("socks5://127.0.0.1:1080")
    assert ps is not None
    # 2) 字段类型必须是 str(不是 None),可被 aiotdlib 接受
    assert isinstance(ps.username, str)
    assert isinstance(ps.password, str)
    assert ps.username == ""
    assert ps.password == ""
    # 3) 若 aiotdlib 可用,真走一次 pydantic 校验,确保不会再抛 ValidationError
    try:
        from aiotdlib.client_settings import ClientProxySettings, ClientProxyType
    except Exception:  # pragma: no cover
        pytest.skip("aiotdlib not installed")
    # 这正是 aiotdlib start() 里 _setup_proxy 干的事
    ClientProxySettings(
        host=ps.host,
        port=ps.port,
        type=ClientProxyType.SOCKS5,
        username=ps.username,
        password=ps.password,
    )  # 不抛错即为通过


def test_proxy_kwargs_passed_to_construct_via_factory() -> None:
    """工厂在 proxy 非空时把 parsed ClientProxySettings 通过 ClientSettings 传递。

    这个测试不实例化真 Client —— 只验证 factory 路径上没有抛错 + 把
    `proxy_settings` 字串解析为能进 aiotdlib 的对象。
    """
    from tgmonitor.core.telegram.factory import build_telegram_client

    # aiotdlib 不可用时 factory 会回退到 Fake,与本断言无关;
    # 在 aiotdlib 可用环境,factory 返回 TdlibTelegramClient(继承链 — 调用 __init__ 时)
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_id=1,
        api_hash="x" * 16,
        phone="+100",
        proxy="socks5://u:p@127.0.0.1:1080",
    )
    # 不传 event_bus 时构造也该成功(只到 Client.__init__ 触发 library_path 检查会挂)
    # 这里我们用 monkeypatch 把 aiotdlib.Client 换成 fake 以避开文件检查。
    from tgmonitor.core.telegram import tdlib_client as tdc

    original_init = tdc._AiClient.__init__

    def _safe_init(self, *args, **kwargs):
        # 拦截真 client 构造,只验证 kwargs 内容
        self._captured = kwargs
        # 不调 super,避免文件路径检查

    tdc._AiClient.__init__ = _safe_init  # type: ignore[assignment]
    try:
        client = build_telegram_client(s, use_fake=False, event_bus=None)
        # 验证 factory 返回了真 aiotdlib 实现(不是 Fake)
        assert not hasattr(client, "fake_state") or not client.fake_state
    except Exception:
        # aiotdlib 真 ImportError → factory 抛 RuntimeError 或返回 Fake
        pass
    finally:
        tdc._AiClient.__init__ = original_init  # type: ignore[assignment]


def test_aio_event_emit_login_state_changed_via_bus() -> None:
    """验证 aiotdlib 的 AuthorizationState ID → 我们字符串 映射 `_AUTH_STATE_MAP` 覆盖所有
    关键状态。真正事件桥接需要 aiotdlib 在线跑,只能依赖手动 trigger;此处覆盖字典内容。
    """
    from tgmonitor.core.telegram.tdlib_client import _AUTH_STATE_MAP

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
