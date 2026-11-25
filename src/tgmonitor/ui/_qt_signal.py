"""Qt signal 安全断连 helper — 2026-11-25 P1-N6。

多处对话框 / controller 在 closeEvent + done() 双路径上重复同一段
「disconnect 已断开的 signal 不抛、不打 warning」样板:

    try:
        sig.disconnect(slot)
    except (RuntimeError, TypeError):
        pass

坑:PySide6 第二次 disconnect 走 shiboken 路径**不 raise**,而是打
`RuntimeWarning`(stderr 噪声源,见 tests/test_batch_progress_dialog.py
的 `test_disconnect_signals_no_runtime_warning`)。所以正确实现必须同时
`catch_warnings(ignore RuntimeWarning)` + 捕获 (RuntimeError, TypeError)。

本 helper 收口这一语义,三处调用点一律改走它;行为完全不变。
"""

from __future__ import annotations

import warnings
from typing import Any


def disconnect_quietly(signal: Any, slot: Any = None) -> None:
    """断开 signal 与 slot 的连接;重复断连 / 已 GC 一律静默。

    - `slot=None` → 断开该 signal 的全部连接(等价于 `sig.disconnect()`)
    - 传具体 slot → 只断该连接
    - `RuntimeError`(已断开)/ `TypeError`(slot 已 GC)/ PySide6 二次断连的
      `RuntimeWarning` 全部吞掉 — 关窗幂等路径不该污染 stderr 或抛异常。
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        try:
            if slot is None:
                signal.disconnect()
            else:
                signal.disconnect(slot)
        except (RuntimeError, TypeError):
            pass
