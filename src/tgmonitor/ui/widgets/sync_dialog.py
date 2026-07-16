"""全量同步对话框 — 用户多选频道 → 选 options → 实时进度。

两个组件:
- `SyncOptionsDialog` (QDialog):让用户选 options(只元数据 / 只历史 / 全部;
  续拉开关;手动覆盖延迟)
- `SyncProgressDialog` (QDialog):运行中显示每个频道状态,可"取消"
"""
from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.dto import (
    SyncOptions,
    SyncResult,
)
from tgmonitor.core.events import ChannelSyncDone, ChannelSyncProgress

log = logging.getLogger(__name__)


class SyncOptionsDialog(QDialog):
    """让用户选全量同步的 options。"""

    def __init__(
        self,
        channel_ids: list[int],
        channel_titles: dict[int, str],
        defaults: SyncOptions,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("全量同步选项")
        self.setModal(True)
        self._channel_ids = channel_ids
        self._channel_titles = channel_titles
        self._result_options: SyncOptions | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        # 概要
        head = QLabel(
            f"将对 <b>{len(channel_ids)}</b> 个频道执行全量同步。"
        )
        root.addWidget(head)

        # 频道列表
        self.list_widget = QListWidget()
        for cid in channel_ids:
            title = channel_titles.get(cid, f"#{cid}")
            item = QListWidgetItem(f"{title}  (#{cid})")
            self.list_widget.addItem(item)
        self.list_widget.setMaximumHeight(180)
        root.addWidget(self.list_widget)

        # 选项
        self.chk_metadata = QCheckBox("拉取 / 刷新元数据(title / username / member_count)")
        self.chk_metadata.setChecked(defaults.include_metadata)
        root.addWidget(self.chk_metadata)

        self.chk_history = QCheckBox("拉取历史消息(getChatHistory)")
        self.chk_history.setChecked(defaults.include_history)
        root.addWidget(self.chk_history)

        self.chk_resume = QCheckBox("续拉(从 storage 已有最大 msg_id 开始)")
        self.chk_resume.setChecked(defaults.resume_from_saved)
        root.addWidget(self.chk_resume)

        # 延迟
        h = QHBoxLayout()
        h.addWidget(QLabel("单条 API 间隔:"))
        self.spin_chat_delay = QSpinBox()
        self.spin_chat_delay.setRange(50, 60000)
        self.spin_chat_delay.setSuffix(" ms")
        self.spin_chat_delay.setValue(defaults.chat_delay_ms)
        h.addWidget(self.spin_chat_delay)
        h.addStretch(1)
        root.addLayout(h)

        h = QHBoxLayout()
        h.addWidget(QLabel("分页间隔(每 100 条):"))
        self.spin_page_delay = QSpinBox()
        self.spin_page_delay.setRange(100, 60000)
        self.spin_page_delay.setSuffix(" ms")
        self.spin_page_delay.setValue(defaults.page_delay_ms)
        h.addWidget(self.spin_page_delay)
        h.addStretch(1)
        root.addLayout(h)

        # 按钮
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _on_ok(self) -> None:
        if not (self.chk_metadata.isChecked() or self.chk_history.isChecked()):
            # 至少选一项
            log.warning("全量同步至少选一项")
            return
        self._result_options = SyncOptions(
            include_metadata=self.chk_metadata.isChecked(),
            include_history=self.chk_history.isChecked(),
            history_limit=None,
            chat_delay_ms=self.spin_chat_delay.value(),
            page_delay_ms=self.spin_page_delay.value(),
            resume_from_saved=self.chk_resume.isChecked(),
        )
        self.accept()

    def options(self) -> SyncOptions | None:
        return self._result_options


class SyncProgressDialog(QDialog):
    """显示每个频道实时进度的对话框 — 可取消。

    订阅 bus 上的 ChannelSyncProgress / ChannelSyncDone 事件。
    """

    def __init__(
        self,
        channel_titles: dict[int, str],
        cancel_cb: callable,  # type: ignore[type-arg]
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("全量同步中…")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setMinimumHeight(380)
        self._channel_titles = channel_titles
        self._cancel_cb = cancel_cb
        self._rows: dict[int, int] = {}  # channel_id -> list row index

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)

        self.lbl_summary = QLabel("准备开始…")
        root.addWidget(self.lbl_summary)

        self.list_widget = QListWidget()
        self.list_widget.setUniformItemSizes(False)
        root.addWidget(self.list_widget, 1)

        h = QHBoxLayout()
        h.addStretch(1)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self._on_cancel)
        h.addWidget(self.btn_cancel)
        self.btn_close = QPushButton("关闭")
        self.btn_close.setEnabled(False)
        self.btn_close.clicked.connect(self.accept)
        h.addWidget(self.btn_close)
        root.addLayout(h)

    def _add_row(self, channel_id: int) -> int:
        title = self._channel_titles.get(channel_id, f"#{channel_id}")
        item = QListWidgetItem(f"⏳ {title}  — 待开始")
        self.list_widget.addItem(item)
        row = self.list_widget.count() - 1
        self._rows[channel_id] = row
        return row

    def _update_row(self, channel_id: int, text: str) -> None:
        row = self._rows.get(channel_id)
        if row is None:
            row = self._add_row(channel_id)
        item = self.list_widget.item(row)
        if item is not None:
            item.setText(text)

    # ---- 事件回调(从 bus 调,主线程) ----

    def on_progress(self, e: ChannelSyncProgress) -> None:
        if e.channel_id not in self._rows:
            self._add_row(e.channel_id)
        title = self._channel_titles.get(e.channel_id, f"#{e.channel_id}")
        icon = {
            "metadata": "🔄",
            "history": "📥",
            "backoff": "⏸",
            "done": "✅",
            "failed": "❌",
        }.get(e.stage, "•")
        if e.stage == "history" and e.total is None:
            progress_str = f"{e.progress} 条"
        elif e.total:
            progress_str = f"{e.progress}/{e.total}"
        else:
            progress_str = str(e.progress)
        detail = f" — {e.detail}" if e.detail else ""
        self._update_row(
            e.channel_id, f"{icon} {title}  [{e.stage}] {progress_str}{detail}"
        )

    def on_done(self, e: ChannelSyncDone) -> None:
        result: SyncResult | None = e.result
        if result is None:
            self.lbl_summary.setText("同步已完成")
        else:
            n_ok = sum(
                1 for r in result.per_channel.values()
                if r.error is None and not r.rate_limited
            )
            n_fail = sum(1 for r in result.per_channel.values() if r.error)
            n_added = result.total_messages_added
            rate = result.rate_limited_seconds or 0
            extra = f"(被限流等待 {rate:.0f}s)" if rate else ""
            self.lbl_summary.setText(
                f"完成:成功 {n_ok} 失败 {n_fail} 新增消息 {n_added} 条 {extra}"
                + ("(已取消)" if result.cancelled else "")
            )
        self.btn_cancel.setEnabled(False)
        self.btn_close.setEnabled(True)
        # 关掉 window title 里的省略号
        self.setWindowTitle("全量同步完成")

    def _on_cancel(self) -> None:
        self._cancel_cb()
        self.lbl_summary.setText("已请求取消,等待当前频道完成…")
        self.btn_cancel.setEnabled(False)
