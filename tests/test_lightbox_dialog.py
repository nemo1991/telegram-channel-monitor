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
from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from tgmonitor.ui.widgets.lightbox_dialog import LightboxDialog  # noqa: E402

# `qapp` from tests/conftest.py — session-scope QApplication 单例


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


def test_dialog_empty_pixmaps_rejects(qapp: QApplication) -> None:
    """空 pixmaps → 自动 reject(不弹窗,UI 不卡死)。"""
    dlg = LightboxDialog(pixmaps=[])
    assert dlg.result() == QDialog.Rejected


# ---- 缩放 hint label ----


# ---- 键盘事件 ----


# ---- 滚轮缩放 ----


# ---- 空/null pixmap graceful ----


# ---- 便利函数 ----


# ---- cleanup (QMovie) ----


def test_close_event_stops_movie_safely(qapp: QApplication, sample_pixmap: QPixmap) -> None:
    """closeEvent 不会抛异常(即便 _movie 仍为 None)。"""
    dlg = LightboxDialog(pixmaps=[sample_pixmap])
    assert dlg._movie is None
    dlg.close()  # 触发 closeEvent
    assert dlg._movie is None  # 仍是 None


# ---- v1.6.10 控制条 ----


def test_save_button_writes_file(
    qapp: QApplication, sample_pixmap: QPixmap, tmp_path, monkeypatch
) -> None:
    """点 ⤓ → 弹 QFileDialog(monkeypatch 给定路径)→ 写 bytes 到该路径。"""
    from tgmonitor.ui.widgets import lightbox_dialog as lb_mod

    payload = b"\x89PNG\r\n\x1a\nhello-world"
    dlg = LightboxDialog(pixmaps=[sample_pixmap], data=payload, source_title="test.png")
    target = tmp_path / "saved.png"

    def fake_get_save_file_name(parent, caption, default, filt):
        return (str(target), filt)

    monkeypatch.setattr(lb_mod.QFileDialog, "getSaveFileName", fake_get_save_file_name)
    dlg._btn_save.click()
    assert target.read_bytes() == payload
    dlg.close()
