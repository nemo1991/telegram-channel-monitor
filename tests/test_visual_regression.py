"""Qt offscreen visual regression — 关键 widget 的 golden image 比对。

# 设计

- 用 `QT_QPA_PLATFORM=offscreen` 强制 offscreen 渲染(无 X server 也跑)
- 每个 widget 调 `.grab()` → `QPixmap` → `.toImage()` → `QImage`
- 存为 PNG 到 `tests/golden/<name>.png`
- 测试时:重新渲染 → 跟 golden 逐像素比对(纯 `QImage.pixelColor`,无 PIL 依赖)
- **容差**:`tolerance=0.001`(= 0.1% 像素差异),留 anti-aliasing / sub-pixel 抖动余地
- **更新流程**:`UPDATE_GOLDENS=1 pytest tests/test_visual_regression.py`
  重新生成 golden(故意 UI 改动后用)

# 覆盖 widget

只测**空状态 / 简单初始化** — 不挂真实事件总线 / 真实 Telegram 数据,
保证测试稳定跨平台:
  - MessageView(空,带「暂无消息」overlay)
  - MessageView(3 条 mock MessageDTO)
  - ChannelWidget(3 条 mock ChannelDTO)
  - SettingsPage(7 个分组,`app.settings` 默认值)
  - DashboardWidget(空 KPI 卡 + 快速操作 + 空时间线)
  - ExportDialog(默认文件名 + 4 种格式 radio)
  - LoginDialog(初始 `phone_required` 状态)
  - MainWindow(初始 dashboard 视图 — 已订 0 条 / 已加入 0 条 / 空消息区)

# 已知局限

- Golden 在**生成机器**上跑 100% 一致;换 macOS / Linux / 不同 Qt 版本
  会因字体 hinting / anti-aliasing 不同 → 失败。CI 需固定 OS + Qt 版本,
  或在 goldens 上加 platform 标记(本轮不展开)
- offscreen 渲染丢 DPI 高分屏,真机 1.25x/1.5x 缩放下可能轻微偏移

# 字体钉死(2026-08-05)

GitHub `macos-latest` 从 Intel 镜像滚到 `macos-26-arm64` 后,CI 与真机对
Qt 默认字体 "Sans Serif" 的解析不同 → 相同 OS/arch/Qt 下 widget sizeHint
与字形 metrics 漂移(8/8 golden 全挂:尺寸差几 px / 像素差异 1–18%)。
修复:fixture 里加载仓库内置的 DejaVu Sans(自由可再分发,见
`tests/fonts/LICENSE-DejaVu.txt`),并 `app.setFont` 固定 pixelSize——
同一个字体文件 + 固定像素尺寸,metrics 完全由字体二进制决定,与系统
字体数据库 / DPI 无关,本地与 CI 字节级一致。
"""
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from tgmonitor.core.dto import ChannelDTO, MessageDTO
from tgmonitor.ui.main_window import MainWindow
from tgmonitor.ui.widgets.channel_widget import ChannelWidget
from tgmonitor.ui.widgets.dashboard_widget import DashboardWidget
from tgmonitor.ui.widgets.export_dialog import ExportDialog
from tgmonitor.ui.widgets.login_dialog import LoginDialog
from tgmonitor.ui.widgets.message_view import MessageView
from tgmonitor.ui.widgets.settings_page import SettingsPage

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="visual goldens are macOS-specific",
)

GOLDEN_DIR = Path(__file__).parent / "golden"
FONT_DIR = Path(__file__).parent / "fonts"
TOLERANCE = 0.005  # 0.5% 像素差异上限
# 为什么 0.5% 而不是 0.1%:
#   - 字体钉死后(2026-08-05)文本 / 布局 / 颜色差异 < 0.1%,稳定。
#   - 但 widget 里出现 emoji 时(🕐 / 💬 / 📋),emoji 走的是 **系统 emoji
#     font**(不是我们 pin 的 DejaVu Sans)— macOS 真机 / `macos-26-arm64`
#     CI VM 的 emoji 字体版本不同,同一字符的 anti-aliasing 边缘像素会
#     漂移 0.3–0.5%(每个 emoji glyph ~10×10 px,3 个 🕐 ≈ 0.4% 像素)。
#   - 0.5% 容差对 emoji glyph 漂移放行,但文本布局 / 颜色 / 边框的差异
#     仍会被抓到(那些的 diff 是百分比级,不会因为容差放宽而漏掉)。


