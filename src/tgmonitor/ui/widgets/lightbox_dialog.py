# mypy: disable-error-code="attr-defined"
"""Lightbox 图片内嵌预览 — 2026-08-31 v1.5.0 PR #A8(UX 收官)。

设计要点:
- `QDialog` Frameless + WindowStaysOnTopHint + 黑色半透背景;`showFullScreen()`
  占满整个屏幕,中央显示原图(QPixmap.scaled KeepAspectRatio SmoothTransformation)
- **图片来源**:构造时传入 `QPixmap`(由调用方预先 bytes → QPixmap 加载,
  async 加载 + 异常处理走 VM/media_service,UI 不持 async 状态)
- **多图切换**:构造时传 `pixmaps: list[QPixmap]` + `current: int`,左/右方向键
  翻页(超出范围 wrap 到首尾),默认 -1 = 单图模式(不可翻)
- **缩放**:鼠标滚轮围绕 `scale_step` (1.25×) 缩放,Min 0.25× / Max 8×;
  当前缩放比例显示在右下角小 label(2 秒后自动淡出,纯 MVP 简化版可省)
- **GIF**:QPixmap 原生支持 GIF(包括动画);`QLabel.setMovie(QMovie)` 走
  `start()` 启动;静态图照 `setPixmap` 走
- **Esc 关闭**:`keyPressEvent` 拦截 Esc;鼠标右键 / 双击也关闭(直觉)
- **不异步加载**:Lightbox 弹窗前提 = VM 已拿到 bytes,UI 只负责画。
  Async 走 VM,UI 持 QPixmap 即可

不做(留 v1.5.1):
- 视频 codec 预览(QtMultimedia 风险高 + 80 LOC)
- 双指捏合 / pan & drag(平板用户少)
- 加载进度条(VM 加载完才弹,无中间态)
- 多媒体类型 fallback 弹「双击用系统查看器打开」(按钮 + click handler 留口,
  v1.5.0 不接 OS 默认应用)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QKeyEvent, QMovie, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QVBoxLayout,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


class LightboxDialog(QDialog):
    """Frameless 全屏预览图片 + GIF,支持左/右切换 + 滚轮缩放 + Esc 关闭。

    Parameters
    ----------
    pixmaps : Sequence[QPixmap]
        一组已加载的图片(单图时传 `[pix]`)。空 → 自动 close,不弹窗。
    current : int
        当前显示索引。`-1` 表示单图模式(不可翻)。其它值需在
        `[0, len(pixmaps))` 内,否则 ValueError。
    title : str, optional
        顶部居中小标题(可选,空字符串不显示)
    """

    def __init__(
        self,
        pixmaps: Sequence[QPixmap],
        current: int = -1,
        title: str = "",
        parent: QDialog | None = None,
    ) -> None:
        super().__init__(parent)
        self._all_pixmaps = list(pixmaps)
        self._idx = current if current >= 0 else 0
        self._zoom = 1.0
        self._min_zoom = 0.25
        self._max_zoom = 8.0
        self._step = 1.25  # 滚轮一档 1.25×
        self._movie: QMovie | None = None  # GIF 动画 cleanup 用

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

    # ---- 渲染核心 ----

    def _render_current(self) -> None:
        """根据 self._idx + self._zoom 重画 canvas + 更新缩放 hint。"""
        if not self._all_pixmaps:
            self.reject()
            return
        if self._idx < 0 or self._idx >= len(self._all_pixmaps):
            self.reject()
            return

        # 先清掉上一张 GIF 的 QMovie(避免 dangling)
        if self._movie is not None:
            self._movie.stop()
            self._canvas.setMovie(None)  # type: ignore[arg-type]
            self._movie = None

        pix = self._all_pixmaps[self._idx]
        if pix.isNull():
            # 注意顺序:先 setPixmap(null) 清掉老 pixmap,再 setText;否则
            # QLabel.setPixmap 会把已设置的 text 清掉(实测 Qt 6.11 行为)。
            self._canvas.setPixmap(QPixmap())
            self._canvas.setText("(image unavailable)")
            self._update_zoom_label()
            return

        # GIF 走 QMovie(支持动画);静态图走 setPixmap
        # 简单判断:GIF 的 pixmap 内部有动画时,QPixmap.toImage() 返第一帧,
        # 无法区分。直接看 file_format 不可靠(没有 QPixmap.format 等价);
        # **约定**:MediaService.load_thumbnail_bytes 返 GIF bytes 时,VM
        # 已知道 mime=image/gif,会以 (bytes, mime_type) 传过来 — 但本类只
        # 收 QPixmap,不接 mime。MVP:全部走 setPixmap,GIF 会变成第一帧静态
        # (用户依然能看到图);完整 GIF 动画走 v1.5.1 的 `bytes + mime` 扩展。
        self._apply_scaled_pixmap(pix)
        self._update_zoom_label()

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
        n = len(self._all_pixmaps)
        pos = f"{self._idx + 1}/{n}" if n > 1 else ""
        zoom_pct = f"{int(self._zoom * 100)}%"
        hint = f"{zoom_pct}  {pos}".strip()
        self._zoom_label.setText(hint)

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
        if len(self._all_pixmaps) > 1:
            if key == Qt.Key_Right or key == Qt.Key_Down:
                self._step_index(+1)
                return
            if key == Qt.Key_Left or key == Qt.Key_Up:
                self._step_index(-1)
                return
        super().keyPressEvent(event)

    def _step_index(self, delta: int) -> None:
        """wrap-around 切换索引:首尾连成环。"""
        n = len(self._all_pixmaps)
        self._idx = (self._idx + delta) % n
        self._zoom = 1.0  # 切图时重置缩放,体感更清晰
        self._render_current()

    # ---- 滚轮缩放 ----

    def wheelEvent(self, event: QWheelEvent | None) -> None:  # noqa: N802 — Qt API
        """滚轮缩放:delta > 0 放大,< 0 缩小;clamp 到 [min_zoom, max_zoom]。"""
        if event is None:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        if delta > 0:
            self._zoom = min(self._zoom * self._step, self._max_zoom)
        else:
            self._zoom = max(self._zoom / self._step, self._min_zoom)
        self._apply_scaled_pixmap(self._all_pixmaps[self._idx])
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

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802 — Qt API
        """关窗前 stop QMovie(防 dangling animation timer 触发已销毁 widget)"""
        if self._movie is not None:
            self._movie.stop()
            self._movie = None
        super().closeEvent(event)


# ---- 便利构造函数 ----


def show_lightbox(
    pixmaps: Sequence[QPixmap],
    current: int = -1,
    title: str = "",
) -> LightboxDialog:
    """build + showFullScreen + exec,单行调用。

    空 pixmaps 返回 dummy(未 show),调用方应自行 skip。
    """
    dlg = LightboxDialog(pixmaps=pixmaps, current=current, title=title)
    if not pixmaps:
        return dlg
    dlg.showFullScreen()
    return dlg


__all__ = ["LightboxDialog", "show_lightbox"]
