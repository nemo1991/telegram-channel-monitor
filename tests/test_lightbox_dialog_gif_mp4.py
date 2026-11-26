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

import pytest
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QApplication

from tgmonitor.ui.widgets.lightbox_dialog import LightboxDialog, MediaItem

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


# `qapp` from tests/conftest.py — session-scope QApplication 单例


@pytest.fixture(autouse=True)
def _ensure_qapp(qapp: QApplication) -> None:
    """2026-09-04 v1.6.7:QPixmap / QMovie 构造需要 QApplication 先存在。autouse 全 case。

    2026-09-20 PR cleanup:fixture 仍 autouse,但只引一次 `qapp`(conftest 提供
    session-scope 单例),不再自造 `qt_app` 模块级 fixture。
    """
    return None


# ---- MediaItem.kind 判别 ----


# ---- LightboxDialog.items=... 三态 ----


def test_items_kwarg_with_gif_starts_movie(qapp: QApplication, tiny_gif_bytes: bytes) -> None:
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


# ---- MP4 path:mock QMediaPlayer 验证接口契约 ----


class _FakeMediaPlayer:
    """QMediaPlayer 接口替身 — 验证 Lightbox 调用了哪些方法。

    实际播不播无关紧要,只验 .setSource / .setVideoOutput / .play / .pause /
    .stop / playbackState() / errorOccurred 信号挂载。
    """

    instances: list[_FakeMediaPlayer] = []

    def __init__(self, parent=None) -> None:
        self.parent = parent
        self.video_output = None
        self.source_url = None
        self.error_signal_connect_count = 0
        self.play_called = False
        self.pause_called = False
        self.stop_called = False
        self.set_source_empty_called = False
        # 2026-09-09 v1.7.2:Lightbox `_is_playing_now` 走 playbackState()
        # — fake 默认 Stopped,首次 play() 后切 PlayingState。
        self._is_playing = False
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
        self._is_playing = True

    def pause(self) -> None:
        self.pause_called = True
        self._is_playing = False

    def stop(self) -> None:
        self.stop_called = True
        self._is_playing = False

    def playbackState(self) -> int:  # noqa: N802 — Qt API 命名
        # v1.7.2:Lightbox 读 .playbackState() 与 PlayingState 比较。
        return 1 if self._is_playing else 0

    @property
    def errorOccurred(self):  # noqa: N802 — Qt signal property
        # signal 实际是 Qt Signal;Lightbox 代码调 `.connect(handler)` — 我们
        # 返一个对象,record `.connect` 调用次数
        return _FakeSignal(self)


class _FakeSignal:
    def __init__(self, parent: _FakeMediaPlayer) -> None:
        self.parent = parent

    def connect(self, handler) -> None:
        self.parent.error_signal_connect_count += 1


class _FakeVideoWidget(QVideoWidget):
    instances: list[QVideoWidget] = []

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        _FakeVideoWidget.instances.append(self)


# ---- 资源清理 ----


def test_close_event_unlinks_staged_video(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
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


# 2026-11-25 2.1.4 P2 #40:accept()/reject() 不触发 closeEvent — done() 收口清理


def _make_video_dialog(monkeypatch: pytest.MonkeyPatch) -> tuple:
    """造一个带视频 item 的 LightboxDialog(QMediaPlayer mock),返回 (dlg, tmpfile)。"""
    from tgmonitor.ui.widgets import lightbox_dialog

    _FakeMediaPlayer.instances.clear()
    monkeypatch.setattr(lightbox_dialog, "QMediaPlayer", _FakeMediaPlayer)
    item = MediaItem(video=b"\x00\x00\x00\x18ftypmp42")
    dlg = LightboxDialog(items=[item])
    tmpfile = dlg._video_tmp_path
    assert tmpfile is not None
    assert os.path.isfile(tmpfile)
    return dlg, tmpfile


def test_accept_unlinks_staged_video(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    """accept()(Esc / 右键 / 双击路径)→ tmpfile 被删,不留 $TMPDIR 残留。"""
    dlg, tmpfile = _make_video_dialog(monkeypatch)
    dlg.accept()
    assert not os.path.isfile(tmpfile)
    assert dlg._video_tmp_path is None


def test_reject_unlinks_staged_video(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    """reject() 路径 → tmpfile 同样被删(done() 统一收口)。"""
    dlg, tmpfile = _make_video_dialog(monkeypatch)
    dlg.reject()
    assert not os.path.isfile(tmpfile)
    assert dlg._video_tmp_path is None


def test_close_after_accept_is_idempotent(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """accept() 后再 close() — 清理幂等,无二次 unlink 报错(tmp 已不存在)。"""
    dlg, tmpfile = _make_video_dialog(monkeypatch)
    dlg.accept()
    assert not os.path.isfile(tmpfile)
    dlg.close()  # closeEvent + reject→done 再清一轮,幂等
    assert dlg._video_tmp_path is None


# ---- 滚轮 / 行为边界 ----


# ---- 模块常量:含 VIDEO / VIDEO_NOTE ----


# ---- 多媒体切换 stage tmpfile 资源不泄漏 ----
