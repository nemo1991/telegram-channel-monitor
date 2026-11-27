# mypy: disable-error-code="attr-defined"
"""PluginManagerDialog — 插件管理页(ADR-001 阶段 4)。

设置页「账户凭证」分组的「插件管理」按钮打开本对话框:
- 左侧列表:全部已发现插件 + 状态标识(已启用 / 已停用 / 失败);
- 右侧详情:manifest 元数据 + 启用/停用开关 + 按 id 隔离的配置 KV 表格。

设计约定:
- **只经 PluginHost 公开 API 操作**:enable / disable / records / config_for /
  save_config / refresh_discovered。对话框不碰 enabled.json / config.json 文件,
  也不 import plugins.config —— 写盘策略归宿主,UI 只呈现。
- **真热切换**(用户确认):enable/disable 立即生效,无需重启;宿主负责能力
  注册/注销、运行期钩子、状态通知。代码改动仍需重启(Python 模块不可卸载)。
- **配置值保留类型**:KV 表格的 value 走 `json.loads` 字面量解析 —— 合法
  JSON(数字/布尔/数组/对象/null)按解析值写回,非法输入当字符串。这样无
  schema 也不破坏插件期望的值类型;解析函数抽成模块级便于单测。
- **状态订阅**:挂 `host.subscribe_state`,宿主 enable/disable 后自动重绘;
  关闭时用返回的退订函数解绑,避免单例持有已销毁 widget。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.plugins.host import PluginState

if TYPE_CHECKING:
    from tgmonitor.plugins.host import PluginHost


def parse_config_value(raw: str) -> object:
    """把 KV 表格 value 文本解析成配置值 — 合法 JSON 保类型,否则字符串。

    空串 → 空字符串(不是 JSON null,插件更常把它当未设置)。`true` / `42` /
    `[1,2]` / `{"k":1}` / `null` 都按 JSON 字面量还原类型;普通文本原样。
    这样无 schema 的配置也不会把插件期望的 bool/int 悄悄变成字符串。
    """
    if raw == "":
        return ""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def display_config_value(value: object) -> str:
    """配置值 → 表格显示文本 — 字符串裸显,其它类型 JSON 序列化。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (ValueError, TypeError):
        return str(value)


_STATE_LABELS = {
    PluginState.ACTIVE: "已启用",
    PluginState.DISABLED: "已停用",
    PluginState.FAILED: "失败",
    PluginState.DISCOVERED: "已发现",
}


