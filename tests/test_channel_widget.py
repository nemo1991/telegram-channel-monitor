"""ChannelWidget v1.6.4 单元测试。

覆盖范围:
- `_kind_icon(kind, photo_local_key)` 头像/placeholder fallback
- `_channel_display_text(ch)` 4 字段徽标后缀
- `_apply_photo_changed` 实时刷新 item 图标
- `_apply_title_changed` 重建 item 文本 + 保留徽标
- 集成 `set_items` / `add_item` 路径

不在本 PR 范围:
- 真实 TDLib local_path(改用临时目录 + bytes fixture)
- ChannelWidget UI 主流程 — 见 test_main_window_channels.py

2026-09-04 v1.6.4:spammer 过滤 UI 标徽显示(✓ / [风险])+ 频道头像实时刷新。
"""

from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import EventBus

# ---------------------------------------------------------------------------
# fixtures
# 2026-11-14:删掉本地 `_LoopThread` + function-scope `qloop_16`,统一用
# `tests.fixtures._qt_runtime` 的 session-scope 共享 loop(避免每 test 起
# 一个后台 daemon 线程)。测试函数参数名 `qloop_16` 保持不变。
# ---------------------------------------------------------------------------


def _build_widget(qapp, qloop_16):
    """造一个 ChannelWidget(无 storage 依赖 — 仅 UI 层测试)。"""
    # 简化版:不接 AppService,直接 mock 一个;ChannelWidget.__init__ 接受
    # (app, loop, parent) — 我们用最小的 MagicMock 替代 app 属性。
    from unittest.mock import MagicMock

    from tgmonitor.ui.widgets.channel_widget import ChannelWidget

    app = MagicMock()
    app.bus = EventBus()

    # 2026-11-19:ChannelWidget 构造时会读库填充左栏，mock 需返回真实
    # coroutine（run_coro 要求 coroutine，不能是 MagicMock）。
    async def _empty_joined() -> list:
        return []

    app.channels.list_tg_subscribed_channels = _empty_joined
    widget = ChannelWidget(app, qloop_16)
    return widget


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _png_1x1() -> bytes:
    """1x1 透明 PNG bytes — 测试头像文件读路径用。

    真实 PNG signature + IHDR + IDAT(0 像素纯透明)+ IEND。
    """
    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    raw = b"\x00\x00\x00\x00\x00"  # 1 像素(filter=0, RGBA=0)
    idat = zlib.compress(raw)
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def _channel(
    cid: int = 100,
    *,
    title: str = "Test Channel",
    kind: str = "channel",
    photo_local_key: str | None = None,
    is_verified: bool = False,
    is_scam: bool = False,
    is_fake: bool = False,
    has_protected_content: bool = False,
    username: str | None = None,
) -> ChannelDTO:
    return ChannelDTO(
        id=cid,
        title=title,
        username=username,
        kind=kind,
        photo_local_key=photo_local_key,
        is_verified=is_verified,
        is_scam=is_scam,
        is_fake=is_fake,
        has_protected_content=has_protected_content,
    )


# ---------------------------------------------------------------------------
# _kind_icon
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _channel_display_text
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _apply_title_changed + _apply_photo_changed
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# set_items 集成路径(头像 + 徽标同时)
# ---------------------------------------------------------------------------


def test_set_items_renders_photo_and_badges(
    qapp,
    qloop_16,
    tmp_path: Path,
) -> None:
    """v1.6.4:混合频道(verified / scam / 普通)— set_items 后 list 文本 + 图标。"""
    from tgmonitor.ui.widgets import channel_widget as cw_mod

    cw_mod._channel_thumb_cache = None  # type: ignore[attr-defined]
    png_path = tmp_path / "av.png"
    png_path.write_bytes(_png_1x1())

    widget = _build_widget(qapp, qloop_16)
    channels = [
        _channel(cid=1, title="A", username="a", is_verified=True),
        _channel(cid=2, title="B", username="b", is_scam=True),
        _channel(cid=3, title="C", username="c", photo_local_key=str(png_path)),
    ]
    widget.set_joined(channels)

    # 三行
    assert widget.joined_card.lst.count() == 3
    texts = [widget.joined_card.lst.item(i).text() for i in range(3)]
    assert any("✓" in t for t in texts)
    assert any("[风险]" in t for t in texts)
    # 所有 icon 都非 null(verified/scam 走 emoji fallback;有 photo 走 render)
    for i in range(3):
        assert not widget.joined_card.lst.item(i).icon().isNull()