@pytest.fixture(scope="session")
def qapp():
    """Qt offscreen QApplication 单例 — 跟其他 UI 测试同款 pattern。

    额外做两件事,让 golden 渲染在生成机与 CI 上字节级一致:
    1. 加载仓库内置的 DejaVu Sans(不用系统默认 "Sans Serif")— CI VM
       与真机对 "Sans Serif" 的解析不同,是 golden 跨机漂移的根因
    2. `app.setFont` 用固定 **pixelSize**(不用 pointSize)— pixelSize 忽略
       DPI,尺寸完全由字体二进制决定,本地 / CI 一致
    """
    from PySide6.QtGui import QFont, QFontDatabase

    app = QApplication.instance() or QApplication([])
    font_path = str(FONT_DIR / "DejaVuSans.ttf")
    fid = QFontDatabase.addApplicationFont(font_path)
    assert fid != -1, f"bundled font failed to load: {font_path}"
    families = QFontDatabase.applicationFontFamilies(fid)
    assert families, f"bundled font registered no families: {font_path}"
    f = QFont(families[0])
    f.setPixelSize(12)
    app.setFont(f)
    yield app


def _update_mode() -> bool:
    """`UPDATE_GOLDENS=1` 时重新生成 golden 并 skip 比对(首跑 / 故意 UI 改动)。"""
    return os.environ.get("UPDATE_GOLDENS") == "1"


def _grab(widget) -> QImage:
    """widget 渲染到 QImage(固定 240×320,跟实际 UI 列表 cell 大致一致)。

    `processEvents()` 让 `widget.resize()` 触发的 layout pass / paint event
    跑完(`SettingsPage` 是 QScrollArea,resize 后不 pump,grab 会拿到未布局
    的 widget)。单次 processEvents 与 closeEvent 之前 busy-poll 完全不同
    (后者是 `while not fut.done(): processEvents()` 高频循环,触发
    macOS-26 VM Cocoa native race)。
    """
    widget.resize(QSize(240, 320))
    # 让 layout / paint event 跑完
    QApplication.processEvents()
    return widget.grab().toImage()


def _compare(name: str, current: QImage) -> None:
    """current vs golden/<name>.png — 不一致时 fail + 存 diff 图。

    UPDATE_GOLDENS=1:直接覆盖 golden + skip。
    首跑(无 golden):生成 + skip(再跑一次才真比对)。
    """
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    golden_path = GOLDEN_DIR / f"{name}.png"

    if _update_mode():
        current.save(str(golden_path), "PNG")
        pytest.skip(f"{name} — UPDATE_GOLDENS=1,golden 重新生成")

    if not golden_path.exists():
        current.save(str(golden_path), "PNG")
        pytest.skip(f"{name} — 首跑,golden 已生成,再跑一次")

    expected = QImage(str(golden_path))
    if expected.size() != current.size():
        diff_dir = GOLDEN_DIR / "_diffs"
        diff_dir.mkdir(parents=True, exist_ok=True)
        current.save(str(diff_dir / f"{name}_size_mismatch.png"), "PNG")
        pytest.fail(
            f"{name}: size mismatch {current.size().width()}×{current.size().height()}"
            f" vs golden {expected.size().width()}×{expected.size().height()}",
        )

    # 逐像素比对(纯 QImage,无 PIL 依赖)
    total = current.width() * current.height()
    diff_count = 0
    cur_fmt = current.convertToFormat(QImage.Format_RGB32)
    exp_fmt = expected.convertToFormat(QImage.Format_RGB32)
    for y in range(cur_fmt.height()):
        for x in range(cur_fmt.width()):
            if cur_fmt.pixel(x, y) != exp_fmt.pixel(x, y):
                diff_count += 1

    pct = diff_count / total
    if pct > TOLERANCE:
        diff_dir = GOLDEN_DIR / "_diffs"
        diff_dir.mkdir(parents=True, exist_ok=True)
        current.save(str(diff_dir / f"{name}_current.png"), "PNG")
        # 也存 expected 副本方便 diff 查看
        expected.save(str(diff_dir / f"{name}_expected.png"), "PNG")
        pytest.fail(f"{name}: {pct:.4%} 像素差异 > {TOLERANCE:.4%}")


# ============================================================
# Widget 测试用例
# ============================================================

def test_message_view_empty(qapp):
    """空 MessageView(首启 / 没订阅 / 还没消息)— 居中显示「暂无消息」overlay。"""
    view = MessageView()
    img = _grab(view)
    _compare("message_view_empty", img)