class PluginManagerDialog(QDialog):
    """插件管理对话框 — 构造注入 host,不碰单例之外的全局状态。

    `on_changed` 是可选回调:任一插件 enable/disable 成功后调用,让 MainWindow
    重建插件 UI 页面(热切换必须即时反映到导航/stack)。
    """

    def __init__(
        self,
        host: PluginHost,
        parent: QWidget | None = None,
        *,
        on_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._host = host
        self._on_changed = on_changed
        self._unsubscribe: Callable[[], None] | None = None
        self._selected_id: str | None = None
        self.setObjectName("pluginManagerDialog")
        self.setWindowTitle(self.tr("插件管理"))
        self.resize(720, 480)
        self._build()
        self._refresh()
        self._unsubscribe = host.subscribe_state(self._refresh)

    def closeEvent(self, event) -> None:  # noqa: N802
        """关闭时退订状态通知 — 单例不能持有已销毁 widget 的绑定方法。"""
        unsub = self._unsubscribe
        if callable(unsub):
            unsub()
        self._unsubscribe = None
        super().closeEvent(event)

    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(16)

        # ---- 左:已发现插件列表 ----
        left = QVBoxLayout()
        left.addWidget(QLabel(self.tr("已发现插件")))
        self.plugin_list = QListWidget()
        self.plugin_list.setObjectName("pluginManagerList")
        self.plugin_list.currentItemChanged.connect(self._on_plugin_selected)
        left.addWidget(self.plugin_list, 1)
        btn_refresh = QPushButton(self.tr("重新发现"))
        btn_refresh.setObjectName("pluginManagerRefreshBtn")
        btn_refresh.setToolTip(self.tr("扫描新安装 / 移除的插件"))
        btn_refresh.clicked.connect(self._on_refresh_discovered)
        left.addWidget(btn_refresh)
        root.addLayout(left, 1)

        # ---- 右:详情 + 开关 + 配置 ----
        right = QVBoxLayout()
        self.lbl_title = QLabel(self.tr("选择一个插件"))
        self.lbl_title.setObjectName("pluginManagerTitle")
        right.addWidget(self.lbl_title)
        self.lbl_meta = QLabel("")
        self.lbl_meta.setWordWrap(True)
        self.lbl_meta.setObjectName("pluginManagerMeta")
        right.addWidget(self.lbl_meta)

        toggle_row = QHBoxLayout()
        self.btn_toggle = QPushButton(self.tr("启用"))
        self.btn_toggle.setObjectName("pluginManagerToggleBtn")
        self.btn_toggle.setEnabled(False)
        self.btn_toggle.clicked.connect(self._on_toggle)
        toggle_row.addWidget(self.btn_toggle)
        self.lbl_hint = QLabel("")
        self.lbl_hint.setObjectName("pluginManagerHint")
        toggle_row.addWidget(self.lbl_hint, 1)
        right.addLayout(toggle_row)

        right.addWidget(QLabel(self.tr("配置(值按 JSON 字面量解析)")))
        self.config_table = QTableWidget(0, 2)
        self.config_table.setObjectName("pluginManagerConfigTable")
        self.config_table.setHorizontalHeaderLabels([self.tr("键"), self.tr("值")])
        self.config_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.config_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.config_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        right.addWidget(self.config_table, 1)

        config_btn_row = QHBoxLayout()
        btn_add = QPushButton(self.tr("添加行"))
        btn_add.setObjectName("pluginManagerAddRowBtn")
        btn_add.clicked.connect(self._on_add_row)
        config_btn_row.addWidget(btn_add)
        btn_remove = QPushButton(self.tr("删除行"))
        btn_remove.setObjectName("pluginManagerRemoveRowBtn")
        btn_remove.clicked.connect(self._on_remove_row)
        config_btn_row.addWidget(btn_remove)
        config_btn_row.addStretch(1)
        self.btn_save_config = QPushButton(self.tr("保存配置"))
        self.btn_save_config.setObjectName("pluginManagerSaveConfigBtn")
        self.btn_save_config.setEnabled(False)
        self.btn_save_config.clicked.connect(self._on_save_config)
        config_btn_row.addWidget(self.btn_save_config)
        right.addLayout(config_btn_row)

        btn_close = QPushButton(self.tr("关闭"))
        btn_close.setObjectName("pluginManagerCloseBtn")
        btn_close.clicked.connect(self.accept)
        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_row.addWidget(btn_close)
        right.addLayout(close_row)
        root.addLayout(right, 2)

    # ---- 呈现 ----

    def _refresh(self) -> None:
        """重绘列表 + 当前选中详情 — 宿主状态变更后由订阅自动调用。"""
        current = self._selected_id
        records = self._host.records()
        self.plugin_list.blockSignals(True)
        self.plugin_list.clear()
        for pid, rec in sorted(records.items()):
            item = QListWidgetItem(
                f"{rec.manifest.name or pid}  [{_STATE_LABELS.get(rec.state, rec.state.value)}]"
            )
            item.setData(Qt.ItemDataRole.UserRole, pid)
            self.plugin_list.addItem(item)
        self.plugin_list.blockSignals(False)

        # 保留选中项(若仍在);否则选第一项
        target_row = 0
        for i in range(self.plugin_list.count()):
            if self.plugin_list.item(i).data(Qt.ItemDataRole.UserRole) == current:
                target_row = i
                break
        if self.plugin_list.count():
            self.plugin_list.setCurrentRow(target_row)
        else:
            self._selected_id = None
            self._show_empty()

    def _show_empty(self) -> None:
        self.lbl_title.setText(self.tr("选择一个插件"))
        self.lbl_meta.setText("")
        self.btn_toggle.setEnabled(False)
        self.config_table.setRowCount(0)
        self.btn_save_config.setEnabled(False)

    def _on_plugin_selected(self, current: QListWidgetItem | None, _prev) -> None:
        if current is None:
            self._selected_id = None
            self._show_empty()
            return
        pid = str(current.data(Qt.ItemDataRole.UserRole))
        self._selected_id = pid
        rec = self._host.records().get(pid)
        if rec is None:
            self._show_empty()
            return
        m = rec.manifest
        self.lbl_title.setText(f"{m.name or pid} · {pid}")
        caps = ", ".join(m.capabilities) or self.tr("无")
        meta = self.tr("版本 {v} · 来源 {src} · 能力 {caps} · 状态 {state}").format(
            v=m.version,
            src=rec.source,
            caps=caps,
            state=_STATE_LABELS.get(rec.state, rec.state.value),
        )
        if m.description:
            meta += f"\n{m.description}"
        if rec.error:
            meta += f"\n{self.tr('错误')}: {rec.error}"
        self.lbl_meta.setText(meta)

        is_active = rec.state is PluginState.ACTIVE
        self.btn_toggle.setText(self.tr("停用") if is_active else self.tr("启用"))
        self.btn_toggle.setEnabled(True)
        self.lbl_hint.setText("")

        config = self._host.config_for(pid)
        self.config_table.setRowCount(0)
        for key, value in sorted(config.items()):
            row = self.config_table.rowCount()
            self.config_table.insertRow(row)
            self.config_table.setItem(row, 0, QTableWidgetItem(str(key)))
            self.config_table.setItem(row, 1, QTableWidgetItem(display_config_value(value)))
        self.btn_save_config.setEnabled(True)

    # ---- 操作 ----

    def _on_refresh_discovered(self) -> None:
        fresh = self._host.refresh_discovered()
        self.lbl_hint.setText(
            self.tr("发现 {n} 个新插件").format(n=len(fresh)) if fresh else self.tr("没有新插件")
        )

    def _on_toggle(self) -> None:
        pid = self._selected_id
        if pid is None:
            return
        rec = self._host.records().get(pid)
        if rec is None:
            return
        if rec.state is PluginState.ACTIVE:
            self._host.disable(pid)
            self.lbl_hint.setText(self.tr("已停用(即时生效)"))
        else:
            ok = self._host.enable(pid)
            self.lbl_hint.setText(
                self.tr("已启用(即时生效)") if ok else self.tr("启用失败,详见日志")
            )
        self._notify_changed()

    def _on_add_row(self) -> None:
        if self._selected_id is None:
            return
        row = self.config_table.rowCount()
        self.config_table.insertRow(row)
        key_item = QTableWidgetItem("")
        self.config_table.setItem(row, 0, key_item)
        self.config_table.setItem(row, 1, QTableWidgetItem(""))
        self.config_table.editItem(key_item)

    def _on_remove_row(self) -> None:
        row = self.config_table.currentRow()
        if row >= 0:
            self.config_table.removeRow(row)

    def _on_save_config(self) -> None:
        pid = self._selected_id
        if pid is None:
            return
        data: dict[str, object] = {}
        for row in range(self.config_table.rowCount()):
            key_item = self.config_table.item(row, 0)
            value_item = self.config_table.item(row, 1)
            key = key_item.text().strip() if key_item is not None else ""
            if not key:
                continue
            raw = value_item.text() if value_item is not None else ""
            data[key] = parse_config_value(raw)
        try:
            self._host.save_config(pid, data)
        except (OSError, TypeError, ValueError) as exc:
            self.lbl_hint.setText(self.tr("配置保存失败: {err}").format(err=exc))
            return
        self.lbl_hint.setText(self.tr("配置已保存"))

    def _notify_changed(self) -> None:
        """通知外部( MainWindow )插件状态已变 — 用于重建插件 UI 页面。"""
        cb = self._on_changed
        if callable(cb):
            cb()
