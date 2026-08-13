# mypy: disable-error-code="misc,assignment"
"""TDLib 启动期 IO + 错误归一 — proxy 解析 / SOCKS5 握手 / 加密 key / boot 错误。

模块拆分(2026-08-02):从 `tdlib_client.py` 抽出,定位是"启动 / 鉴权握手"
相关的 pure helpers(无 class state,无 `TdlibJsonClient` 子类化)。

包含:
- `parse_socks5_proxy` (公开) — URL → `Socks5Proxy`
- `_load_or_create_encryption_key` — session 加密 key 持久化(rotate 支持)
- `_probe_proxy` — 真做 SOCKS5 握手(不只是 TCP 通)
- `_translate_boot_error` — start() 超时时把 seen codes 翻人话
- `_AUTH_STATE_MAP` — TDLib `authorizationState*` 的 `@type` → 字符串

不依赖 `TdlibTelegramClient` 类,可独立测试;`factory.py` 也复用 `parse_socks5_proxy`。
"""
from __future__ import annotations

import asyncio
import base64
import collections
import logging
import os
import secrets

from tdlib_json import Socks5Proxy

log = logging.getLogger(__name__)

# ---- TDLib authorizationState* 的 @type → 我们的字符串 ----
# 注:与 TDLib 的 `authorizationState*` 常量一一对应;用裸字符串
# 更稳,直接匹配 TDLib 返回的 `@type` 字段。

_AUTH_STATE_MAP: dict[str, str] = {
    "authorizationStateWaitTdlibParameters": "tdlib_parameters",
    "authorizationStateWaitPhoneNumber": "phone_required",
    "authorizationStateWaitCode": "code_required",
    "authorizationStateWaitEmailAddress": "email_required",
    "authorizationStateWaitEmailCode": "email_code_required",
    "authorizationStateWaitRegistration": "registration_required",
    "authorizationStateWaitPassword": "password_required",
    "authorizationStateReady": "ready",
    "authorizationStateLoggingOut": "logging_out",
    "authorizationStateClosing": "closing",
    "authorizationStateClosed": "closed",
}


def parse_socks5_proxy(url: str | None) -> Socks5Proxy | None:
    """`socks5://[user:pass@]host:port` → `tdlib_json.Socks5Proxy`;空/None → None。

    注意:`Socks5Proxy` 的 username/password 字段类型是 `str`(严格),
    不能传 `None` — 必须空串 `""`。
    """
    if not url or not url.strip():
        return None
    s = url.strip()
    if not (s.startswith("socks5://") or s.startswith("SOCKS5://")):
        raise ValueError(f"unsupported proxy scheme: {s!r}(仅支持 socks5)")
    rest = s.split("://", 1)[1]
    user: str = ""
    password: str = ""
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
    return Socks5Proxy(
        host=host,
        port=int(port_s),
        username=user,
        password=password,
    )


def _load_or_create_encryption_key(td_dir, *, rotate: bool = False) -> str:
    """session 加密 key 必须跨启动稳定,否则 TDLib 解不开上次落盘的 db。

    做法:首次启动生成 32 字节随机 key,base64 编码后存到 `tdlib/.encryption_key`;
    后续启动从文件读。

    Args:
        td_dir:   TDLib 数据目录
        rotate:   若 True,无视现有文件,删除后重新生成。用于检测到 401 等
                  "key 不匹配" 时的恢复路径。
    """
    key_file = td_dir / ".encryption_key"

    if rotate and key_file.exists():
        try:
            key_file.unlink()
            log.warning("encryption key rotated (deleted %s)", key_file)
        except OSError as e:
            log.warning("rotate failed: %s", e)

    try:
        if key_file.exists():
            key_b64 = key_file.read_text("utf-8").strip()
            raw = base64.b64decode(key_b64, validate=True)
            if len(raw) >= 32:
                return key_b64
            log.warning("encryption key file too short (%d bytes), regenerating", len(raw))
    except OSError as e:
        log.warning("read encryption key failed: %s — regenerating", e)

    td_dir.mkdir(parents=True, exist_ok=True)
    raw = secrets.token_bytes(32)
    key_b64 = base64.b64encode(raw).decode("ascii")
    try:
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key_b64.encode("ascii"))
        finally:
            os.close(fd)
        log.info("generated new TDLib encryption key: %s (32 bytes)", key_file)
    except OSError as e:
        log.error("write encryption key failed: %s — will use ephemeral key", e)
    return key_b64


def _translate_boot_error(seen_codes: collections.deque[int], last_msg: str = "") -> str:
    """把 start() 超时期间 tdlib_json 报的 error code 集合翻成人话给 UI。

    翻译规则(2026-07-22 实测 + 2026-08-13 补):
      - **401** → encryption key 不匹配;AppService 据此外层 rotate key
      - **429** → TDLib 限流,让用户稍后重试
      - **其他已见 code** → 优先看原生 msg:`Can't lock file ... already in use`
        (另一个实例占用 session)→ 直接提示;否则 DC 握手失败 + codes 列表
      - **0 个 code** → TDLib 启动超时,可能是代理不可达或 DC 不通

    设计为 module-level pure function(跟 `_extract_error_detail` 同 pattern),
    因为:
      - 不依赖 `self`,只依赖参数(在 `start()` 内调,在单元测试可直接测
        各种 deque 输入);
      - 同样的翻译未来若其他 entry point 也用得到(reconnect 等),直接 import
        复用,无需继承。
    """
    if 401 in seen_codes:
        return "local session db encryption key 不匹配 (TDLib code 401)"
    if 429 in seen_codes:
        return "TDLib 限流 (code 429),稍后重试"
    if seen_codes:
        # 优先用 TDLib 原生 msg 兜底 — "Can't lock file" 类信息量远超
        # "DC 握手失败",能直接告诉用户是另一个实例占用了 session。
        if last_msg and any(
            k in last_msg.lower() for k in ("lock", "already in use", "another program")
        ):
            return f"session 被占用,可能是另一个 tgmonitor 实例在运行({last_msg.strip()[:120]})"
        return f"DC 握手失败 (TDLib codes {list(seen_codes)})"
    return "TDLib 启动超时(可能代理不可达或 DC 不通)"


async def _probe_proxy(proxy_url: str, timeout: float = 3.0) -> tuple[bool, str]:  # noqa: ASYNC109 — `timeout` 是 SOCKS5 握手本身的超时,不是 asyncio.wait_for;命名直白可用
    """真做 SOCKS5 握手 — 不光 TCP 端口可达,还要回 greeting + 响应 CONNECT。

    返回 `(ok, message)`:ok=True 时 message="SOCKS5 proxy OK: host:port";
    ok=False 时 message 是给 UI 看的失败原因。

    之前只 open_connection,碰到端口开但服务挂掉(例如 Clash 关了 SOCKS 但
    别的进程在 listen)就会误报通。

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
            asyncio.open_connection(host, port), timeout=timeout,
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
        req = bytes([0x05, 0x01, 0x00, 0x01]) + bytes([len(target_host)]) + target_host + bytes(
            [(target_port >> 8) & 0xFF, target_port & 0xFF]
        )
        writer.write(req)
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        # reply: ver(1) rep(1) rsv(1) atyp(1) bnd.addr bnd.port — 至少 10 字节
        reply = await asyncio.wait_for(reader.readexactly(10), timeout=timeout)
        if reply[1] != 0x00:
            log.error(
                "SOCKS5 CONNECT to 1.1.1.1:443 failed: reply=%s (rep=%d — 0x00=success)",
                reply.hex(), reply[1],
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