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

2026-09-04 v1.6.4:spammer 过滤 UI 标徽显示(✓ / ⚠️)+ 频道头像实时刷新。
"""

from __future__ import annotations

import asyncio
import os
import struct
import threading
import zlib
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.core.dto import ChannelDTO
from tgmonitor.core.events import EventBus

# ---------------------------------------------------------------------------
# fixtures(本地,复用其它 test 的 qapp / qloop 模式,不放 conftest 是 scope 不同)
# ---------------------------------------------------------------------------


class _LoopThread:
    """后台 asyncio loop(模拟 qasync 主线程)— 走 thread+run_forever。"""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture(scope="session")
def qapp_16():
    """确保 QApplication 存在 — offscreen 模式,SVG render 也能跑。"""
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


@pytest.fixture
def qloop_16() -> _LoopThread:
    lt = _LoopThread()
    lt.start()
    yield lt.loop
    lt.stop()
    if lt._thread is not None:
        lt._thread.join(timeout=2)


def _build_widget(qapp_16, qloop_16):  # noqa: ANN001 — 测试 helper
    """造一个 ChannelWidget(无 storage 依赖 — 仅 UI 层测试)。"""
    # 简化版:不接 AppService,直接 mock 一个;ChannelWidget.__init__ 接受
    # (app, loop, parent) — 我们用最小的 MagicMock 替代 app 属性。
    from unittest.mock import MagicMock

    from tgmonitor.ui.widgets.channel_widget import ChannelWidget

    app = MagicMock()
    app.bus = EventBus()
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


def test_kind_icon_no_photo_returns_emoji_placeholder(qapp_16) -> None:
    """v1.6.4:`_kind_icon` 无 photo_local_key → 走 emoji placeholder。"""
    from tgmonitor.ui.widgets.channel_widget import _kind_emoji_icon, _kind_icon

    icon = _kind_icon("channel")
    fallback = _kind_emoji_icon("channel")
    # fallback 与空 photo 路径应等价(同一图标对象或同名图)。
    # 简单判断:两者 isNull 状态一致 + size>0。
    assert not icon.isNull()
    assert not fallback.isNull()


def test_kind_icon_with_photo_uses_thumbnail_cache(qapp_16, tmp_path: Path) -> None:
    """v1.6.4:有 photo_local_key → `ThumbnailCache` LRU 命中。

    测试步骤:
    1. 临时目录写 1x1 PNG 文件
    2. 首次 `_kind_icon(kind, path)` → 触发 cache miss + 同步读文件 + render
    3. 二次 `_kind_icon(kind, path)` → cache hit,QIcon 内部 pixmap 不变
    """
    from tgmonitor.ui.widgets import channel_widget as cw_mod
    from tgmonitor.ui.widgets.channel_widget import _kind_icon

    # 注入干净的 cache 实例(避免被其它 test 污染)
    cw_mod._channel_thumb_cache = None  # type: ignore[attr-defined]
    png_path = tmp_path / "avatar.png"
    png_path.write_bytes(_png_1x1())

    icon1 = _kind_icon("channel", str(png_path))
    assert not icon1.isNull()
    # 触发二次调用 — 命中 cache
    icon2 = _kind_icon("channel", str(png_path))
    assert not icon2.isNull()
    # cache len 应 == 1
    assert len(cw_mod._thumbnail_cache()) == 1


def test_kind_icon_photo_load_failure_falls_back_to_emoji(
    qapp_16,
    tmp_path: Path,
) -> None:
    """v1.6.4:photo_local_key 指向不存在文件 → IO 失败 → 走 emoji 占位。"""
    from tgmonitor.ui.widgets import channel_widget as cw_mod
    from tgmonitor.ui.widgets.channel_widget import _kind_emoji_icon, _kind_icon

    cw_mod._channel_thumb_cache = None  # type: ignore[attr-defined]
    nonexistent = tmp_path / "no_such_avatar.png"

    icon = _kind_icon("channel", str(nonexistent))
    # 不抛 + 非 null + 与 emoji fallback 都不为 null
    assert not icon.isNull()
    emoji_icon = _kind_emoji_icon("channel")
    assert not emoji_icon.isNull()


# ---------------------------------------------------------------------------
# _channel_display_text
# ---------------------------------------------------------------------------


def test_channel_display_text_no_badges(qapp_16) -> None:
    """v1.6.4:无 4 字段时 display = `ch.display`,无徽标后缀。"""
    from tgmonitor.ui.widgets.channel_widget import _channel_display_text

    # ChannelDTO.username 不带 @ 前缀,display 属性加 @
    ch = _channel(username="test")
    text = _channel_display_text(ch)
    assert text == "@test"
    assert "✓" not in text
    assert "⚠" not in text


def test_channel_display_text_verified_badge(qapp_16) -> None:
    """v1.6.4:`is_verified=True` → display 末尾 `✓`。"""
    from tgmonitor.ui.widgets.channel_widget import _channel_display_text

    ch = _channel(username="v", is_verified=True)
    text = _channel_display_text(ch)
    assert text.endswith(" ✓")


def test_channel_display_text_scam_warning(qapp_16) -> None:
    """v1.6.4:`is_scam=True` → display 末尾 `⚠️`。"""
    from tgmonitor.ui.widgets.channel_widget import _channel_display_text

    ch = _channel(username="s", is_scam=True)
    text = _channel_display_text(ch)
    assert "⚠" in text


def test_channel_display_text_fake_warning(qapp_16) -> None:
    """v1.6.4:`is_fake=True` → display 末尾 `⚠️`(与 scam 同后缀,不双标)。"""
    from tgmonitor.ui.widgets.channel_widget import _channel_display_text

    ch = _channel(username="f", is_fake=True)
    text = _channel_display_text(ch)
    # 一个 ⚠ 即可(任一为真)
    assert text.count("⚠") == 1


def test_channel_display_text_verified_and_scam(qapp_16) -> None:
    """v1.6.4:`is_verified` + `is_scam` 同时 → ✓ ⚠️ 都出现。"""
    from tgmonitor.ui.widgets.channel_widget import _channel_display_text

    ch = _channel(is_verified=True, is_scam=True)
    text = _channel_display_text(ch)
    assert "✓" in text
    assert "⚠" in text


# ---------------------------------------------------------------------------
# _apply_title_changed + _apply_photo_changed
# ---------------------------------------------------------------------------


def test_apply_title_changed_preserves_verified_badge(qapp_16, qloop_16) -> None:
    """v1.6.4:title 更新后徽标后缀(✓ / ⚠️)保留。"""
    widget = _build_widget(qapp_16, qloop_16)
    # 无 username → display = `#id title`,title 改动会反映到文本上
    ch = _channel(cid=100, title="Old", username=None, is_verified=True)
    widget.set_joined([ch])
    widget._apply_title_changed(100, "New")

    item = widget.joined_card.lst.item(0)
    assert item is not None
    # display 用 `#100 <title>` 格式
    text = item.text()
    assert "#100" in text
    assert "New" in text
    assert "Old" not in text
    assert "✓" in text  # 徽标保留


def test_apply_photo_changed_with_path_updates_item_icon(
    qapp_16,
    qloop_16,
    tmp_path: Path,
) -> None:
    """v1.6.4:`_apply_photo_changed(100, local_path)` → 替换 item 图标。

    验证:`_apply_photo_changed` 把 photo_local_key 写入 _joined 缓存 + 调
    `_kind_icon` 渲染新图标后 `item.setIcon`。测试用 1x1 PNG 作真实头像
    文件路径。
    """
    from tgmonitor.ui.widgets import channel_widget as cw_mod

    cw_mod._channel_thumb_cache = None  # type: ignore[attr-defined]
    widget = _build_widget(qapp_16, qloop_16)
    ch = _channel(cid=100, title="X", username="x")
    widget.set_joined([ch])

    png_path = tmp_path / "x.png"
    png_path.write_bytes(_png_1x1())
    widget._apply_photo_changed(100, str(png_path))

    assert widget._joined[100].photo_local_key == str(png_path)
    item = widget.joined_card.lst.item(0)
    assert item is not None
    # icon 不为 null(成功 load 或 fallback 都不 null)
    assert not item.icon().isNull()
    # cache 应已写入
    assert len(cw_mod._thumbnail_cache()) >= 1


def test_apply_photo_changed_none_reverts_to_kind_icon(
    qapp_16,
    qloop_16,
    tmp_path: Path,
) -> None:
    """v1.6.4:`_apply_photo_changed(100, None)` → 头像被删,退回 kind 占位。"""
    from tgmonitor.ui.widgets import channel_widget as cw_mod

    cw_mod._channel_thumb_cache = None  # type: ignore[attr-defined]
    widget = _build_widget(qapp_16, qloop_16)
    png_path = tmp_path / "y.png"
    png_path.write_bytes(_png_1x1())
    # 初始有 photo
    ch = _channel(cid=100, title="Y", username="y", photo_local_key=str(png_path))
    widget.set_joined([ch])

    # 头像被删 → None
    widget._apply_photo_changed(100, None)
    assert widget._joined[100].photo_local_key is None
    item = widget.joined_card.lst.item(0)
    assert item is not None
    # 仍非 null(走 kind emoji fallback)
    assert not item.icon().isNull()


# ---------------------------------------------------------------------------
# set_items 集成路径(头像 + 徽标同时)
# ---------------------------------------------------------------------------


def test_set_items_renders_photo_and_badges(
    qapp_16,
    qloop_16,
    tmp_path: Path,
) -> None:
    """v1.6.4:混合频道(verified / scam / 普通)— set_items 后 list 文本 + 图标。"""
    from tgmonitor.ui.widgets import channel_widget as cw_mod

    cw_mod._channel_thumb_cache = None  # type: ignore[attr-defined]
    png_path = tmp_path / "av.png"
    png_path.write_bytes(_png_1x1())

    widget = _build_widget(qapp_16, qloop_16)
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
    assert any("⚠" in t for t in texts)
    # 所有 icon 都非 null(verified/scam 走 emoji fallback;有 photo 走 render)
    for i in range(3):
        assert not widget.joined_card.lst.item(i).icon().isNull()
