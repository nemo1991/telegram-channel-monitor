"""v1.6.7 LightboxDialog GIF / MP4 多模态测试 — 2026-09-04。

覆盖:
- `MediaItem` dataclass kind 判别(image/gif/video/empty)
- `LightboxDialog` 新接口 `items=[MediaItem(...)]` 三态分发
- `_render_gif` QMovie 装到 canvas + start()
- `_render_video` QMediaPlayer 构造 + setSource + play + errorOccurred connect
- MP4 codec miss → `fallback_fn` 被调 + dialog 自动 accept
- `_stop_active_player` 严格清理 QMovie + QMediaPlayer + QVideoWidget
- `closeEvent` unlink staged tmpfile
- GIF ↔ image 多 item 切换(must stop _movie between items)
- 老 `pixmaps=` kwarg 向后兼容(0 regression)
- `show_lightbox(items=...)` 便利构造
- wheelEvent 在 video 上不缩放(透传给 QVideoWidget)
- 模块常量 `LIGHTBOX_PREVIEWABLE_TYPES` 含 VIDEO / VIDEO_NOTE

测试环境 `QT_QPA_PLATFORM=offscreen` — QMovie(image-only)能播;
QMediaPlayer(codec-dependent)真实播不出,我们 mock 整个 class 走接口契约。

GIF fixture:42-byte 1x1 GIF89a(Wikipedia「smallest valid GIF89a」)— 写入
`tests/fixtures/data/tiny.gif` 一次性生成,subsequent run 复用。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtMultimediaWidgets import QVideoWidget  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tgmonitor.core.dto import MediaType  # noqa: E402
from tgmonitor.ui.widgets.lightbox_dialog import LightboxDialog, MediaItem  # noqa: E402
from tgmonitor.ui.widgets.media_manager_widget import LIGHTBOX_PREVIEWABLE_TYPES  # noqa: E402

# ---- GIF fixture:42-byte 1x1 GIF89a(Wikipedia 「smallest valid GIF89a」) ----

# Hex bytes 来自 https://en.wikipedia.org/wiki/GIF(2026-09-04 检索),手工校验
# QImage.fromData / QMovie.parse 均通过。一次性写入 tests/fixtures/data/tiny.gif,
# 后续 run 复用,不重复生成。
_TINY_GIF_HEX = (
    "47494638396101000100800000000000ffffff21f90401000000002c000000000100010000020144003b"
)


def _ensure_tiny_gif(path: str) -> bytes:
    """确保 path 存在并含 GIF89a bytes;返文件内容。"""
    if not os.path.isfile(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(bytes.fromhex(_TINY_GIF_HEX))
    with open(path, "rb") as f:
        return f.read()


@pytest.fixture(scope="session")
def tiny_gif_bytes() -> bytes:
    """2026-09-04 v1.6.7:42-byte 1x1 GIF89a bytes — QMovie/QPixmap 都接受。"""
    path = os.path.join(os.path.dirname(__file__), "fixtures", "data", "tiny.gif")
    return _ensure_tiny_gif(path)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    """模块级 QApplication — 多个 LightboxDialog 实例共享。"""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def _ensure_qapp(qt_app: QApplication) -> None:
    """2026-09-04 v1.6.7:QPixmap / QMovie 构造需要 QApplication 先存在。autouse 全 case。"""
    return None


# ---- MediaItem.kind 判别 ----


def test_media_item_kind_image() -> None:
    """MediaItem(pixmap=...) → kind='image'。"""
    item = MediaItem(pixmap=QPixmap())
    assert item.kind == "image"


def test_media_item_kind_gif() -> None:
    """MediaItem(animated=b'...') → kind='gif'。"""
    item = MediaItem(animated=b"GIF89a")
    assert item.kind == "gif"


def test_media_item_kind_video() -> None:
    """MediaItem(video=b'...') → kind='video'。"""
    item = MediaItem(video=b"\x00\x00\x00\x18ftypmp42")
    assert item.kind == "video"


def test_media_item_kind_empty_when_all_none() -> None:
    """三个字段都 None → kind='empty'(理论上构造不该发生)。"""
    item = MediaItem()
    assert item.kind == "empty"


# ---- LightboxDialog.items=... 三态 ----


def test_items_kwarg_with_gif_starts_movie(qt_app: QApplication, tiny_gif_bytes: bytes) -> None:
    """items=[MediaItem(animated=...)] → _render_gif → _movie 装上 canvas 并 start()。

    验:
    - dlg._movie is not None
    - dlg.current_kind == "gif"
    - _movie.isValid() + _movie.state() in (Running, NotRunning — start() 可能异步)
    """
    item = MediaItem(animated=tiny_gif_bytes, mime_type="image/gif")
    dlg = LightboxDialog(items=[item], current=-1, title="test.gif")

    try:
        assert dlg._movie is not None
        assert dlg._movie.isValid()
        assert dlg.current_kind == "gif"
        # canvas 已绑 movie
        assert dlg._canvas.movie() is dlg._movie
    finally:
        dlg.close()


def test_items_kwarg_with_pixmap_backcompat(qt_app: QApplication) -> None:
    """items=[MediaItem(pixmap=...)] → 走 image 路径,canvas 拿到 pixmap。"""
    pix = QPixmap(2, 2)
    pix.fill(Qt.red)
    item = MediaItem(pixmap=pix, mime_type="image/png")
    dlg = LightboxDialog(items=[item], current=-1, title="img.png")

    try:
        assert dlg.current_kind == "image"
        assert dlg._movie is None  # 没起 QMovie
        # 老 _all_pixmaps 仍同步,21 个 v1.5.0 老测试用这个
        assert dlg._all_pixmaps == [pix]
    finally:
        dlg.close()


def test_old_pixmaps_kwarg_still_works(qt_app: QApplication) -> None:
    """2026-09-04 v1.6.7 向后兼容:老 `pixmaps=` kwarg 走 image 路径,与 v1.5.x 一致。"""
    pix = QPixmap(2, 2)
    pix.fill(Qt.blue)
    dlg = LightboxDialog(pixmaps=[pix], current=-1, title="legacy.png")

    try:
        assert dlg.current_kind == "image"
        assert dlg._all_pixmaps == [pix]
        assert dlg._all_items == [MediaItem(pixmap=pix)]
        # 没有 gif/video 状态
        assert dlg._movie is None
        assert dlg._player is None
        assert dlg._video_widget is None
    finally:
        dlg.close()


# ---- MP4 path:mock QMediaPlayer 验证接口契约 ----


class _FakeMediaPlayer:
    """QMediaPlayer 接口替身 — 验证 Lightbox 调用了哪些方法。

    实际播不播无关紧要,只验 .setSource / .setVideoOutput / .play / errorOccurred
    信号挂载。
    """

    instances: list[_FakeMediaPlayer] = []

    def __init__(self, parent=None) -> None:
        self.parent = parent
        self.video_output = None
        self.source_url = None
        self.error_signal_connect_count = 0
        self.play_called = False
        self.stop_called = False
        self.set_source_empty_called = False
        _FakeMediaPlayer.instances.append(self)

    def setVideoOutput(self, widget):  # noqa: N802 — Qt API 命名
        self.video_output = widget

    def setSource(self, url):  # noqa: N802 — Qt API 命名
        if url is None or url.toString() == "":
            self.set_source_empty_called = True
        else:
            self.source_url = url

    def play(self) -> None:
        self.play_called = True

    def stop(self) -> None:
        self.stop_called = True

    @property
    def errorOccurred(self):  # noqa: N802 — Qt signal property
        # signal 实际是 Qt Signal;Lightbox 代码调 `.connect(handler)` — 我们
        # 返一个对象,record `.connect` 调用次数
        return _FakeSignal(self)


class _FakeSignal:
    def __init__(self, parent: _FakeMediaPlayer) -> None:
        self.parent = parent

    def connect(self, handler) -> None:  # noqa: ANN001 — Qt slot signature
        self.parent.error_signal_connect_count += 1


class _FakeVideoWidget(QVideoWidget):
    instances: list[QVideoWidget] = []

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        _FakeVideoWidget.instances.append(self)


def test_items_kwarg_with_video_sets_up_player(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """items=[MediaItem(video=...)] → QMediaPlayer 构造 + setSource + play + errorOccurred connect。"""
    # monkeypatch QMediaPlayer / QVideoWidget 在 lightbox_dialog 模块里
    from tgmonitor.ui.widgets import lightbox_dialog

    _FakeMediaPlayer.instances.clear()
    _FakeVideoWidget.instances.clear()

    monkeypatch.setattr(lightbox_dialog, "QMediaPlayer", _FakeMediaPlayer)
    monkeypatch.setattr(lightbox_dialog, "QVideoWidget", _FakeVideoWidget)

    item = MediaItem(video=b"\x00\x00\x00\x18ftypmp42", mime_type="video/mp4")
    dlg = LightboxDialog(items=[item], current=-1, title="clip.mp4")

    try:
        assert dlg.current_kind == "video"
        # 一个 player / 一个 widget
        assert len(_FakeMediaPlayer.instances) == 1
        assert len(_FakeVideoWidget.instances) == 1
        player = _FakeMediaPlayer.instances[0]
        # 接口契约被满足
        assert player.setVideoOutput is not None  # called with widget
        assert player.video_output is _FakeVideoWidget.instances[0]
        assert player.source_url is not None
        assert player.source_url.toString().endswith(".mp4")
        assert player.play_called is True
        assert player.error_signal_connect_count == 1
        # stage 的 tmpfile 路径被记录到 dlg
        assert dlg._video_tmp_path is not None
        assert os.path.isfile(dlg._video_tmp_path)
    finally:
        dlg.close()


def test_video_fallback_called_on_player_error(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-09-04 v1.6.7:QMediaPlayer 抛 errorOccurred → fallback_fn 被调 + dialog accept。"""
    from tgmonitor.ui.widgets import lightbox_dialog

    _FakeMediaPlayer.instances.clear()
    monkeypatch.setattr(lightbox_dialog, "QMediaPlayer", _FakeMediaPlayer)

    fallback_called: list[bool] = []

    def fallback() -> None:
        fallback_called.append(True)

    item = MediaItem(video=b"\x00\x00\x00\x18ftypmp42")
    dlg = LightboxDialog(items=[item], fallback_fn=fallback)

    # 触发 errorOccurred(模拟 codec miss)— dlg._on_video_error 是
    # errorOccurred.connect 注册的 handler,直接调它模拟 signal 触发
    dlg._on_video_error(QMediaPlayer.Error.ResourceError, "no codec")

    assert fallback_called == [True]
    assert dlg._player is None  # _stop_active_player 已清
    assert dlg._video_widget is None


