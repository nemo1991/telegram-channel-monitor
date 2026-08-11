from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Socks5Proxy:
    """SOCKS5 代理配置(`TG_PROXY` 解析结果)。

    仅支持 SOCKS5:tgmonitor 的 `parse_socks5_proxy` 只产出它,
    TDLib 侧映射到 `proxyTypeSocks5`。
    """

    host: str
    port: int
    username: str = ""
    password: str = ""
