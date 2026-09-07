# mypy: disable-error-code="attr-defined"
"""Lightbox 图片 / GIF / 视频内嵌预览 — 2026-08-31 v1.5.0 PR #A8 + 2026-09-04 v1.6.7。

设计要点:
- `QDialog` Frameless + WindowStaysOnTopHint + 黑色半透背景;`showFullScreen()`
  占满整个屏幕,中央显示媒体(QPixmap / QMovie / QMediaPlayer)
- **三种媒体形态**(v1.6.7):
    - 静态图(JPG/PNG/WebP/Sticker thumbnail)— QPixmap
    - 动画 GIF — QMovie + QLabel.setMovie
    - MP4 视频 — QMediaPlayer + QVideoWidget
- **媒体来源**:构造时传 `Sequence[MediaItem]`(新接口)或 `Sequence[QPixmap]`
  (老接口,向后兼容);bytes 由调用方提前异步加载,UI 持 QMediaItem 即可
- **多图切换**:构造时传多个 item + `current: int`,左/右方向键翻页(wrap),
  默认 -1 = 单图模式
- **缩放**:鼠标滚轮围绕 `scale_step` (1.25×) 缩放,Min 0.25× / Max 8×;
  当前缩放比例显示在右下角小 label
- **Esc 关闭**:`keyPressEvent` 拦截 Esc;鼠标右键 / 双击也关闭
- **MP4 fallback**:`QMediaPlayer` 在 Linux 缺 GStreamer plugins 可能播不了 —
  `errorOccurred` 信号触发 graceful fallback:把 media bytes 写到 tmpfile
  + 走调用方注入的 `fallback_fn`(通常是 vm.open_media 系统查看器)
- **资源清理**:`closeEvent` + `_stop_active_player` 严格 stop QMovie /
  QMediaPlayer / 解绑 video widget / unlink tmpfile,防 dangling decoder
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, Qt, QUrl
from PySide6.QtGui import QCloseEvent, QKeyEvent, QMovie, QPixmap, QWheelEvent
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QVBoxLayout,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


# ---------- MediaItem:Lightbox 单条媒体抽象(2026-09-04 v1.6.7) ----------


@dataclass(frozen=True)
class MediaItem:
    """Lightbox 单条媒体 — 三种形态:

    - pixmap:静态图(JPG/PNG/WebP/Sticker thumbnail)— `QLabel.setPixmap`
    - animated:动画 GIF bytes(`image/gif`)— QMovie 装到 QLabel
    - video:MP4 bytes(`video/mp4`)— QMediaPlayer + QVideoWidget

    三者互斥(只设一个);`kind` property 自动判别。
    """

    pixmap: QPixmap | None = None
    animated: bytes | None = None
    video: bytes | None = None
    mime_type: str = ""  # 诊断 / 日志用

    @property
    def kind(self) -> str:
        """`image` / `gif` / `video` / `empty`。"""
        if self.pixmap is not None:
            return "image"
        if self.animated is not None:
            return "gif"
        if self.video is not None:
            return "video"
        return "empty"


# ---------- LightboxDialog ----------


class LightboxDialog(QDialog):
    """Frameless 全屏预览图片 / GIF / 视频,支持左/右切换 + 滚轮缩放 + Esc 关闭。

    Parameters
    ----------
    pixmaps : Sequence[QPixmap] | None
        一组已加载的图片(单图时传 `[pix]`)。空 → 自动 close,不弹窗。
        向后兼容 v1.5.x 老接口。
    items : Sequence[MediaItem] | None
        新接口(2026-09-04 v1.6.7)— 多模态媒体序列。传 `items` 时 `pixmaps` 忽略。
    current : int
        当前显示索引。`-1` 表示单图模式(不可翻)。其它值需在
        `[0, len(items))` 内。
    title : str, optional
        顶部居中小标题(可选,空字符串不显示)
    fallback_fn : Callable[[], None] | None
        MP4 codec miss / 严重错误时调,触发系统外部播放器 fallback;
        通常是 `lambda: vm.open_media(channel_id, msg_id, idx)`
    """

    def __init__(
        self,
        pixmaps: Sequence[QPixmap] | None = None,
        current: int = -1,
        title: str = "",
        parent: QDialog | None = None,
        *,
        items: Sequence[MediaItem] | None = None,
        fallback_fn: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)

        # ---- 媒体列表(items 优先,否则 pixmaps) ----
        # 2026-09-04 v1.6.7:`_all_items` 是新主存;`_all_pixmaps` 保留作
        # 兼容属性(21 个老测试用 `dlg._all_pixmaps == [...]` 断言)。
        if items is not None:
            self._all_items: list[MediaItem] = list(items)
            self._all_pixmaps: list[QPixmap] = [
                i.pixmap for i in self._all_items if i.pixmap is not None
            ]
        else:
            self._all_pixmaps = list(pixmaps or [])
            self._all_items = [MediaItem(pixmap=p) for p in self._all_pixmaps]
        self._idx = current if current >= 0 else 0 if self._all_items else -1
        self._zoom = 1.0
        self._min_zoom = 0.25
        self._max_zoom = 8.0
        self._step = 1.25  # 滚轮一档 1.25×
        self._movie: QMovie | None = None  # GIF 动画 cleanup 用
        # 2026-09-04 v1.6.7:MP4 媒体播放状态
        self._player: QMediaPlayer | None = None
        self._video_widget: QVideoWidget | None = None
        self._video_tmp_path: str | None = None  # stage 的 tmpfile 路径,closeEvent unlink
        self._fallback_fn = fallback_fn

        # ---- 窗口外观 ----
        # Frameless + 始终置顶 + 工具窗口(任务栏不出现条目);半透背景由 stylesheet 实现
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setStyleSheet("LightboxDialog { background-color: rgba(0, 0, 0, 220); }")

        # ---- 内容布局 ----
        # 外层 QFrame 提供可读背景(避免全透字串);内嵌 QLabel 居中
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._title_label = QLabel(title, self) if title else None
        if self._title_label is not None:
            self._title_label.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
            self._title_label.setStyleSheet(
                "color: white; background: transparent; padding: 8px; font-size: 14px;"
            )
            self._title_label.setFixedHeight(28)
            outer.addWidget(self._title_label)

        # 居中画布 — QLabel stretch=1 自适应
        self._canvas = QLabel(self)
        self._canvas.setAlignment(Qt.AlignCenter)
        self._canvas.setStyleSheet("background: transparent;")
        outer.addWidget(self._canvas, 1)

        # 缩放比例 hint(右下角)— 静态 label,值改就 setText
        self._zoom_label = QLabel(self)
        self._zoom_label.setAlignment(Qt.AlignBottom | Qt.AlignRight)
        self._zoom_label.setStyleSheet(
            "color: rgba(255,255,255,180); background: transparent;"
            " padding: 4px 8px; font-size: 12px;"
        )
        self._zoom_label.setFixedHeight(24)
        outer.addWidget(self._zoom_label)

        # 初始渲染
        self._render_current()

    # ---- 公有 API ----

    @property
    def current_index(self) -> int:
        """当前显示索引(单图模式恒为 0)。"""
        return self._idx

    @property
    def current_zoom(self) -> float:
        """当前缩放比例。"""
        return self._zoom

    @property
    def current_kind(self) -> str:
        """当前 item 的媒体类型(image/gif/video/empty)— 2026-09-04 v1.6.7。"""
        if 0 <= self._idx < len(self._all_items):
            return self._all_items[self._idx].kind
        return "empty"

    # ---- 渲染核心 ----

    def _render_current(self) -> None:
        """根据当前 item.kind 三态分发渲染(image/gif/video)。"""
        if not self._all_items:
            self.reject()
            return
        if self._idx < 0 or self._idx >= len(self._all_items):
            self.reject()
            return

        # 切 item 前先清旧媒体(防 decoder / animation timer dangling)
        self._stop_active_player()

        item = self._all_items[self._idx]
        if item.kind == "image" and item.pixmap is not None:
            self._render_image(item.pixmap)
        elif item.kind == "gif" and item.animated:
            self._render_gif(item.animated)
        elif item.kind == "video" and item.video:
            self._render_video(item.video)
        else:
            # 空 item / 全部 None
            self._canvas.setPixmap(QPixmap())
            self._canvas.setText("(image unavailable)")
            self._update_zoom_label()

    def _render_image(self, pix: QPixmap) -> None:
        """静态图路径 — 与 v1.5.0 PR #A8 完全一致。"""
        if pix.isNull():
            # 注意顺序:先 setPixmap(null) 清掉老 pixmap,再 setText;否则
            # QLabel.setPixmap 会把已设置的 text 清掉(实测 Qt 6.11 行为)。
            self._canvas.setPixmap(QPixmap())
            self._canvas.setText("(image unavailable)")
            self._update_zoom_label()
            return
        self._apply_scaled_pixmap(pix)
        self._update_zoom_label()

    def _render_gif(self, data: bytes) -> None:
        """2026-09-04 v1.6.7:GIF bytes → QMovie 装到 canvas。

        PySide6 6.11 没有 `QMovie.loadFromData` 绑定(只暴露 `__init__(QIODevice)`),
        所以走 `QBuffer` 路径:bytes → QByteArray → QBuffer(ReadOnly)→ QMovie(buf)。
        QMovie 构造失败时降级到第一帧(QPixmap.loadFromData)— 让用户至少
        看到一张静态图,不黑屏。
        """
        ba = QByteArray(data)
        buf = QBuffer()
        buf.setData(ba)
        if not buf.open(QIODevice.ReadOnly):
            log.warning("Lightbox: QBuffer.open failed for GIF bytes")
            self._show_unavailable()
            return
        self._movie = QMovie(buf)
        self._movie.setFormat(b"gif")
        if not self._movie.isValid():
            log.warning("Lightbox: QMovie.isValid()=False, falling back to first frame")
            self._movie = None
            buf.close()
            pix = QPixmap()
            if pix.loadFromData(ba, b"GIF"):
                self._apply_scaled_pixmap(pix)
            else:
                self._show_unavailable()
            self._update_zoom_label()
            return
        self._canvas.setMovie(self._movie)
        self._movie.start()
        # GIF 不支持缩放(动画逐帧 dec)— zoom label 仍显示但固定 100%
        self._update_zoom_label()

    def _show_unavailable(self) -> None:
        """占位 — image / GIF decode 失败都走这里。"""
        self._canvas.setPixmap(QPixmap())
        self._canvas.setText("(image unavailable)")

    def _render_video(self, data: bytes) -> None:
        """2026-09-04 v1.6.7:MP4 bytes → QMediaPlayer + QVideoWidget。

        Linux offscreen CI 上 GStreamer plugins 不可用 → QMediaPlayer
        构造可能失败 / errorOccurred 触发 → 走 `_show_video_fallback`
        (调 `fallback_fn`,通常是系统查看器)。
        """
        # stage 到 tmpfile — `setSource` 接受 QUrl.fromLocalFile,某些平台
        # `setMedia(QByteArray)` 有兼容问题,tmpfile 最稳
        tmp_path = self._stage_video_tmp(data)
        if tmp_path is None:
            self._show_video_fallback()
            self._update_zoom_label()
            return

        try:
            self._video_widget = QVideoWidget(self)
            self._player = QMediaPlayer(self)
            self._player.setVideoOutput(self._video_widget)
            self._player.setSource(QUrl.fromLocalFile(tmp_path))
            self._player.errorOccurred.connect(self._on_video_error)  # noqa: F821 — Qt slot
            # 把 _canvas 替换成 _video_widget — layout 接管
            outer = self.layout()
            assert outer is not None
            outer.replaceWidget(self._canvas, self._video_widget)
            self._video_widget.show()
            self._player.play()
        except Exception:  # noqa: BLE001 — QMediaPlayer 构造 / signal connect 都可能 throw
            log.warning("Lightbox: QMediaPlayer setup failed", exc_info=True)
            self._show_video_fallback()

        self._update_zoom_label()

    def _on_video_error(self, *args: object) -> None:
        """2026-09-04 v1.6.7:`QMediaPlayer.errorOccurred` 信号 handler。

        Qt 6.x 信号签名:`errorOccurred(error: QMediaPlayer.Error, errorString: str)`。
        我们只关心是否出错,不细分类型。
        """
        # args 可能是 (error, error_string) 或 (error,) — 兼容两种
        error_str = str(args[1]) if len(args) > 1 and args[1] else str(args[0])
        log.warning("Lightbox: QMediaPlayer error: %s", error_str)
        self._show_video_fallback()

    def _show_video_fallback(self) -> None:
        """MP4 codec miss / 严重错误 → graceful fallback 到系统外部播放器。

        调用方注入的 `fallback_fn`(通常 `lambda: vm.open_media(...)`)。
        没有 fallback_fn 就显示提示文字。
        """
        self._stop_active_player()
        self._canvas.setPixmap(QPixmap())
        if self._fallback_fn is not None:
            try:
                self._fallback_fn()
            except Exception:  # noqa: BLE001
                log.warning("Lightbox: fallback_fn raised", exc_info=True)
            # 关掉自己 — fallback 走系统 viewer 后 Lightbox 没意义继续存在
            self.accept()
            return
        self._canvas.setText("(video unavailable — codec missing)")

    def _apply_scaled_pixmap(self, pix: QPixmap) -> None:
        """按 self._zoom 缩放 + 居中显示;屏幕尺寸 = 当前主屏 90%。"""
        screen = QApplication.primaryScreen()
        if screen is None:
            target_size = pix.size()
        else:
            screen_size = screen.size()
            target_size = screen_size * 0.9
        # 缩放 = pix 大小 × zoom,再按 target 缩到不大于
        scaled = pix.scaled(
            target_size * self._zoom,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self._canvas.setPixmap(scaled)

    def _update_zoom_label(self) -> None:
        n = len(self._all_items)
        pos = f"{self._idx + 1}/{n}" if n > 1 else ""
        zoom_pct = f"{int(self._zoom * 100)}%"
        kind_tag = ""
        if self.current_kind == "gif":
            kind_tag = " [GIF]"
        elif self.current_kind == "video":
            kind_tag = " [VIDEO]"
        hint = f"{zoom_pct}{kind_tag}  {pos}".strip()
        self._zoom_label.setText(hint)

    # ---- 媒体切换 / 资源释放 ----

    def _stop_active_player(self) -> None:
        """切 item / close dialog 前停 QMovie + QMediaPlayer,解绑 video widget。

        严格顺序:stop → 解绑 widget → deleteLater → 清引用。否则
        decoder 后台线程 / animation timer 会触发已销毁 widget 抛 RuntimeError。
        """
        if self._movie is not None:
            try:
                self._movie.stop()
            except RuntimeError:  # pragma: no cover — 二次 close 防
                pass
            # 解绑 QLabel 上的 QMovie — 传空 QMovie(Qt 接受此 idiom)
            try:
                self._canvas.setMovie(QMovie())  # noqa: F821 — 解绑 idiom
            except RuntimeError:  # pragma: no cover
                pass
            self._movie = None
        if self._player is not None:
            try:
                self._player.stop()
            except RuntimeError:  # pragma: no cover
                pass
            try:
                self._player.setSource(QUrl())  # 解绑
            except RuntimeError:  # pragma: no cover
                pass
            self._player = None
        if self._video_widget is not None:
            self._video_widget.hide()
            # 把 _canvas 放回 layout(if it was replaced by video widget)
            outer = self.layout()
            if outer is not None and outer.indexOf(self._canvas) == -1:
                outer.insertWidget(0 if self._title_label is None else 1, self._canvas)
            self._video_widget.setParent(None)
            self._video_widget.deleteLater()
            self._video_widget = None

    def _stage_video_tmp(self, data: bytes) -> str | None:
        """MP4 bytes → 临时文件路径。closeEvent 调 `_unlink_staged_video` 清理。

        失败返 None — 调用方走 fallback。
        """
        try:
            # NamedTemporaryFile + close 才能让 Windows / Linux 都打开(WIN
            # 上 open 排他 lock,NamedTemporaryFile 默认已 close)。
            fd, path = tempfile.mkstemp(suffix=".mp4", prefix="lightbox_")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
            except Exception:  # noqa: BLE001
                os.close(fd)
                raise
            self._video_tmp_path = path
            return path
        except Exception:  # noqa: BLE001
            log.warning("Lightbox: _stage_video_tmp failed", exc_info=True)
            return None

    def _unlink_staged_video(self) -> None:
        """closeEvent 调 — unlink _video_tmp_path。失败仅 log,best-effort。"""
        if self._video_tmp_path is not None:
            try:
                os.unlink(self._video_tmp_path)
            except OSError:  # pragma: no cover — 已删 / 权限
                pass
            self._video_tmp_path = None

    # ---- 键盘事件 ----

    def keyPressEvent(self, event: QKeyEvent | None) -> None:  # noqa: N802 — Qt API
        """Esc 关闭 / 左右切上一张下一张。"""
        if event is None:
            # Qt 总传非 None event,此分支为 type-narrowing 防御
            return
        key = event.key()
        if key == Qt.Key_Escape:
            self.accept()
            return
        if len(self._all_items) > 1:
            if key == Qt.Key_Right or key == Qt.Key_Down:
                self._step_index(+1)
                return
            if key == Qt.Key_Left or key == Qt.Key_Up:
                self._step_index(-1)
                return
        super().keyPressEvent(event)

    def _step_index(self, delta: int) -> None:
        """wrap-around 切换索引:首尾连成环。"""
        n = len(self._all_items)
        self._idx = (self._idx + delta) % n
        self._zoom = 1.0  # 切图时重置缩放,体感更清晰
        self._render_current()

    # ---- 滚轮缩放 ----

    def wheelEvent(self, event: QWheelEvent | None) -> None:  # noqa: N802 — Qt API
        """滚轮缩放:image 才生效;gif 固定 100%;video 透传给 QVideoWidget。"""
        if event is None:
            return
        if self.current_kind != "image":
            # GIF / video:不缩放,让 wheel 透传给 child widget(video 音量/seek)
            super().wheelEvent(event)
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        # 取当前 pixmap(idx 在 _all_items 内,kind 是 image)
        item = self._all_items[self._idx]
        if item.pixmap is None:
            super().wheelEvent(event)
            return
        if delta > 0:
            self._zoom = min(self._zoom * self._step, self._max_zoom)
        else:
            self._zoom = max(self._zoom / self._step, self._min_zoom)
        self._apply_scaled_pixmap(item.pixmap)
        self._update_zoom_label()

    # ---- 鼠标交互 ----

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: ANN001, N802 — Qt 签名固定
        """双击关闭(单图模式体感)"""
        if event is None or event.button() != Qt.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        self.accept()

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802 — Qt API
        """右键 / 单击空白处关闭"""
        if event is None:
            # Qt 总传非 None event,此分支为 type-narrowing 防御
            return
        if event.button() == Qt.RightButton:
            self.accept()
            return
        # 左键单击:背景空白处关闭;若点中图片本身(由 QLabel 子对象),不关
        if event.button() == Qt.LeftButton and self._canvas is not None:
            pos: QPoint = event.pos()
            if not self._canvas.geometry().contains(pos):
                self.accept()
                return
        super().mousePressEvent(event)

    # ---- 关闭时清理 ----

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt API
        """2026-09-04 v1.6.7:关窗前严格 stop QMovie / QMediaPlayer + unlink tmpfile。"""
        self._stop_active_player()
        self._unlink_staged_video()
        super().closeEvent(event)


# ---- 便利构造函数 ----


def show_lightbox(
    pixmaps: Sequence[QPixmap] | None = None,
    current: int = -1,
    title: str = "",
    *,
    items: Sequence[MediaItem] | None = None,
    fallback_fn: Callable[[], None] | None = None,
) -> LightboxDialog:
    """build + showFullScreen + exec,单行调用。

    空 pixmaps/items 返回 dummy(未 show),调用方应自行 skip。
    """
    dlg = LightboxDialog(
        pixmaps=pixmaps,
        current=current,
        title=title,
        items=items,
        fallback_fn=fallback_fn,
    )
    if not (pixmaps or items):
        return dlg
    dlg.showFullScreen()
    return dlg


__all__ = ["LightboxDialog", "MediaItem", "show_lightbox"]