def test_message_view_with_messages(qapp):
    """3 条 mock MessageDTO — 验证 _format 行格式 + 去重表 + row index。"""
    view = MessageView()
    base = datetime(2026, 8, 3, 14, 23, 10, tzinfo=UTC)
    for i in range(3):
        view.append(MessageDTO(
            id=i,
            channel_id=1001,
            telegram_msg_id=900 + i,
            date=base.replace(minute=base.minute + i),
            text=f"测试消息 #{i} — 来自 fixture",
            author=f"author_{i}",
            media=[],
        ))
    img = _grab(view)
    _compare("message_view_with_messages", img)


def test_channel_widget_with_channels(qapp):
    """3 条 mock ChannelDTO(已订 + 已加入)— 两张卡片同时显数据。

    `ChannelWidget.__init__` 要 `app: AppService` + `loop`;本测试只验
    视觉布局,不接 EventBus / Telegram,用 `Mock()` 占位即可。
    """
    from unittest.mock import Mock
    mock_app = Mock()
    mock_loop = Mock()
    widget = ChannelWidget(app=mock_app, loop=mock_loop)
    channels = [
        ChannelDTO(id=cid, title=f"频道 {cid}", username=f"ch{cid}")
        for cid in (1001, 1002, 1003)
    ]
    widget.set_joined(channels)
    widget.set_subscribed(channels)
    img = _grab(widget)
    _compare("channel_widget_with_channels", img)


def test_settings_page_default(qapp, tmp_path):
    """SettingsPage 默认加载(`app.settings` 字段进表单)— 7 个分组 + 底部按钮。

    `app` 是 Mock(只读 `.settings` 字段);`env_path` 用 tmp_path(platform-native
    真路径,避免 OS 差异下 golden 不稳)。
    """
    from unittest.mock import Mock

    from tgmonitor.core.config import (
        DBBackend,
        MediaPolicy,
        ObjectStoreBackend,
        Settings,
    )
    mock_app = Mock()
    mock_loop = Mock()
    mock_app.settings = Settings(
        api_id=1, api_hash="a" * 32, phone="+1",
        session_dir=tmp_path / "session",
        db_backend=DBBackend.JSONL, db_dsn="", db_root=tmp_path / "m",
        objectstore_backend=ObjectStoreBackend.FOLDER,
        objectstore_root=tmp_path / "media",
        media_policy=MediaPolicy.METADATA, data_root=tmp_path,
    )
    widget = SettingsPage(app=mock_app, loop=mock_loop, env_path=tmp_path / ".env")
    # 240 宽太窄,SettingsPage 是 scroll area,设 480 看主表单
    widget.resize(QSize(480, 320))
    QApplication.processEvents()
    img = widget.grab().toImage()
    _compare("settings_page_default", img)


def test_dashboard_widget_empty(qapp):
    """DashboardWidget 空状态 — 4 张 KPI 卡 + 快速操作 + 空时间线 + 空频道表。

    `loop=None` 是测试路径(`__init__` docstring 明确);`app` + `monitor` 用 Mock。
    """
    from unittest.mock import Mock
    mock_app = Mock()
    mock_monitor = Mock()
    widget = DashboardWidget(app=mock_app, monitor=mock_monitor, loop=None)
    img = _grab(widget)
    _compare("dashboard_widget_empty", img)


def test_export_dialog_default(qapp, tmp_path):
    """ExportDialog 打开默认状态 — 默认文件名 + 4 种格式 radio + OK/Cancel。

    `app` Mock;`channel_ids` 给 2 个 channel。
    文件名默认带 `datetime.now().strftime('%Y%m%d-%H%M%S')` — 直接覆盖成
    固定串,保证 golden 稳定(否则每次跑时间戳都不一样)。
    """
    from unittest.mock import Mock
    mock_app = Mock()
    dlg = ExportDialog(app=mock_app, channel_ids=[1001, 1002])
    # 覆盖默认时间戳文件名 → 固定串,golden 可重现
    dlg.in_path.setText("./export-test.json")
    dlg.resize(QSize(360, 240))
    QApplication.processEvents()
    img = dlg.grab().toImage()
    _compare("export_dialog_default", img)


def test_login_dialog_initial(qapp):
    """LoginDialog 初始状态 — 按当前 `client.state` 选页(默认 phone)。

    `client.state` 通过 Mock 返 `phone_required`;`app` Mock,`loop` Mock。
    """
    from unittest.mock import Mock
    mock_app = Mock()
    mock_loop = Mock()
    mock_app.client.state = "phone_required"
    dlg = LoginDialog(app=mock_app, loop=mock_loop)
    dlg.resize(QSize(320, 280))
    QApplication.processEvents()
    img = dlg.grab().toImage()
    _compare("login_dialog_initial", img)


