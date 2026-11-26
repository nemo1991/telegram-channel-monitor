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


def redact_url_credentials(url: str) -> str:
    """抹掉 URL 里的 `user:pass@`,其余(scheme / host:port / path)原样保留。

    2026-11-26 2.1.7 P1-8:代理 / DSN 这类 URL 可能带账号密码。解析失败的
    错误信息会进日志、回显到设置页、也会被贴进 issue —— 一律先过这里。
    没有 `@` 时原样返回。
    """
    scheme, sep, rest = url.partition("://")
    if "@" not in rest:
        return url
    hostpart = rest.rsplit("@", 1)[1]
    return f"{scheme}{sep}****@{hostpart}"


def parse_socks5_proxy(url: str | None) -> ProxyTuple | None:
    """`socks5://[user:pass@]host:port` → python-socks 代理 tuple;空/None → None。

    username/password 缺省给 None(python-socks 接受 None = 无认证)。
    rdns=True:域名解析交给代理端(国内网络本地 DNS 污染场景必须)。
    解析失败抛 `ValueError`,message 里的 URL 已抹掉凭据。
    """
    if not url or not url.strip():
        return None
    s = url.strip()
    shown = redact_url_credentials(s)
    if not (s.startswith("socks5://") or s.startswith("SOCKS5://")):
        raise ValueError(f"unsupported proxy scheme: {shown!r}(仅支持 socks5)")
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
        raise ValueError(f"proxy missing port: {shown!r}")
    host, _, port_s = hostport.rpartition(":")
    if not host or not port_s.isdigit():
        raise ValueError(f"invalid proxy host:port: {shown!r}")
    return (ProxyType.SOCKS5, host, int(port_s), True, user, password)


async def probe_proxy(proxy_url: str, timeout: float = 3.0) -> tuple[bool, str]:  # noqa: ASYNC109 — `timeout` 是 SOCKS5 握手本身的超时,不是 asyncio.wait_for;命名直白可用
    """真做 SOCKS5 握手 — 不光 TCP 端口可达,还要回 greeting + 响应 CONNECT。

    返回 `(ok, message)`:ok=True 时 message="SOCKS5 proxy OK: host:port";
    ok=False 时 message 是给 UI 看的失败原因。

    只 open_connection 会误报:端口开但服务挂掉(例如 Clash 关了 SOCKS 但
    别的进程在 listen)时 TCP 也通。

    流程:
      1) TCP open host:port
      2) 发 SOCKS5 greeting:[0x05, 0x02, 0x00, 0x02]
         (版本 5, 2 种认证方式:no-auth + username/password —
         2026-11-25 修 P1-9:只声明 no-auth 会被带认证的代理回
         `0x05 0x02` 一票否决,Telethon 本可完成 RFC 1929 认证)
      3) 等 server 选方式:
         - 选 no-auth([0x05, 0x00])→ 直接进 CONNECT
         - 选 username/password([0x05, 0x02])→ RFC 1929 子协商:
           发 [0x01, ulen, uname, plen, passwd](凭据取自 proxy URL),
           等 server 回 [0x01, status],status≠0 即认证失败;
           URL 无凭据时直接报清晰错误
         - 其他(含 0xFF 拒绝全部)→ 失败
      4) 发 CONNECT 到 1.1.1.1:443(可达目标,只验证代理本身通,不出 DC)
      5) 等 server 回 [0x05, 0x00, ...]
    """
    if not proxy_url or not proxy_url.strip():
        return False, "代理未配置"
    try:
        parsed = parse_socks5_proxy(proxy_url)
    except ValueError as e:
        log.error("proxy URL parse failed: %s", e)
        return False, f"代理 URL 格式错: {e}"
    assert parsed is not None  # 上面已排除空 URL
    _, host, port, _, username, password = parsed

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except (TimeoutError, OSError) as e:
        log.error("proxy TCP unreachable: %s:%d — %s", host, port, e)
        return False, f"代理 TCP 不通 {host}:{port} — {e}"

    try:
        # 1) greeting — 同时声明 no-auth 与 username/password(P1-9)
        writer.write(bytes([0x05, 0x02, 0x00, 0x02]))  # ver=5, nmethods=2
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        greeting = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
        if len(greeting) < 2 or greeting[0] != 0x05:
            log.error(
                "proxy greeting response invalid: %s — 不是 SOCKS5 服务",
                greeting.hex(),
            )
            return False, f"代理不是 SOCKS5 ({greeting.hex()})"
        method = greeting[1]
        if method == 0x02:
            # RFC 1929 username/password 子协商
            if not username or not password:
                log.error("proxy requires auth but URL has no credentials: %s:%d", host, port)
                return False, f"代理要求认证,但 URL 未提供用户名/密码 ({host}:{port})"
            uname = username.encode("utf-8")
            passwd = password.encode("utf-8")
            auth_req = bytes([0x01, len(uname)]) + uname + bytes([len(passwd)]) + passwd
            writer.write(auth_req)
            await asyncio.wait_for(writer.drain(), timeout=timeout)
            auth_reply = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
            if len(auth_reply) < 2 or auth_reply[0] != 0x01 or auth_reply[1] != 0x00:
                log.error(
                    "SOCKS5 auth failed: reply=%s (status=0x00 表示成功)",
                    auth_reply.hex(),
                )
                status = auth_reply[1] if len(auth_reply) > 1 else -1
                return False, f"代理认证失败 (status={status:#04x})"
            log.debug("SOCKS5 RFC 1929 auth OK: %s:%d", host, port)
        elif method != 0x00:
            log.error(
                "proxy chose unsupported auth method 0x%02X: %s:%d",
                method,
                host,
                port,
            )
            return False, f"代理选择了不支持的认证方式 (method=0x{method:02X})"
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
