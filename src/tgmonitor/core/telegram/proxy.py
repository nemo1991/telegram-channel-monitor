"""SOCKS5 代理解析 + 探测 — Telethon(python-socks)后端用,零 TDLib 依赖。

v2.0.0 从 `tdlib_proxy.py` 抽出的后端无关部分:
- `parse_socks5_proxy` — URL → python-socks 代理 tuple(Telethon `proxy=` 入参)
- `probe_proxy` — 真做 SOCKS5 握手(不只是 TCP 通),启动 preflight 用

`tdlib_proxy.py` 里的 `_load_or_create_encryption_key` / `_translate_boot_error`
是 TDLib 专属(session db 加密 / boot 错误码翻译),Telethon 无对应概念,不搬。
"""

from __future__ import annotations

import asyncio
import logging

from python_socks import ProxyType

log = logging.getLogger(__name__)

# Telethon `proxy=` tuple 形如 `(proxy_type, host, port, rdns, username, password)`
# (见 telethon/network/connection/connection.py `_parse_proxy`)。
ProxyTuple = tuple[ProxyType, str, int, bool, str | None, str | None]


def parse_socks5_proxy(url: str | None) -> ProxyTuple | None:
    """`socks5://[user:pass@]host:port` → python-socks 代理 tuple;空/None → None。

    username/password 缺省给 None(python-socks 接受 None = 无认证)。
    rdns=True:域名解析交给代理端(国内网络本地 DNS 污染场景必须)。
    """
    if not url or not url.strip():
        return None
    s = url.strip()
    if not (s.startswith("socks5://") or s.startswith("SOCKS5://")):
        raise ValueError(f"unsupported proxy scheme: {s!r}(仅支持 socks5)")
    rest = s.split("://", 1)[1]
    user: str | None = None
    password: str | None = None
    if "@" in rest:
        creds, hostport = rest.rsplit("@", 1)
        if ":" in creds:
            user, _, password = creds.partition(":")
        else:
            user = creds
    else:
        hostport = rest
    if ":" not in hostport:
        raise ValueError(f"proxy missing port: {s!r}")
    host, _, port_s = hostport.rpartition(":")
    if not host or not port_s.isdigit():
        raise ValueError(f"invalid proxy host:port: {s!r}")
    return (ProxyType.SOCKS5, host, int(port_s), True, user, password)


async def probe_proxy(proxy_url: str, timeout: float = 3.0) -> tuple[bool, str]:  # noqa: ASYNC109 — `timeout` 是 SOCKS5 握手本身的超时,不是 asyncio.wait_for;命名直白可用
    """真做 SOCKS5 握手 — 不光 TCP 端口可达,还要回 greeting + 响应 CONNECT。

    返回 `(ok, message)`:ok=True 时 message="SOCKS5 proxy OK: host:port";
    ok=False 时 message 是给 UI 看的失败原因。

    只 open_connection 会误报:端口开但服务挂掉(例如 Clash 关了 SOCKS 但
    别的进程在 listen)时 TCP 也通。

    流程:
      1) TCP open host:port
      2) 发 SOCKS5 greeting:[0x05, 0x01, 0x00] (版本 5, 1 种认证方式:无)
      3) 等 server 回 [0x05, 0x00] (选 no-auth)
      4) 发 CONNECT 到 1.1.1.1:443(可达目标,只验证代理本身通,不出 DC)
      5) 等 server 回 [0x05, 0x00, ...]
    """
    if not proxy_url or not proxy_url.strip():
        return False, "代理未配置"
    try:
        rest = proxy_url.strip().split("://", 1)[1]
        hostport = rest.rsplit("@", 1)[1] if "@" in rest else rest
        host, _, port_s = hostport.rpartition(":")
        port = int(port_s)
    except Exception as e:  # noqa: BLE001
        log.error("proxy URL parse failed: %s", e)
        return False, f"代理 URL 格式错: {e}"

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except (TimeoutError, OSError) as e:
        log.error("proxy TCP unreachable: %s:%d — %s", host, port, e)
        return False, f"代理 TCP 不通 {host}:{port} — {e}"

    try:
        # 1) greeting
        writer.write(bytes([0x05, 0x01, 0x00]))  # ver=5, nmethods=1, method=no-auth
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        greeting = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
        if len(greeting) < 2 or greeting[0] != 0x05 or greeting[1] != 0x00:
            log.error(
                "proxy greeting response invalid: %s — 不是 SOCKS5 服务,或要求认证",
                greeting.hex(),
            )
            return False, f"代理不是 SOCKS5 或要求认证 ({greeting.hex()})"
        # 2) CONNECT 1.1.1.1:443 (验证代理本身可达,不出 DC)
        target_host = b"1.1.1.1"
        target_port = 443
        req = (
            bytes([0x05, 0x01, 0x00, 0x01])
            + bytes([len(target_host)])
            + target_host
            + bytes([(target_port >> 8) & 0xFF, target_port & 0xFF])
        )
        writer.write(req)
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        # reply: ver(1) rep(1) rsv(1) atyp(1) bnd.addr bnd.port — 至少 10 字节
        reply = await asyncio.wait_for(reader.readexactly(10), timeout=timeout)
        if reply[1] != 0x00:
            log.error(
                "SOCKS5 CONNECT to 1.1.1.1:443 failed: reply=%s (rep=%d — 0x00=success)",
                reply.hex(),
                reply[1],
            )
            return False, f"代理拒绝 CONNECT (rep={reply[1]})"
        log.info("SOCKS5 proxy OK: %s:%d", host, port)
        return True, f"SOCKS5 proxy OK: {host}:{port}"
    except (TimeoutError, Exception) as e:  # noqa: BLE001
        log.error("SOCKS5 handshake failed: %s:%d — %s", host, port, e)
        return False, f"代理握手失败 {host}:{port} — {e}"
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
