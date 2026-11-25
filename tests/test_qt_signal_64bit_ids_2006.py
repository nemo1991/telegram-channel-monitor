"""Qt Signal 承载 64 位 Telegram channel_id 的回归契约(2026-11-19 hotfix)。

真实 chat_id 形如 -1001375475051,超过 C++ 32 位 int。PySide6 的
`Signal(int, ...)` 映射到 32 位 int:emit 时 shiboken 抛 OverflowError,
作为 unraisable 残留 error indicator;slot 里第一个 Qt 绑定调用
(`render_pixmap` 的 `QPixmap()`)随即抛 SystemError。用户日志里媒体页
缩略图批量报 `<class 'PySide6.QtGui.QPixmap'> returned a result with an
exception set` 即此路径。

本文件把所有承载 channel_id 的 UI Signal 钉死在 `qlonglong`,并用真实
64 位 id 走通「信号 → slot → QPixmap → QLabel」完整链路。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import QBuffer, QIODevice, QObject, QSize
from PySide6.QtGui import QPixmap

from tgmonitor.ui.viewmodels._media import MediaMixin

if TYPE_CHECKING:
    from PySide6.QtCore import SignalInstance
    from PySide6.QtWidgets import QApplication

CHANNEL_ID = -1001375475051  # 真实 Telegram 频道 chat_id,32 位 int 装不下
MSG_ID = 123456
MEDIA_IDX = 0


class _VMProbe(QObject):
    """复用生产 Signal 描述符,避免为测信号去构造完整 VM/AppService。"""

    channel_cleared = MediaMixin.channel_cleared
    thumbnail_loaded = MediaMixin.thumbnail_loaded
    open_media_failed = MediaMixin.open_media_failed


def _assert_roundtrip(signal: SignalInstance, payload: tuple[object, ...]) -> None:
    """emit 后 slot 必须收到原样 payload,且首个 Qt 调用不炸。"""
    seen: list[tuple[object, ...]] = []

    def _slot(*args: object) -> None:
        # 残留 OverflowError 会在此处变成 SystemError(用户日志的现场)。
        QPixmap()
        seen.append(args)

    signal.connect(_slot)
    try:
        signal.emit(*payload)
    finally:
        signal.disconnect(_slot)
    assert seen == [payload]


def _png_bytes() -> bytes:
    pix = QPixmap(QSize(2, 2))
    pix.fill()
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pix.save(buf, "PNG")
    return bytes(buf.data())


@pytest.mark.parametrize(
    ("signal_name", "payload"),
    [
        ("channel_cleared", (CHANNEL_ID, 3)),
        ("thumbnail_loaded", (CHANNEL_ID, MSG_ID, MEDIA_IDX, b"data")),
        ("open_media_failed", (CHANNEL_ID, MSG_ID, MEDIA_IDX, "reason")),
    ],
)
def test_vm_signals_carry_64bit_channel_id(
    qapp: QApplication, signal_name: str, payload: tuple[object, ...]
) -> None:
    probe = _VMProbe()
    _assert_roundtrip(getattr(probe, signal_name), payload)