def test_main_window_initial(qapp, tmp_path):
    """MainWindow 启动后初始 dashboard 视图 — 已订 0 条 / 已加入 0 条 / 空消息区。

    `app` Mock,但 `.settings` 给真 `Settings`(SettingsPage._load_from_settings
    要 `api_id`/`api_hash`/... 真字段,不能是 Mock);`monitor.subscribed_ids`
    给 `set()`(不是 Mock)— MainWindow._refresh_state 拿它当 iterable 算交集。
    `list_joined_channels` / `list_messages` 走 AsyncMock return [],bootstrap
    UI 调度后拿到空集,屏幕稳定。
    """
    import asyncio
    from unittest.mock import AsyncMock, Mock

    from tgmonitor.core.config import (
        DBBackend,
        MediaPolicy,
        ObjectStoreBackend,
        Settings,
    )
    mock_app = Mock()
    mock_app.settings = Settings(
        api_id=1, api_hash="a" * 32, phone="+1",
        session_dir=tmp_path / "session",
        db_backend=DBBackend.JSONL, db_dsn="", db_root=tmp_path / "m",
        objectstore_backend=ObjectStoreBackend.FOLDER,
        objectstore_root=tmp_path / "media",
        media_policy=MediaPolicy.METADATA, data_root=tmp_path,
    )
    mock_app.client.state = "phone_required"
    mock_app.list_joined_channels = AsyncMock(return_value=[])
    mock_app.list_messages = AsyncMock(return_value=[])
    mock_monitor = Mock()
    mock_monitor.subscribed_ids = set()
    loop = asyncio.new_event_loop()
    try:
        win = MainWindow(
            app=mock_app, monitor=mock_monitor, loop=loop,
            env_path=tmp_path / ".env",
        )
        QApplication.processEvents()
        img = win.grab().toImage()
        _compare("main_window_initial", img)
    finally:
        # 让 `bootstrap_ui` / `load_recent_messages` 用 run_coro 排进 loop
        # 但还没 tick 的 coro 真跑一次 + cancel,避免 "coroutine was never
        # awaited"。一次 0s sleep 让 call_soon 调度生效,够 cancel。
        # `asyncio.set_event_loop(loop)` 显式把当前 loop 钉死,后续
        # `asyncio.all_tasks(loop)` / `asyncio.gather(...)` 不会触发
        # "There is no current event loop" DeprecationWarning
        # (Python 3.12+ 严格要求显式 set)。
        try:
            asyncio.set_event_loop(loop)
            # Drain loop to let run_coro-scheduled tasks complete.
            #
            # `MonitorViewModel.bootstrap_ui` 和 `_refresh_state` 通过
            # `run_coro(self.loop, _go())` 把 `_go` coro 推上 loop,内部用
            # `loop.call_soon_threadsafe` 排 Task 创建。`_go` 调
            # `await app.list_messages(...)`(AsyncMock),需要 ≥1 个 tick
            # resolve。我们 drain 几次到 `all_tasks` 稳定为空,然后 cancel
            # 兜底 + gather 排空。
            #
            # **已知限制**:CI run #3097969961 / #30979320017 等多次跑
            # 仍有 `RuntimeWarning: coroutine 'MonitorViewModel.
            # load_recent_messages.<locals>._go' was never awaited` —
            # 根因是 `channels_changed` signal handler(由
            # `refresh_joined_channels._go` 触发)在 tick 期间 emit 又调
            # `_refresh_state`,后者再排新的 `load_recent_messages._go`,
            # 与 drain 产生 race。warning 不让 pytest fail,只是噪声 —
            # 真传话的集成测试在
            # `test_main_window_channels.py::test_main_window_initial_refresh_state_is_empty`
            # 用 `qloop` fixture 走标准 asyncio 测试路径,无 warning。
            for _tick in range(20):
                loop.run_until_complete(asyncio.sleep(0))
                if not asyncio.all_tasks(loop):
                    break
            for t in [x for x in asyncio.all_tasks(loop) if not x.done()]:
                t.cancel()
            loop.run_until_complete(
                asyncio.gather(*asyncio.all_tasks(loop), return_exceptions=True),
            )
        except Exception:
            pass
        loop.close()