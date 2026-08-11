from __future__ import annotations

# tdlib_json — aiotdlib 归档后的轻量替代:直接绑定 libtdjson 的 ctypes 封装。
from .client import TdlibJsonClient
from .errors import TdlibError
from .objects import TDLibObject
from .proxy import Socks5Proxy
from .tdjson import TDJsonClient

__version__ = "0.1.0"

__all__ = [
    "TdlibJsonClient",
    "TdlibError",
    "TDLibObject",
    "Socks5Proxy",
    "TDJsonClient",
    "__version__",
]
