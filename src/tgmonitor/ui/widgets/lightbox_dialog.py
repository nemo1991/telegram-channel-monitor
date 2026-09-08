# mypy: disable-error-code="attr-defined"
"""Lightbox 图片 / GIF / 视频内嵌预览 — 2026-08-31 v1.5.0 PR #A8 + 2026-09-04 v1.6.7 + 2026-09-08 v1.6.10。

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

v1.6.10 控制条:
- 底部贴边 QFrame 工具栏,7 个按钮(上一张 / 下一张 / 缩小 / 放大 / 旋转 /
  另存为 / 关闭)
- 默认隐藏,`mouseMoveEvent` 触发淡入 + 2s QTimer 静默淡出
- 旋转(image / GIF)+ 90°累加,`QPixmap.transformed(QTransform().rotate(_rotation))`,
  GIF 旋转变静态(QMovie 不支持 transform)— 已知妥协
- 另存为:`QFileDialog.getSaveFileName` 弹保存对话框,caller 传 `data`
  时按钮 enabled,默认文件名 `source_title` 或 `lightbox_<idx>.<ext>`
- 启灰规则按 `_update_button_states`:kind==image 才允许 zoom,kind==
  image/gif 才允许 rotate,`data` 非空才允许 save
- 单图 / 单项 mode 也走同一套 UI(button 仅 prev/next 自动 disabled)
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QCloseEvent,
    QKeyEvent,
    QMovie,
    QPixmap,
    QTransform,
    QWheelEvent,
)
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


# v1.6.10:mime → 文件扩展名映射,save-as QFileDialog 默认文件名 + filter 用
_MIME_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
}


def _ext_for_mime(mime: str) -> str:
    """mime → 扩展名(含 `.`)。未知 mime  → `.bin`(避免无扩展)。"""
    return _MIME_EXT.get(mime.lower(), ".bin")


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
        data: bytes | list[bytes] | None = None,  # v1.6.10:save-as 用 bytes
        source_title: str | None = None,  # v1.6.10:QFileDialog 默认文件名
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
        self._rotation = 0  # v1.6.10:旋转 90° 累加;切图 / 切 GIF 时归零
        self._min_zoom = 0.25
        self._max_zoom = 8.0
        self._step = 1.25  # 滚轮一档 1.25×
        self._movie: QMovie | None = None  # GIF 动画 cleanup 用
        # 2026-09-04 v1.6.7:MP4 媒体播放状态
        self._player: QMediaPlayer | None = None
        self._video_widget: QVideoWidget | None = None
        self._video_tmp_path: str | None = None  # stage 的 tmpfile 路径,closeEvent unlink
        self._fallback_fn = fallback_fn
        # v1.6.10:save-as 用 — bytes(单图) / list[bytes](多图) / None(不可保存)
        self._data: bytes | list[bytes] | None = data
        self._current_data: bytes | None = (  # 当前 idx 的 bytes,_render_current 同步
            data if isinstance(data, bytes) else None
        )
        self._source_title = source_title

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

        # 底部工具栏(v1.6.10)— 默认隐藏,鼠标移动触发 _show_bar
        self._build_control_bar()

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

        # v1.6.10:同步当前 idx 对应的 bytes(save-as 用)
        if isinstance(self._data, list):
            if 0 <= self._idx < len(self._data):
                self._current_data = self._data[self._idx]
            else:
                self._current_data = None
        elif isinstance(self._data, bytes):
            self._current_data = self._data
        else:
            self._current_data = None

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
            # 2026-09-07 v1.6.8:英文 fallback 文本走 tr()。
            self._canvas.setText(self.tr("(image unavailable)"))
            self._update_zoom_label()

        # v1.6.10:按当前 kind / data 启灰工具栏按钮
        self._update_button_states()

    def _render_image(self, pix: QPixmap) -> None:
        """静态图路径 — 与 v1.5.0 PR #A8 完全一致。"""
        if pix.isNull():
            # 注意顺序:先 setPixmap(null) 清掉老 pixmap,再 setText;否则
            # QLabel.setPixmap 会把已设置的 text 清掉(实测 Qt 6.11 行为)。
            self._canvas.setPixmap(QPixmap())
            # 2026-09-07 v1.6.8:fallback 文本走 tr()。
            self._canvas.setText(self.tr("(image unavailable)"))
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
        # 2026-09-07 v1.6.8:fallback 文本走 tr()。
        self._canvas.setText(self.tr("(image unavailable)"))

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
        # 2026-09-07 v1.6.8:fallback 文本走 tr()。
        self._canvas.setText(self.tr("(video unavailable — codec missing)"))

    def _apply_scaled_pixmap(self, pix: QPixmap) -> None:
        """按 self._zoom 缩放 + 居中显示;屏幕尺寸 = 当前主屏 90%。

v1.6.10:先按 `self._rotation` 旋转,再缩放。顺序很重要 — 旋转 90°
后 width/height 互换,缩放按原 width 算导致新图比例失调;先旋转得到
正确几何后再缩。
"""
        if self._rotation:
            pix = pix.transformed(QTransform().rotate(self._rotation))
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
        self._rotation = 0  # v1.6.10:切图时旋转归零
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

    # ---- v1.6.10 控制条 ----

    def _build_control_bar(self) -> None:
        """底部贴边工具栏 — 7 个按钮(上一张/下一张/缩小/放大/旋转/另存为/关闭)。

默认隐藏,`mouseMoveEvent` 触发 `_show_bar` 淡入 + 重启 2s QTimer
静默淡出。视觉风格延续 `_zoom_label` 的半透黑底白字(避免引入
stylesheet 二套体系)。
"""
        self._control_bar = QFrame(self)
        self._control_bar.setStyleSheet(
            "QFrame { background-color: rgba(0, 0, 0, 180); border-radius: 6px; }"
            "QPushButton { color: white; background: transparent; border: none;"
            " padding: 6px 12px; font-size: 16px; }"
            "QPushButton:hover { background-color: rgba(255, 255, 255, 30); }"
            "QPushButton:disabled { color: rgba(255, 255, 255, 80); }"
        )
        bar = QHBoxLayout(self._control_bar)
        bar.setContentsMargins(12, 6, 12, 6)
        bar.setSpacing(4)

        self._btn_prev = self._make_btn("‹", self.tr("上一张"))
        self._btn_next = self._make_btn("›", self.tr("下一张"))
        self._btn_zoom_out = self._make_btn("−", self.tr("缩小"))
        self._btn_zoom_in = self._make_btn("＋", self.tr("放大"))
        self._btn_rotate = self._make_btn("⟳", self.tr("旋转 90°"))
        self._btn_save = self._make_btn("⤓", self.tr("另存为…"))
        self._btn_close = self._make_btn("✕", self.tr("关闭"))

        bar.addWidget(self._btn_prev)
        bar.addWidget(self._btn_next)
        bar.addSpacing(16)
        bar.addWidget(self._btn_zoom_out)
        bar.addWidget(self._btn_zoom_in)
        bar.addSpacing(16)
        bar.addWidget(self._btn_rotate)
        bar.addWidget(self._btn_save)
        bar.addWidget(self._btn_close)

        self._btn_prev.clicked.connect(lambda: self._step_index(-1))
        self._btn_next.clicked.connect(lambda: self._step_index(+1))
        self._btn_zoom_out.clicked.connect(self._zoom_out)
        self._btn_zoom_in.clicked.connect(self._zoom_in)
        self._btn_rotate.clicked.connect(self._rotate_90)
        self._btn_save.clicked.connect(self._save_current)
        self._btn_close.clicked.connect(self.accept)

        # 底部居中 — 用独立 QHBoxLayout 加一层 stretch wrapper 比较啰嗦,
        # 直接 fixed bottom + adjustSize + 在 resizeEvent 里 move 居中
        self._control_bar.hide()
        # QFrame 在父 dialog 内不是独立 window → setWindowOpacity 无效。
        # 用 QGraphicsOpacityEffect 走 widget 的 graphics effect 通道。
        self._bar_opacity_effect = QGraphicsOpacityEffect(self._control_bar)
        self._bar_opacity_effect.setOpacity(0.0)
        self._control_bar.setGraphicsEffect(self._bar_opacity_effect)
        self._control_bar.adjustSize()

        # 把 control_bar 加进 outer layout,align bottom-center。stretch=0
        # 不抢画布空间;_canvas(stretch=1)撑满中间区域
        outer = self.layout()
        assert outer is not None
        outer.addWidget(self._control_bar)
        outer.setAlignment(self._control_bar, Qt.AlignBottom | Qt.AlignHCenter)

        # 2s 不动 → 淡出
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(2000)
        self._hide_timer.timeout.connect(self._fade_out_bar)

    def _make_btn(self, text: str, tooltip: str) -> QPushButton:
        """单按钮工厂 — NoFocus 防按钮抢键盘焦点,左右方向键继续走 keyPressEvent 翻页。"""
        btn = QPushButton(text, self._control_bar)
        btn.setToolTip(tooltip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.setFixedHeight(32)
        return btn

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001, N802 — Qt API
        """v1.6.10:鼠标移动 → 控制条淡入 + 重启 2s 静默淡出 timer。

不抢 `mousePressEvent` 的关闭语义 — 关闭仍由 mousePress 处理。
"""
        if event is not None and self._control_bar is not None:
            self._show_bar()
        super().mouseMoveEvent(event)

    def _show_bar(self) -> None:
        """鼠标动 → bar 渐显 + 重启 2s timer。"""
        if self._control_bar is None:
            return
        self._control_bar.show()
        self._bar_opacity_effect.setOpacity(1.0)
        self._hide_timer.start()

    def _fade_out_bar(self) -> None:
        """2s 不动 → bar 渐隐。

        用 QGraphicsOpacityEffect 而不是 setWindowOpacity,因为 QFrame
        在父 dialog 内不是独立 window,setWindowOpacity 不生效。
        QGraphicsOpacityEffect 走 widget 的 graphics effect 通道,所有
        平台统一支持。
        """
        if self._control_bar is None:
            return
        self._bar_opacity_effect.setOpacity(0.0)

    def _zoom_in(self) -> None:
        """点 + 按钮放大 1.25×,clamp 到 _max_zoom。仅 image 生效。"""
        if self.current_kind != "image":
            return
        item = self._all_items[self._idx]
        if item.pixmap is None:
            return
        self._zoom = min(self._zoom * self._step, self._max_zoom)
        self._apply_scaled_pixmap(item.pixmap)
        self._update_zoom_label()

    def _zoom_out(self) -> None:
        """点 − 按钮缩小 1/1.25×,clamp 到 _min_zoom。仅 image 生效。"""
        if self.current_kind != "image":
            return
        item = self._all_items[self._idx]
        if item.pixmap is None:
            return
        self._zoom = max(self._zoom / self._step, self._min_zoom)
        self._apply_scaled_pixmap(item.pixmap)
        self._update_zoom_label()

    def _rotate_90(self) -> None:
        """点 ⟳ 顺时针 90°。多次累加 `_rotation %= 360`。

仅 image / GIF 生效;GIF 旋转变静态(QMovie 不支持 transformed)
— 已知妥协,与原 v1.6.7 行为一致(GIF 锁 100% 不强求 360° 动画旋转)。
"""
        self._rotation = (self._rotation + 90) % 360
        item = self._all_items[self._idx]
        if item.kind == "image" and item.pixmap is not None:
            self._apply_scaled_pixmap(item.pixmap)
        elif item.kind == "gif" and item.animated:
            # GIF 走第一帧 + 旋转 — 静态显示
            ba = QByteArray(item.animated)
            pix = QPixmap()
            if pix.loadFromData(ba, b"GIF"):
                self._apply_scaled_pixmap(pix)

    def _save_current(self) -> None:
        """点 ⤓ 弹 QFileDialog.getSaveFileName,写 `self._current_data` 到选定路径。

文件名默认:`source_title` 或 `lightbox_<idx>.<ext>`(ext 由
`_ext_for_mime(item.mime_type)` 推断:image/jpeg → .jpg, image/png
→ .png, image/gif → .gif, video/mp4 → .mp4, 其它 → .bin)。

caller 没传 `data` → 按钮 disabled,本方法不应被调到(防御性 early-return)。
"""
        if not self._current_data:
            return
        item = self._all_items[self._idx]
        ext = _ext_for_mime(item.mime_type)
        default_name = self._source_title or f"lightbox_{self._idx + 1}{ext}"
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("另存为…"),
            default_name,
            self.tr("媒体文件 (*.{ext});;所有文件 (*)").format(ext=ext.lstrip(".")),
        )
        if not path:
            return
        try:
            Path(path).write_bytes(self._current_data)
        except OSError as exc:
            QMessageBox.warning(
                self,
                self.tr("保存失败"),
                self.tr("无法写入 {path}: {err}").format(path=path, err=exc),
            )

    def _update_button_states(self) -> None:
        """按当前 kind / data 启灰工具栏按钮。"""
        if not hasattr(self, "_btn_prev"):
            # _build_control_bar 未调 — 老测试无控制条场景,跳过
            return
        kind = self.current_kind
        n = len(self._all_items)
        self._btn_prev.setEnabled(n > 1)
        self._btn_next.setEnabled(n > 1)
        self._btn_zoom_in.setEnabled(kind == "image")
        self._btn_zoom_out.setEnabled(kind == "image")
        self._btn_rotate.setEnabled(kind in ("image", "gif"))
        self._btn_save.setEnabled(
            self._current_data is not None and len(self._current_data) > 0
        )
        self._btn_close.setEnabled(True)

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
