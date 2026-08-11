from __future__ import annotations


class TdlibError(Exception):
    """TDLib 请求返回 `error` 对象时抛出的异常。

    与 aiotdlib 的 `AioTDLibError` 对应:`code` 是 TDLib 错误码
    (如 429=限流、401=加密 key 不匹配),`message` 是错误描述。
    """

    def __init__(self, code: int = 0, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")
