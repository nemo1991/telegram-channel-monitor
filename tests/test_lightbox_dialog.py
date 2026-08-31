"""PR #A8 — LightboxDialog 单测(图片 + GIF 内嵌预览)。

QT_QPA_PLATFORM=offscreen 跑(测试环境无 GUI):
- dialog 弹/关(构造 + showFullScreen + accept)
- 单图模式 / 多图模式 / 空 list 自动 reject
- 当前索引 + 缩放比例属性
- Esc 关闭 + 左右键 wrap-around 翻页
- 滚轮缩放 clamp 到 [0.25, 8.0]
- 双击 / 右键关闭
- QPixmap 为空(null)graceful 显示占位文字
- 缩放 hint label 实时更新
- show_lightbox() 便利函数
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QColor, QKeyEvent, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from tgmonitor.ui.widgets.lightbox_dialog import LightboxDialog, show_lightbox  # noqa: E402


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    """QApplication 模块级 fixture — 整个测试模块只构造一次。"""
    app = QApplication.instance() or QApplication([])
    return app  # type: ignore[return-value]


@pytest.fixture
def sample_pixmap() -> QPixmap:
    """构造一个 100×80 纯色 pixmap(模拟小图片)— 加载逻辑不依赖图像解码。"""
    pm = QPixmap(100, 80)
    pm.fill(QColor(200, 100, 50))
    return pm


@pytest.fixture
def multi_pixmaps() -> list[QPixmap]:
    """3 张不同色小图,多图模式用。"""
    pm1 = QPixmap(100, 80)
    pm1.fill(QColor(255, 0, 0))
    pm2 = QPixmap(120, 90)
    pm2.fill(QColor(0, 255, 0))
    pm3 = QPixmap(80, 100)
    pm3.fill(QColor(0, 0, 255))
    return [pm1, pm2, pm3]


# ---- 构造 + 基本状态 ----


def test_dialog_constructs_single_image(qt_app: QApplication, sample_pixmap: QPixmap) -> None:
    """单图模式 (current=-1 默认)→ 构造后索引 0,缩放 1.0。"""
    dlg = LightboxDialog(pixmaps=[sample_pixmap])
    assert dlg.current_index == 0
    assert dlg.current_zoom == 1.0
    assert dlg._all_pixmaps == [sample_pixmap]
    # 关闭,不真弹窗
    dlg.reject()


def test_dialog_constructs_multi_image(qt_app: QApplication, multi_pixmaps: list[QPixmap]) -> None:
    """多图模式:current 指定起始索引,total 跟 len 一致。"""
    dlg = LightboxDialog(pixmaps=multi_pixmaps, current=1)
    assert dlg.current_index == 1
    assert len(dlg._all_pixmaps) == 3
    dlg.reject()


def test_dialog_empty_pixmaps_rejects(qt_app: QApplication) -> None:
    """空 pixmaps → 自动 reject(不弹窗,UI 不卡死)。"""
    dlg = LightboxDialog(pixmaps=[])
    assert dlg.result() == QDialog.Rejected


def test_dialog_constructs_with_title(qt_app: QApplication, sample_pixmap: QPixmap) -> None:
    """title 非空 → 顶部 title label 出现并显示文本。"""
    dlg = LightboxDialog(pixmaps=[sample_pixmap], title="hello.jpg")
    assert dlg._title_label is not None
    assert dlg._title_label.text() == "hello.jpg"
    dlg.reject()


def test_dialog_title_empty_omits_label(qt_app: QApplication, sample_pixmap: QPixmap) -> None:
    """title 空串 → 不创建 _title_label(轻量,占位无文字不浪费高度)。"""
    dlg = LightboxDialog(pixmaps=[sample_pixmap], title="")
    assert dlg._title_label is None
    dlg.reject()


# ---- 缩放 hint label ----


def test_zoom_label_initial_value(qt_app: QApplication, sample_pixmap: QPixmap) -> None:
    """初始缩放 1.0 → zoom label 显示 100%。"""
    dlg = LightboxDialog(pixmaps=[sample_pixmap])
    assert "100%" in dlg._zoom_label.text()
    dlg.reject()


def test_zoom_label_updates_after_zoom_step(qt_app: QApplication, sample_pixmap: QPixmap) -> None:
    """手动设 _zoom=1.25 → _update_zoom_label 后文本含 125%。"""
    dlg = LightboxDialog(pixmaps=[sample_pixmap])
    dlg._zoom = 1.25
    dlg._update_zoom_label()
    assert "125%" in dlg._zoom_label.text()
    dlg.reject()


def test_zoom_label_multi_image_shows_position(
    qt_app: QApplication, multi_pixmaps: list[QPixmap]
) -> None:
    """多图模式:zoom label 显示 `100%  2/3`(百分比 + 当前位置/总数)。"""
    dlg = LightboxDialog(pixmaps=multi_pixmaps, current=1)
    dlg._update_zoom_label()
    txt = dlg._zoom_label.text()
    assert "2/3" in txt
    dlg.reject()


# ---- 键盘事件 ----


def test_esc_closes_dialog(qt_app: QApplication, sample_pixmap: QPixmap) -> None:
    """Esc 键 → accept()(等价于"关闭并保留"语义)— result == Accepted。"""
    dlg = LightboxDialog(pixmaps=[sample_pixmap])
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    dlg.keyPressEvent(ev)
    assert dlg.result() == QDialog.Accepted


def test_right_arrow_advances_index(qt_app: QApplication, multi_pixmaps: list[QPixmap]) -> None:
    """→ 键 → 索引 +1,wrap 到首尾。"""
    dlg = LightboxDialog(pixmaps=multi_pixmaps, current=0)
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier)
    dlg.keyPressEvent(ev)
    assert dlg.current_index == 1

    # 走到末尾再 → wrap 到 0
    dlg._idx = 2
    dlg.keyPressEvent(ev)
    assert dlg.current_index == 0
    dlg.reject()


def test_down_arrow_advances_index(qt_app: QApplication, multi_pixmaps: list[QPixmap]) -> None:
    """↓ 键也是"下一张"(类 vim 习惯)— 与 → 行为一致。"""
    dlg = LightboxDialog(pixmaps=multi_pixmaps, current=0)
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
    dlg.keyPressEvent(ev)
    assert dlg.current_index == 1
    dlg.reject()


def test_left_arrow_retreats_index(qt_app: QApplication, multi_pixmaps: list[QPixmap]) -> None:
    """← 键 → 索引 -1,首尾 wrap。"""
    dlg = LightboxDialog(pixmaps=multi_pixmaps, current=1)
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier)
    dlg.keyPressEvent(ev)
    assert dlg.current_index == 0

    # 走到 0 再 ← wrap 到末尾
    dlg.keyPressEvent(ev)
    assert dlg.current_index == 2  # wrap 到 3 张图的索引 2
    dlg.reject()


def test_arrows_no_op_single_image(qt_app: QApplication, sample_pixmap: QPixmap) -> None:
    """单图模式:←/→ 键无操作(不切)— 索引恒为 0。"""
    dlg = LightboxDialog(pixmaps=[sample_pixmap])
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier)
    dlg.keyPressEvent(ev)
    assert dlg.current_index == 0
    dlg.reject()


def test_step_index_resets_zoom(qt_app: QApplication, multi_pixmaps: list[QPixmap]) -> None:
    """切图后缩放重置为 1.0(切图后体感清爽,不背老缩放)。"""
    dlg = LightboxDialog(pixmaps=multi_pixmaps, current=0)
    dlg._zoom = 3.0  # 手动放大
    dlg._step_index(+1)
    assert dlg.current_zoom == 1.0
    assert dlg.current_index == 1
    dlg.reject()


# ---- 滚轮缩放 ----


def test_wheel_zoom_in_clamps_to_max(qt_app: QApplication, sample_pixmap: QPixmap) -> None:
    """滚轮向上连点 → 放大但不超过 _max_zoom (8.0)。"""
    dlg = LightboxDialog(pixmaps=[sample_pixmap])
    for _ in range(20):  # 1.25^20 ≈ 86 倍,远超 max
        dlg._zoom = min(dlg._zoom * dlg._step, dlg._max_zoom)
    assert dlg.current_zoom == dlg._max_zoom
    dlg.reject()


def test_wheel_zoom_out_clamps_to_min(qt_app: QApplication, sample_pixmap: QPixmap) -> None:
    """滚轮向下连点 → 缩小但不低于 _min_zoom (0.25)。"""
    dlg = LightboxDialog(pixmaps=[sample_pixmap])
    for _ in range(20):  # 1/1.25^20 ≈ 0.012,远低于 min
        dlg._zoom = max(dlg._zoom / dlg._step, dlg._min_zoom)
    assert dlg.current_zoom == dlg._min_zoom
    dlg.reject()


def test_wheel_event_none_no_op(qt_app: QApplication, sample_pixmap: QPixmap) -> None:
    """wheelEvent(None) 防御分支 — 不动缩放(不抛异常)。"""
    dlg = LightboxDialog(pixmaps=[sample_pixmap])
    dlg.wheelEvent(None)
    assert dlg.current_zoom == 1.0
    dlg.reject()


# ---- 空/null pixmap graceful ----


def test_null_pixmap_shows_placeholder_text(qt_app: QApplication) -> None:
    """QPixmap.isNull() 为 True → canvas 显示占位文字,不崩溃。"""
    null_pm = QPixmap()  # 默认构造 = null
    dlg = LightboxDialog(pixmaps=[null_pm])
    assert dlg._canvas.text() == "(image unavailable)"
    # zoom label 仍应正常显示 100%
    assert "100%" in dlg._zoom_label.text()
    dlg.reject()


# ---- 便利函数 ----


def test_show_lightbox_returns_dialog(qt_app: QApplication, sample_pixmap: QPixmap) -> None:
    """show_lightbox(pixmaps) 返回构造的 dialog 实例。"""
    dlg = show_lightbox([sample_pixmap])
    assert isinstance(dlg, LightboxDialog)
    # showFullScreen 在 offscreen 环境无副作用,不检查
    dlg.reject()


def test_show_lightbox_empty_no_show(qt_app: QApplication) -> None:
    """空 pixmaps → show_lightbox 仍返 dialog(不弹窗),caller 自行 skip。"""
    dlg = show_lightbox([])
    assert isinstance(dlg, LightboxDialog)
    assert not dlg._all_pixmaps
    dlg.reject()


# ---- cleanup (QMovie) ----


def test_close_event_stops_movie_safely(qt_app: QApplication, sample_pixmap: QPixmap) -> None:
    """closeEvent 不会抛异常(即便 _movie 仍为 None)。"""
    dlg = LightboxDialog(pixmaps=[sample_pixmap])
    assert dlg._movie is None
    dlg.close()  # 触发 closeEvent
    assert dlg._movie is None  # 仍是 None
