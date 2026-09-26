"""MainWindow Media Retried / Downloaded slot — 反馈可见性回归。

2026-09-26 用户反馈:Media Manager 点 Retry「无反应」。
根因:`AppService.retry_media` 发 `MediaRetried` + `MediaDownloaded` 两个事件,
但 MainWindow 没订阅 `media_retried`,`media_downloaded` 也只刷 LIVE view 不刷
Media Manager widget 列表 — 用户视觉上无变化(row 仍 FAILED)。
本测试守住两条边界:
  1. `_on_media_retried` 必须:更新活动指示器 + 触发 widget refresh
  2. `_on_media_downloaded` 必须:除现有 LIVE view 刷新外,额外触发 widget refresh

策略:用真实 `MediaRetried` / `MediaDownloaded` dataclass(slot 内 isinstance
走真类型),MediaDTO 走真 dataclass(`MediaDownloadStatus` 字段直接控制)。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QLabel  # noqa: E402

from tgmonitor.core.dto import MediaDownloadStatus, MediaDTO, MediaType  # noqa: E402
from tgmonitor.core.events import MediaDownloaded, MediaRetried  # noqa: E402
from tgmonitor.ui.main_window import MainWindow  # noqa: E402


class _FakeSignal:
    """可连接的伪 Qt Signal — 简单 callable 列表,够用 + 不依赖 QObject metaclass。

    真 PySide6 Signal 是 class-level descriptor 需要 QObject 元类初始化,
    在测试 stub 里太重,这里只测「slot 是否调到了 `signal.emit()`」。
    """

    def __init__(self) -> None:
        self._callbacks: list = []

    def connect(self, cb) -> None:
        self._callbacks.append(cb)

    def emit(self) -> None:
        for cb in list(self._callbacks):
            cb()


class _FakeMediaManager:
    """最小 MediaManager 桩 — 只暴露 `refresh_requested` 触发计数。"""

    def __init__(self) -> None:
        self.refresh_requested = _FakeSignal()
        self.refresh_count = 0
        self.refresh_requested.connect(self._on_refresh)

    def _on_refresh(self) -> None:
        self.refresh_count += 1


class _FakeLiveView:
    """LiveView 桩 — `_on_media_downloaded` 会调 `update_media_status`,留空。"""

    def update_media_status(self, *_args, **_kw) -> None:
        return


class _FakeDetailPanel:
    """message_detail 桩 — 同上。"""

    def refresh_if_showing(self, *_args, **_kw) -> None:
        return


class _FakeWindow:
    """最小 MainWindow 桩 — 覆盖 retry feedback 两个 slot 需要的依赖。"""

    def __init__(self) -> None:
        self._activity_label = QLabel("")
        self._activity_throttle: dict[str, float] = {}
        self.media_manager = _FakeMediaManager()
        self.live_view = _FakeLiveView()
        self.message_detail = _FakeDetailPanel()

    def tr(self, text: str) -> str:
        """slot 内 `self.tr("…")` 调用 — fake 不挂 QObject,走 identity。"""
        return text

    # 绑 MainWindow 上的 unbound method,slot 内会调到
    _show_activity = MainWindow._show_activity


def _make_media(file_name: str, *, is_done: bool) -> MediaDTO:
    """构造真实 MediaDTO;status 用 `is_done` 切 DONE / FAILED。"""
    return MediaDTO(
        type=MediaType.PHOTO,
        mime_type="image/jpeg",
        file_name=file_name,
        file_size=1024,
        download_status=MediaDownloadStatus.DONE if is_done else MediaDownloadStatus.FAILED,
    )


# ============================================================
# `_on_media_retried` 行为
# ============================================================


def test_on_media_retried_triggers_widget_refresh(qapp) -> None:
    """`_on_media_retried` 必须 emit `media_manager.refresh_requested`。

    否则 widget 列表不变,用户看不到 retry 已触发 — 这是用户报告的
    「点击 Retry 无反应」的根因。
    """
    win = _FakeWindow()
    MainWindow._on_media_retried(win, MediaRetried(channel_id=100, telegram_msg_id=5, media_idx=0))
    assert win.media_manager.refresh_count == 1


def test_on_media_retried_shows_activity_toast(qapp) -> None:
    """`_on_media_retried` 必须更新活动指示器,给用户视觉反馈。

    之前完全没提示,现在 status bar 左侧立刻显示「正在重试…」2 秒后清空。
    """
    win = _FakeWindow()
    assert win._activity_label.text() == ""  # 初始空
    MainWindow._on_media_retried(win, MediaRetried())
    assert "正在重试" in win._activity_label.text()


# ============================================================
# `_on_media_downloaded` 行为
# ============================================================


def test_on_media_downloaded_triggers_widget_refresh_on_success(qapp) -> None:
    """`_on_media_downloaded`(成功路径)完成后必须 emit widget refresh。

    retry 完成后 Media Manager row 应从 PENDING 切到 DONE — 没有 refresh
    就一直停在 PENDING / 旧字节进度文字。
    """
    win = _FakeWindow()
    ev = MediaDownloaded(
        channel_id=100,
        telegram_msg_id=5,
        media=_make_media("photo.jpg", is_done=True),
    )
    MainWindow._on_media_downloaded(win, ev)
    assert win.media_manager.refresh_count == 1


def test_on_media_downloaded_triggers_widget_refresh_on_failure(qapp) -> None:
    """失败路径(retry 重试后仍 FAILED)同样要刷新 widget — UI 反映真实状态。"""
    win = _FakeWindow()
    ev = MediaDownloaded(
        channel_id=100,
        telegram_msg_id=5,
        media=_make_media("photo.jpg", is_done=False),
    )
    MainWindow._on_media_downloaded(win, ev)
    assert win.media_manager.refresh_count == 1


def test_on_media_downloaded_shows_activity_on_success(qapp) -> None:
    """成功下载 → activity 显示「已下载: <name>」。"""
    win = _FakeWindow()
    ev = MediaDownloaded(media=_make_media("photo.jpg", is_done=True))
    MainWindow._on_media_downloaded(win, ev)
    assert "已下载" in win._activity_label.text()
    assert "photo.jpg" in win._activity_label.text()


def test_on_media_downloaded_shows_warning_on_failure(qapp) -> None:
    """下载失败 → activity 显示「⚠ 下载失败: <name>」。"""
    win = _FakeWindow()
    ev = MediaDownloaded(media=_make_media("photo.jpg", is_done=False))
    MainWindow._on_media_downloaded(win, ev)
    assert "下载失败" in win._activity_label.text()
    assert "photo.jpg" in win._activity_label.text()


def test_on_media_downloaded_with_none_media_skips_refresh(qapp) -> None:
    """`_on_media_downloaded` 收到 `e.media is None` → 直接 return,不抛、不刷 widget。

    守住现有契约。
    """
    win = _FakeWindow()
    ev = MediaDownloaded(media=None)
    MainWindow._on_media_downloaded(win, ev)
    assert win.media_manager.refresh_count == 0