def test_video_fallback_no_fn_shows_message(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fallback_fn 为 None → error 后 dialog 不关,显示 '(video unavailable — codec missing)'。"""
    from tgmonitor.ui.widgets import lightbox_dialog

    _FakeMediaPlayer.instances.clear()
    monkeypatch.setattr(lightbox_dialog, "QMediaPlayer", _FakeMediaPlayer)

    item = MediaItem(video=b"\x00\x00\x00\x18ftypmp42")
    dlg = LightboxDialog(items=[item])  # fallback_fn=None

    dlg._on_video_error(QMediaPlayer.Error.ResourceError, "no codec")

    assert dlg._player is None
    # canvas 显示 fallback 文案
    assert "video unavailable" in dlg._canvas.text()


# ---- 资源清理 ----


def test_close_event_unlinks_staged_video(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """closeEvent unlink _video_tmp_path,不留 garbage。"""
    from tgmonitor.ui.widgets import lightbox_dialog

    _FakeMediaPlayer.instances.clear()
    monkeypatch.setattr(lightbox_dialog, "QMediaPlayer", _FakeMediaPlayer)

    item = MediaItem(video=b"\x00\x00\x00\x18ftypmp42")
    dlg = LightboxDialog(items=[item])

    tmpfile = dlg._video_tmp_path
    assert tmpfile is not None
    assert os.path.isfile(tmpfile)

    dlg.close()

    # unlink 已删
    assert not os.path.isfile(tmpfile)
    assert dlg._video_tmp_path is None


def test_stop_active_player_clears_movie_and_player(
    qt_app: QApplication, tiny_gif_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_stop_active_player 同时清 QMovie + QMediaPlayer(先 GIF 再 video 切换场景)。"""
    from tgmonitor.ui.widgets import lightbox_dialog

    _FakeMediaPlayer.instances.clear()
    monkeypatch.setattr(lightbox_dialog, "QMediaPlayer", _FakeMediaPlayer)

    items = [
        MediaItem(animated=tiny_gif_bytes, mime_type="image/gif"),
        MediaItem(video=b"\x00\x00\x00\x18ftypmp42", mime_type="video/mp4"),
    ]
    dlg = LightboxDialog(items=items)

    # 初始 idx=0 → gif
    assert dlg.current_kind == "gif"
    assert dlg._movie is not None
    assert dlg._player is None

    # 切到 idx=1 → video(触发 _stop_active_player 清旧 _movie)
    dlg._step_index(+1)
    assert dlg.current_kind == "video"
    assert dlg._movie is None  # 旧 gif 已被清
    assert dlg._player is not None  # 新 video player

    # 再切回 idx=0 → gif(触发 _stop_active_player 清旧 video player)
    dlg._step_index(-1)
    assert dlg.current_kind == "gif"
    assert dlg._movie is not None
    # video player 已 stop + 清空 source
    last_player = _FakeMediaPlayer.instances[0]
    assert last_player.stop_called is True
    assert last_player.set_source_empty_called is True

    dlg.close()


# ---- 滚轮 / 行为边界 ----


def test_wheel_event_on_video_does_not_zoom(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-09-04 v1.6.7:video 状态下 wheel 不缩放,透传给 QVideoWidget。"""
    from tgmonitor.ui.widgets import lightbox_dialog

    _FakeMediaPlayer.instances.clear()
    monkeypatch.setattr(lightbox_dialog, "QMediaPlayer", _FakeMediaPlayer)

    item = MediaItem(video=b"\x00\x00\x00\x18ftypmp42")
    dlg = LightboxDialog(items=[item])

    initial_zoom = dlg.current_zoom
    # 模拟 wheel up 100 — 不应改变 zoom
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QWheelEvent

    event = QWheelEvent(
        QPoint(10, 10),
        QPoint(10, 10),
        QPoint(0, 0),
        QPoint(0, 120),  # delta y > 0
        Qt.NoButton,
        Qt.NoModifier,
        Qt.ScrollBegin,  # event_type
        False,
    )
    dlg.wheelEvent(event)

    assert dlg.current_zoom == initial_zoom  # 没变
    dlg.close()


def test_show_lightbox_accepts_items_kwarg(qt_app: QApplication, tiny_gif_bytes: bytes) -> None:
    """show_lightbox(items=...) 便利构造 + showFullScreen。"""
    from tgmonitor.ui.widgets.lightbox_dialog import show_lightbox

    item = MediaItem(animated=tiny_gif_bytes)
    dlg = show_lightbox(items=[item], title="via-show.gif")

    try:
        assert isinstance(dlg, LightboxDialog)
        assert dlg.current_kind == "gif"
        # full screen 调用不一定在 offscreen 环境真生效,但 isVisible 应该为 True
        # (实际 offscreen 上 showFullScreen 退化为 show,依然 visible)
        assert dlg.isVisible()
    finally:
        dlg.close()


# ---- 模块常量:含 VIDEO / VIDEO_NOTE ----


def test_lightbox_previewable_types_includes_video() -> None:
    """2026-09-04 v1.6.7:LIGHTBOX_PREVIEWABLE_TYPES 含 VIDEO + VIDEO_NOTE。"""
    assert MediaType.VIDEO in LIGHTBOX_PREVIEWABLE_TYPES
    assert MediaType.VIDEO_NOTE in LIGHTBOX_PREVIEWABLE_TYPES
    # 老 path 仍包含
    assert MediaType.PHOTO in LIGHTBOX_PREVIEWABLE_TYPES
    assert MediaType.STICKER in LIGHTBOX_PREVIEWABLE_TYPES
    assert MediaType.ANIMATION in LIGHTBOX_PREVIEWABLE_TYPES
    # 排除 AUDIO / VOICE / DOCUMENT(仍走系统查看器)
    assert MediaType.AUDIO not in LIGHTBOX_PREVIEWABLE_TYPES
    assert MediaType.VOICE not in LIGHTBOX_PREVIEWABLE_TYPES
    assert MediaType.DOCUMENT not in LIGHTBOX_PREVIEWABLE_TYPES


# ---- 多媒体切换 stage tmpfile 资源不泄漏 ----


def test_switching_to_then_from_video_cleans_up(
    qt_app: QApplication, tiny_gif_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gif → video → gif 切换:video 阶段 stage 的 tmpfile 在 close 时被 unlink。"""
    from tgmonitor.ui.widgets import lightbox_dialog

    _FakeMediaPlayer.instances.clear()
    monkeypatch.setattr(lightbox_dialog, "QMediaPlayer", _FakeMediaPlayer)

    items = [
        MediaItem(animated=tiny_gif_bytes),
        MediaItem(video=b"\x00\x00\x00\x18ftypmp42"),
    ]
    dlg = LightboxDialog(items=items)

    # 切到 video
    dlg._step_index(+1)
    staged = dlg._video_tmp_path
    assert staged is not None and os.path.isfile(staged)

    # 切回 gif(video tmpfile 暂时留着,_video_tmp_path 还指向它)
    dlg._step_index(-1)
    assert dlg._video_tmp_path == staged

    # close 时 unlink
    dlg.close()
    assert not os.path.isfile(staged)
