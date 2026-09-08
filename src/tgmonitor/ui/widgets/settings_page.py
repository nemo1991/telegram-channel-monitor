# mypy: disable-error-code="attr-defined"
"""SettingsPage — 整页设置(不再是模态对话框)。

囊括原 settings_dialog.py 的全部配置项 + account_widget.py 的凭据编辑。
以 QScrollArea 内分组排列,底部固定「保存到 .env」+「保存并应用」按钮。

分组:
  1. 📱 账户凭证   — API ID / Hash / Phone(来自 account_widget)
  2. 🌐 网络代理   — SOCKS5 URL + 测试连接
  3. 💾 消息存储   — DB 后端 + DSN / 目录
  4. 📁 对象存储   — 后端 + 本地目录 / S3 凭据
  5. ⚙️ 策略       — 媒体下载策略
  6. 🌐 语言       — 2026-09-07 v1.6.8 新增(zh_CN / en_US)
  7. ⌨ 快捷键      — 2026-09-07 v1.6.9 新增(14 个 action × QKeySequenceEdit)
  8. 🎨 外观       — 主题 3 选(浅色 / 暗色 / 跟随系统)
  9. 🔄 同步参数   — chat_delay / page_delay / resume_from_saved
 10. 储存按钮栏
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend, _user_data_dir
from tgmonitor.core.settings_store import EditableSettings, update_env_with_settings
from tgmonitor.i18n import install_translator
from tgmonitor.ui._async import run_coro
from tgmonitor.ui.widgets.form_row import combo_field, path_field, spin_field, text_field

if TYPE_CHECKING:
    from pathlib import Path

    from tgmonitor.core.app_service import AppService

log = logging.getLogger(__name__)


class SettingsPage(QWidget):
    """整页设置。在 QStackedWidget 中作为一页使用。

    构造后自动从 app.settings 加载当前值。
    UI 改动不实时生效 — 用户点「保存并应用」或「保存到 .env」才写。
    """

    def __init__(
        self,
        app: AppService,
        loop: asyncio.AbstractEventLoop,
        env_path: Path,
        parent: QWidget | None = None,
    ) -> None:
        """建 7 个分组(账户 / 代理 / DB / OS / 策略 / 同步)+ 底部存盘按钮 + 加载当前设置。

        `env_path` 是「保存到 .env」按钮的写入路径(platform-native 由 app.py 注入)。
        """
        super().__init__(parent)
        self._app = app
        self._loop = loop
        self._env_path = env_path

        self._build()
        self._load_from_settings()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 固定标题
        # 2026-09-07 v1.6.8:标题走 tr() — retranslateUi 时再设一次,免得
        # LanguageChange 不刷。
        self.header_label = QLabel(self.tr("设置"))
        self.header_label.setObjectName("pageTitle")
        self.header_label.setContentsMargins(24, 24, 24, 8)
        root.addWidget(self.header_label)

        # 滚动区域
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("settingsScroll")

        scroll_content = QWidget()
        form_root = QVBoxLayout(scroll_content)
        form_root.setContentsMargins(24, 8, 24, 24)
        form_root.setSpacing(16)

        self._build_account(form_root)
        self._build_proxy(form_root)
        self._build_storage(form_root)
        self._build_objectstore(form_root)
        self._build_policy(form_root)
        # 2026-09-07 v1.6.8:新增「语言」分组,置于「外观(主题)」之上,
        # 由 user 决定的语言在主题之前更显眼。
        self._build_language(form_root)
        # 2026-09-07 v1.6.9:新增「快捷键」分组,置于「语言」与「外观」之间 —
        # 全局 UI 设置在视觉主题之上更显眼。
        self._build_keybindings(form_root)
        # 2026-08-30 v1.5.0 PR #A5:外观组(主题 3 选)— 与策略平级,
        # 不走「保存并应用」(主题是 session 内即时生效,不写 .env)。
        self._build_appearance(form_root)
        self._build_sync(form_root)

        form_root.addStretch(1)
        scroll.setWidget(scroll_content)
        root.addWidget(scroll, 1)

        # 底部固定按钮
        btn_bar = QHBoxLayout()
        btn_bar.setContentsMargins(24, 12, 24, 16)

        self.btn_save_env = QPushButton(self.tr("仅保存到 .env"))
        self.btn_save_env.clicked.connect(self._on_save_env)
        btn_bar.addWidget(self.btn_save_env)

        btn_bar.addStretch(1)

        self.btn_apply = QPushButton(self.tr("保存并应用"))
        self.btn_apply.setObjectName("primaryBtn")
        self.btn_apply.clicked.connect(self._on_apply)
        btn_bar.addWidget(self.btn_apply)

        root.addLayout(btn_bar)

    # ------ 各分组装配 ------

    def _build_account(self, root: QVBoxLayout) -> None:
        # 2026-09-07 v1.6.8:所有用户可见 label 走 tr()。
        g = QGroupBox(self.tr("📱 账户凭证"))
        f = QFormLayout(g)
        f.setSpacing(6)

        self.in_api_id = spin_field(
            f,
            self.tr("API ID:"),
            min=0,
            max=2_000_000_000,
            value=0,
        )

        self.in_api_hash = text_field(
            f,
            self.tr("API Hash:"),
            self.tr("32 位 hash · my.telegram.org"),
            echo_password=True,
        )

        self.in_phone = text_field(f, self.tr("手机号:"), "+8613800000000")

        # Session 目录(浏览 + 恢复默认)— path_field helper
        self.in_session_dir = path_field(
            f,
            self.tr("Session 目录:"),
            str(_user_data_dir() / "session"),
            on_default=lambda: self._set_default(self.in_session_dir, "session"),
            default_tooltip=self.tr("恢复为 platform-native 默认目录"),
            parent=self,
        )

        root.addWidget(g)

    def _build_proxy(self, root: QVBoxLayout) -> None:
        g = QGroupBox(self.tr("🌐 网络代理 (Proxy)"))
        f = QFormLayout(g)
        f.setSpacing(6)

        proxy_row = QHBoxLayout()
        self.in_proxy = QLineEdit()
        self.in_proxy.setPlaceholderText("socks5://[user:pass@]host:port")
        proxy_row.addWidget(self.in_proxy, 1)
        self.btn_test_proxy = QPushButton(self.tr("测试连接"))
        self.btn_test_proxy.clicked.connect(self._on_test_proxy)
        proxy_row.addWidget(self.btn_test_proxy)
        f.addRow(self.tr("代理 URL:"), proxy_row)

        root.addWidget(g)

    def _build_storage(self, root: QVBoxLayout) -> None:
        g = QGroupBox(self.tr("💾 消息存储 (Database)"))
        f = QFormLayout(g)
        f.setSpacing(6)

        self.cmb_db = combo_field(f, self.tr("后端:"), DBBackend)

        self.in_db_dsn = text_field(f, self.tr("DSN:"), "postgresql://user:pass@host/db")

        self.in_db_root = path_field(
            f,
            self.tr("JSONL 目录:"),
            str(_user_data_dir() / "messages"),
            on_default=lambda: self._set_default(self.in_db_root, "messages"),
            default_tooltip=self.tr("恢复为 platform-native 默认目录"),
            parent=self,
        )

        # DB 后端切换 → 显隐 DSN / 目录
        self.cmb_db.currentIndexChanged.connect(self._on_db_backend_changed)
        self._on_db_backend_changed()

        root.addWidget(g)

    def _build_objectstore(self, root: QVBoxLayout) -> None:
        g = QGroupBox(self.tr("📁 对象存储 (ObjectStore)"))
        f = QFormLayout(g)
        f.setSpacing(6)

        self.cmb_os = combo_field(f, self.tr("后端:"), ObjectStoreBackend)

        # 本地
        self.in_os_root = path_field(
            f,
            self.tr("本地目录:"),
            str(_user_data_dir() / "media"),
            on_default=lambda: self._set_default(self.in_os_root, "media"),
            default_tooltip=self.tr("恢复为 platform-native 默认目录"),
            parent=self,
        )

        # S3
        self.in_os_endpoint = text_field(
            f, self.tr("S3 Endpoint:"), "https://s3.<region>.amazonaws.com"
        )
        self.in_os_region = text_field(f, self.tr("Region:"), "us-east-1")
        self.in_os_access_key = text_field(f, self.tr("Access Key:"), "", echo_password=True)
        self.in_os_secret_key = text_field(f, self.tr("Secret Key:"), "", echo_password=True)
        self.in_os_bucket = text_field(f, self.tr("Bucket:"), "tgmonitor")
        # 2026-09-07 v1.6.8:长 hint 也走 tr()。
        self.lbl_os_s3_hint = QLabel(
            self.tr(
                "提示:AWS 填 s3.<region>.amazonaws.com(留空走默认);"
                "MinIO / 阿里 OSS 填各自 API 地址;勿填控制台 / 网页地址"
            )
        )
        self.lbl_os_s3_hint.setProperty("role", "hint")
        self.lbl_os_s3_hint.setWordWrap(True)
        f.addRow("", self.lbl_os_s3_hint)

        self.cmb_os.currentIndexChanged.connect(self._on_os_backend_changed)
        self._on_os_backend_changed()

        root.addWidget(g)

    def _build_policy(self, root: QVBoxLayout) -> None:
        g = QGroupBox(self.tr("⚙️ 策略"))
        f = QFormLayout(g)
        f.setSpacing(6)

        self.cmb_media = combo_field(f, self.tr("媒体下载:"), MediaPolicy)

        self.in_media_max = spin_field(
            f,
            self.tr("单文件大小上限:"),
            min=0,
            max=10240,  # 0 = 无限制,10 GB 上限
            suffix=" MB",
            single_step=10,
            tooltip=self.tr("单文件下载上限。0 = 无限制(慎用,可能下载 GB 级视频把磁盘占满)。"),
        )

        self.in_data_root = path_field(
            f,
            self.tr("数据根目录:"),
            str(_user_data_dir()),
            on_default=lambda: self._set_default(self.in_data_root, ""),
            default_tooltip=self.tr("恢复为 platform-native 默认目录"),
            parent=self,
        )

        root.addWidget(g)

    def _build_language(self, root: QVBoxLayout) -> None:
        """2026-09-07 v1.6.8:新增「语言」分组,提供界面语言切换下拉。

        位置:`_build_policy` 之后、`_build_appearance` 之前(语言 = 主题之
        上)。设置保存后调 `install_translator(...)` 并 post LanguageChange
        让所有 top-level widget 立即重译。

        注:`retranslateUi` 也会重建这个下拉的 item 文本(「简体中文」/
        「English」),但保留 `currentData()` 不变以免误触发 on_lang_changed。
        """
        g = QGroupBox(self.tr("🌐 语言"))
        f = QFormLayout(g)
        f.setSpacing(6)

        self.cmb_lang = QComboBox()
        # 显示文本用 tr() 翻译;data 仍是 locale code(zh_CN / en_US)
        # 写 .env / Settings.lang 时按 data 取,跟 enum 一样不依赖显示。
        self.cmb_lang.addItem(self.tr("简体中文"), "zh_CN")
        self.cmb_lang.addItem(self.tr("English"), "en_US")
        f.addRow(self.tr("界面语言:"), self.cmb_lang)

        help_lang = QLabel(self.tr("切换后立即生效,所有界面文字立即重译。重启后保留。"))
        help_lang.setProperty("role", "hint")
        help_lang.setWordWrap(True)
        f.addRow(help_lang)

        root.addWidget(g)

    def _build_keybindings(self, root: QVBoxLayout) -> None:
        """2026-09-07 v1.6.9:新增「快捷键」分组,提供 14 个 action × QKeySequenceEdit。

        位置:`_build_language` 之后、`_build_appearance` 之前(语言 = 主
        题之上的全局 UI 设置,快捷键同性质)。设置保存后:
        1. `_on_apply` 校验冲突(`find_duplicates`);冲突 → 弹 warning
           保留旧值,不落盘。
        2. `_app.reconfigure(new_settings)` 走默认路径(diff_settings 全
           False → 不重 backend,cheap)。
        3. `MainWindow.reload_shortcuts(new_settings)` 热重绑 QShortcut
           + tray `act_show`/`act_quit` 的 `setShortcut`。
        4. `update_env_with_settings` 批量写 .env,下次启动保留。

        注:`retranslateUi` 重建 label 文案(label 是 `self.tr(...)` 静
        态创建,LanguageChange 时 `changeEvent → retranslateUi` 重新调
        `self.tr(label_text)`);edit widget 自身无 tr() 文本,保留。
        """
        from tgmonitor.core.keybinding import DEFAULT_BINDINGS, default_for

        g = QGroupBox(self.tr("⌨ 快捷键"))
        f = QFormLayout(g)
        f.setSpacing(6)

        self._keybinding_edits: dict[str, QKeySequenceEdit] = {}
        self._keybinding_labels: dict[str, QLabel] = {}
        for action in DEFAULT_BINDINGS:
            edit = QKeySequenceEdit()
            # 初值:Settings 字段非空用 settings,否则 default_for(action)
            settings_val = getattr(self._app.settings, f"key_{action}", "")
            edit.setKeySequence(QKeySequence(settings_val) if settings_val else default_for(action))
            edit.setToolTip(self.tr("清空 = 恢复默认"))
            self._keybinding_edits[action] = edit

            lbl = QLabel(self._keybinding_label(action) + ":")
            self._keybinding_labels[action] = lbl
            f.addRow(lbl, edit)

        help_keys = QLabel(
            self.tr(
                "点击输入框后按新快捷键即可重绑。清空 = 恢复默认。两动作绑同一键时,保存会失败。"
            )
        )
        help_keys.setProperty("role", "hint")
        help_keys.setWordWrap(True)
        f.addRow(help_keys)

        root.addWidget(g)

    def _keybinding_label(self, action: str) -> str:
        """action → self.tr(中文 label)。

        这里每个分支都 `self.tr(<literal>)` — pyside6-lupdate 静态扫描能
        抽到所有 14 行 label 字面量到 zh_CN.ts / en_US.ts(若用模块级
        dict literal,lupdate 看不到,strings 漏到翻译里)。

        `tr()` 在 zh_CN 模式下 = 原文;en_US 模式下 = .qm 里收录的英文。
        返回值供 QLabel.setText(...) + retranslateUi 用。
        """
        # 注意:每个分支必须是 `self.tr("literal")`,literal 必须是字面量;
        # dict literal / list comprehension / format() 都会被 lupdate 跳过。
        if action == "tab_live":
            return self.tr("切到「实时」页")
        if action == "tab_dashboard":
            return self.tr("切到「大盘」页")
        if action == "tab_channels":
            return self.tr("切到「频道」页")
        if action == "tab_media":
            return self.tr("切到「媒体管理」页")
        if action == "tab_settings":
            return self.tr("切到「设置」页")
        if action == "refresh":
            return self.tr("刷新频道列表")
        if action == "search":
            return self.tr("聚焦搜索框")
        if action == "export":
            return self.tr("导出")
        if action == "toggle_theme":
            return self.tr("切换主题")
        if action == "quit":
            return self.tr("退出")
        if action == "settings":
            return self.tr("打开设置页")
        if action == "escape":
            return self.tr("全局取消(Esc)")
        if action == "copy":
            return self.tr("复制当前消息")
        if action == "show_window":
            return self.tr("显示主窗口(tray)")
        return action

    def _build_appearance(self, root: QVBoxLayout) -> None:
        """2026-09-03 v1.5.4 PR #P4:外观设置 — 主题 3 选(LIGHT / DARK / SYSTEM)
        + **持久化 checkbox**(填 v1.5.0 PR #A5 尾巴)。

        - 主题下拉 → `ThemeManager.apply` 即时生效(原 v1.5.0 行为不变)
        - checkbox「持久化主题(写 .env `TG_KEY_THEME`)」勾选时:
          1. 立即写 .env 的 `key_theme` 字段(走既有 `update_env_with_settings`)
          2. 启动时 `app.settings.key_theme` 非空 → `ThemeManager.apply` 启动即应用
        - checkbox 取消勾选:清空 `key_theme`(.env 字段移除),与 v1.5.0 行为一致
        - 「仅保存到 .env」/「保存并应用」按钮不感知 theme 字段(主题是 session
          内即时生效,与 settings 表单分离;这是 v1.5.0 设计延续)
        """
        from tgmonitor.ui.theme import Theme, ThemeManager

        g = QGroupBox(self.tr("🎨 外观"))
        f = QFormLayout(g)
        f.setSpacing(6)

        self.cmb_theme = QComboBox()
        # 2026-09-07 v1.6.8:item 显示文本走 tr() — retranslateUi 时重建。
        self.cmb_theme.addItem(self.tr("浅色"), Theme.LIGHT.value)
        self.cmb_theme.addItem(self.tr("暗色"), Theme.DARK.value)
        self.cmb_theme.addItem(self.tr("跟随系统"), Theme.SYSTEM.value)
        # 同步当前 ThemeManager 状态
        cur = ThemeManager.current()
        for i in range(self.cmb_theme.count()):
            if self.cmb_theme.itemData(i) == cur.value:
                self.cmb_theme.setCurrentIndex(i)
                break
        self.cmb_theme.currentIndexChanged.connect(self._on_theme_change)
        f.addRow(self.tr("主题:"), self.cmb_theme)

        # 2026-09-03 v1.5.4 PR #P4:主题持久化 checkbox
        self.chk_theme_persist = QCheckBox(
            self.tr("持久化主题(写 .env `TG_KEY_THEME`,重启应用仍生效)")
        )
        self.chk_theme_persist.toggled.connect(self._on_theme_persist_toggled)
        f.addRow("", self.chk_theme_persist)

        # 快捷键帮助(只读 label,PR #A5 plan 列「快捷键组可改(v1 不持久化,
        # session 内生效)」)— v1.5.0 暂只显示帮助,不改键位。
        # 2026-09-07 v1.6.8:多行 hint 整体走 tr()。
        self.help_shortcuts = QLabel(
            self.tr(
                "快捷键: Ctrl+1..5 切页 · Ctrl+F 搜索 · Ctrl+E 导出 · Ctrl+T 主题切换\n"
                "        Ctrl+R 刷新频道 · Ctrl+Q 退出 · Ctrl+, 设置 · Esc 取消\n"
                "        Ctrl+C 复制当前消息文本\n"
                "\n"
                "快捷键目前 session 内生效,不持久化(后续 v1.5.5 支持)."
            )
        )
        self.help_shortcuts.setObjectName("helpText")
        self.help_shortcuts.setWordWrap(True)
        f.addRow(self.help_shortcuts)

        root.addWidget(g)

    def _on_theme_change(self, idx: int) -> None:
        """2026-09-03 v1.5.4 PR #P4:主题下拉变化 → 即时 ThemeManager.apply +
        (若 checkbox 勾选)持久化当前主题到 .env。
        """
        from tgmonitor.ui.theme import Theme, ThemeManager

        value = self.cmb_theme.itemData(idx)
        try:
            theme = Theme(value)
        except ValueError:
            return
        ThemeManager.apply(theme)
        # 持久化:checkbox 勾选时把当前 theme 写 .env
        if self.chk_theme_persist.isChecked():
            self._write_theme_to_env(theme.value)

    def _on_theme_persist_toggled(self, checked: bool) -> None:
        """2026-09-03 v1.5.4 PR #P4:勾选 = 写当前 theme,取消 = 清空字段。

        走与 ThemeManager 解耦的 `update_env_with_settings` 路径(传
        只带 key_theme 的 EditableSettings,其他字段保持原样)。
        """
        from tgmonitor.ui.theme import ThemeManager

        current_theme_value = ThemeManager.current().value
        e = EditableSettings(key_theme=current_theme_value if checked else "")
        # validate() 宽松,只校验 enum / 数值 — key_theme 不会被它管
        # 但 EditableSettings 的其他必填字段(api_id 等)会 fail。
        # 改走 EditableSettings 全部字段 + 仅改 key_theme
        e = EditableSettings.from_settings(self._app.settings)
        e.key_theme = current_theme_value if checked else ""
        try:
            update_env_with_settings(self._env_path, e.to_settings())
        except OSError:
            log.exception("write theme to .env failed")

    def _write_theme_to_env(self, theme_value: str) -> None:
        """2026-09-03 v1.5.4 PR #P4:写单一 key_theme 到 .env — 不动其他字段。"""
        e = EditableSettings.from_settings(self._app.settings)
        e.key_theme = theme_value
        try:
            update_env_with_settings(self._env_path, e.to_settings())
        except OSError:
            log.exception("write theme to .env failed")

    def _build_sync(self, root: QVBoxLayout) -> None:
        # 2026-09-07 v1.6.8:所有 label 走 tr()。
        g = QGroupBox(self.tr("🔄 同步参数"))
        f = QFormLayout(g)
        f.setSpacing(6)

        self.in_chat_delay = spin_field(
            f,
            self.tr("频道间间隔:"),
            min=50,
            max=60000,
            suffix=" ms",
            single_step=50,
        )

        self.in_page_delay = spin_field(
            f,
            self.tr("分页间隔:"),
            min=100,
            max=60000,
            suffix=" ms",
            single_step=100,
        )

        self.chk_resume = QCheckBox(self.tr("续拉(从已保存位置继续)"))
        f.addRow("", self.chk_resume)

        root.addWidget(g)

    # ------ 后端切换显隐 ------

    def _on_db_backend_changed(self) -> None:
        is_jsonl = self.cmb_db.currentData() == DBBackend.JSONL
        # DSN 行:postgres/mongo 时启用,jsonl 时禁用
        hit = self._find_form_row(self.in_db_dsn)
        if hit is not None:
            g, idx = hit
            self._set_form_row_visible(g, idx, not is_jsonl)
        # 目录行:jsonl 时启用
        hit = self._find_form_row(self.in_db_root)
        if hit is not None:
            g, idx = hit
            self._set_form_row_visible(g, idx, is_jsonl)

    def _on_os_backend_changed(self) -> None:
        is_local = self.cmb_os.currentData() in (
            ObjectStoreBackend.LOCAL,
            ObjectStoreBackend.FOLDER,
        )
        is_s3 = self.cmb_os.currentData() == ObjectStoreBackend.S3
        # 本地目录:local/folder 时显示
        hit = self._find_form_row(self.in_os_root)
        if hit is not None:
            g, idx = hit
            self._set_form_row_visible(g, idx, is_local)
        # S3 字段 + 提示:S3 时显示
        for w in (
            self.in_os_endpoint,
            self.in_os_region,
            self.in_os_access_key,
            self.in_os_secret_key,
            self.in_os_bucket,
            self.lbl_os_s3_hint,
        ):
            hit = self._find_form_row(w)
            if hit is not None:
                g, idx = hit
                self._set_form_row_visible(g, idx, is_s3)

    def _find_form_row(self, widget: QWidget) -> tuple[QGroupBox, int] | None:
        """在 form layout 里找到 widget 所在分组与行号,返回 `(group, row)`。

        `QFormLayout` 的 row index 只在**所属分组**内有效,必须连同分组一起
        返回,否则按裸 row index 跨分组操作会误伤其他分组的同名行(历史 bug:
        隐藏 S3 字段时把账户分组的手机号 / Session 目录、JSONL 目录一起藏掉)。

        `fl.itemAt(row, role)` 返回 `QLayoutItem`(不是 tuple),
        调 `.widget()` 拿真实控件再做相等检查。
        """
        for g in self.findChildren(QGroupBox):
            fl = g.findChild(QFormLayout)
            if fl is None:
                continue
            for i in range(fl.rowCount()):
                item = fl.itemAt(i, QFormLayout.FieldRole)
                if item is None:
                    continue
                fw = item.widget()
                if fw is None:
                    continue
                if fw is widget or _is_child_of(widget, fw):
                    return g, i
        return None

    def _set_form_row_visible(self, group: QGroupBox, row: int, visible: bool) -> None:
        """只操作**指定分组**内的一行(Label + Field 一起显隐)。"""
        fl = group.findChild(QFormLayout)
        if fl is None or row >= fl.rowCount():
            return
        for role in (QFormLayout.LabelRole, QFormLayout.FieldRole):
            item = fl.itemAt(row, role)
            # mypy 看到 `item.widget() -> QWidget | None`,虽然 item 已 truthy,
            # 但 widget() 自身仍返 None。显式 None 守卫清掉 union-attr。
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.setVisible(visible)

    # ------ 存/取 ------

    def _collect(self) -> EditableSettings:
        """收集当前表单值 → EditableSettings。"""
        ud = _user_data_dir()
        # 2026-09-07 v1.6.8:加 lang 字段。cmb_lang.data() 是 locale code
        # ("zh_CN" / "en_US"),直接传给 EditableSettings(它内部 pydantic
        # 字段是 Literal["zh_CN", "en_US"] — 用 data() 而非 currentText
        # 保证翻译后 user 选「English」不会写出 "English" 字面)。
        lang_value = self.cmb_lang.currentData() if hasattr(self, "cmb_lang") else "zh_CN"
        # 2026-09-07 v1.6.9:快捷键 — keySequence().toString() 直传
        # QKeySequence 串格式("Ctrl+R" 等);空 = 用户清空,落 .env = ""
        # (走 keybinding.default_for 兜底,与 `key_theme=""` 同语义)。
        kb = (
            {a: e.keySequence().toString() for a, e in self._keybinding_edits.items()}
            if hasattr(self, "_keybinding_edits")
            else {}
        )
        return EditableSettings(
            api_id=self.in_api_id.value(),
            api_hash=self.in_api_hash.text().strip(),
            phone=self.in_phone.text().strip(),
            session_dir=self.in_session_dir.text().strip() or str(ud / "session"),
            db_backend=self.cmb_db.currentData(),
            db_dsn=self.in_db_dsn.text().strip(),
            db_root=self.in_db_root.text().strip() or str(ud / "messages"),
            objectstore_backend=self.cmb_os.currentData(),
            objectstore_root=self.in_os_root.text().strip() or str(ud / "media"),
            objectstore_endpoint=self.in_os_endpoint.text().strip(),
            objectstore_region=self.in_os_region.text().strip() or "us-east-1",
            objectstore_access_key=self.in_os_access_key.text().strip(),
            objectstore_secret_key=self.in_os_secret_key.text().strip(),
            objectstore_bucket=self.in_os_bucket.text().strip() or "tgmonitor",
            media_policy=self.cmb_media.currentData(),
            media_max_mb=self.in_media_max.value(),
            data_root=self.in_data_root.text().strip() or str(ud),
            proxy=self.in_proxy.text().strip(),
            sync_chat_delay_ms=self.in_chat_delay.value(),
            sync_page_delay_ms=self.in_page_delay.value(),
            sync_resume_from_saved=self.chk_resume.isChecked(),
            # 2026-09-07 v1.6.8:新增语言字段(EditableSettings 上对应
            # lang: Literal["zh_CN", "en_US"];若 EditableSettings 未跟上,
            # pydantic extra="ignore" 会安全丢弃 — 但 Step 0 已加)。
            lang=lang_value,
            # 2026-09-07 v1.6.9:14 个 key_<action> 字段
            key_tab_live=kb.get("tab_live", ""),
            key_tab_dashboard=kb.get("tab_dashboard", ""),
            key_tab_channels=kb.get("tab_channels", ""),
            key_tab_media=kb.get("tab_media", ""),
            key_tab_settings=kb.get("tab_settings", ""),
            key_refresh=kb.get("refresh", ""),
            key_search=kb.get("search", ""),
            key_export=kb.get("export", ""),
            key_toggle_theme=kb.get("toggle_theme", ""),
            key_quit=kb.get("quit", ""),
            key_settings=kb.get("settings", ""),
            key_escape=kb.get("escape", ""),
            key_copy=kb.get("copy", ""),
            key_show_window=kb.get("show_window", ""),
        )

    def _load_from_settings(self) -> None:
        """从 app.settings 加载当前值到表单。"""
        s = self._app.settings
        self.in_api_id.setValue(s.api_id)
        self.in_api_hash.setText(s.api_hash)
        self.in_phone.setText(s.phone)
        self.in_session_dir.setText(str(s.session_dir))

        self.in_proxy.setText(s.proxy or "")

        idx = self.cmb_db.findData(s.db_backend)
        if idx >= 0:
            self.cmb_db.setCurrentIndex(idx)
        self.in_db_dsn.setText(s.db_dsn or "")
        self.in_db_root.setText(str(s.db_root))

        idx = self.cmb_os.findData(s.objectstore_backend)
        if idx >= 0:
            self.cmb_os.setCurrentIndex(idx)
        self.in_os_root.setText(str(s.objectstore_root))
        self.in_os_endpoint.setText(s.objectstore_endpoint or "")
        self.in_os_region.setText(s.objectstore_region or "")
        self.in_os_access_key.setText(s.objectstore_access_key or "")
        self.in_os_secret_key.setText(s.objectstore_secret_key or "")
        self.in_os_bucket.setText(s.objectstore_bucket or "")

        idx = self.cmb_media.findData(s.media_policy)
        if idx >= 0:
            self.cmb_media.setCurrentIndex(idx)
        self.in_media_max.setValue(s.media_max_bytes // (1024 * 1024))
        self.in_data_root.setText(str(s.data_root))

        self.in_chat_delay.setValue(s.sync_chat_delay_ms)
        self.in_page_delay.setValue(s.sync_page_delay_ms)
        self.chk_resume.setChecked(s.sync_resume_from_saved)

        # 2026-09-03 v1.5.4 PR #P4:主题持久化 checkbox 状态同步
        # (空 = 不勾选,与 v1.5.0 行为一致;非空 = 勾选)
        if hasattr(self, "chk_theme_persist"):
            self.chk_theme_persist.setChecked(bool(s.key_theme))

        # 2026-09-07 v1.6.8:回填当前语言。findData 用 locale code,不会
        # 受显示文本翻译影响。
        if hasattr(self, "cmb_lang"):
            idx_lang = self.cmb_lang.findData(s.lang)
            if idx_lang >= 0:
                self.cmb_lang.setCurrentIndex(idx_lang)

        # 2026-09-07 v1.6.9:回填快捷键 — keybinding.binding_for 拿实际
        # QKeySequence(空 settings.value → 走 default_for(action) 兜底,
        # UI 上仍显示硬编码默认,避免空白看起来「没绑」)。
        if hasattr(self, "_keybinding_edits"):
            from tgmonitor.core.keybinding import binding_for

            for action, edit in self._keybinding_edits.items():
                settings_val = getattr(s, f"key_{action}", "")
                edit.setKeySequence(binding_for(action, settings_val))

    # ------ 槽 ------

    def _on_save_env(self) -> None:
        """仅写 .env,不热重载;写前同样做后端连通性校验。

        2026-08-18 交互要求:与「保存并应用」一致,写 .env 前先建连验证
        storage / 对象存储(失败上抛、不落盘),避免把不可达的 DSN / S3
        端点写进 .env、下次启动 bootstrap 直接挂掉。
        """
        try:
            e = self._collect()
        except Exception as exc:  # noqa: BLE001 — Qt 槽内异常只打 stderr,用户无感知
            log.exception("collect settings failed")
            QMessageBox.critical(self, self.tr("保存失败"), self.tr(f"读取表单失败: {exc}"))
            return
        errs = e.validate()
        if errs:
            QMessageBox.warning(self, self.tr("校验失败"), "\n".join(errs))
            return
        new_settings = e.to_settings()

        # 校验期间禁用按钮,避免重复点击叠加多个校验任务
        self.btn_save_env.setEnabled(False)
        self.btn_apply.setEnabled(False)

        async def _validate_and_save() -> None:
            await self._app.validate_backends(new_settings)
            # 后端就绪后才写 .env(重启 bootstrap 用的就是这份配置)
            update_env_with_settings(self._env_path, new_settings)

        def _saved(_result: object) -> None:
            self.btn_save_env.setEnabled(True)
            self.btn_apply.setEnabled(True)
            QMessageBox.information(
                self, self.tr("已保存"), self.tr(f"设置已写入 {self._env_path}")
            )

        def _save_failed(exc: BaseException) -> None:
            self.btn_save_env.setEnabled(True)
            self.btn_apply.setEnabled(True)
            if isinstance(exc, OSError):
                QMessageBox.critical(self, self.tr(".env 写入失败"), str(exc))
                return
            QMessageBox.critical(
                self,
                self.tr("保存失败"),
                self.tr(
                    f"后端配置未通过校验,已放弃保存(设置未写入 .env):\n\n{exc}\n\n"
                    "请检查数据库 / 对象存储配置与对应服务是否可达后重试。"
                ),
            )

        run_coro(
            self._loop,
            _validate_and_save(),
            on_success=_saved,
            on_error=_save_failed,
            error_label="validate_backends",
        )

    def _on_apply(self) -> None:
        """保存并应用:后端校验通过后才写 .env + 热重载。

        2026-08-13 交互要求:存储 / 对象存储配置发生变更时,先验证新配置
        (`reconfigure` 内部先建新库 → connect → init_schema,失败上抛),
        全部就绪才落盘 .env 并切换;否则提示用户、.env 保持原样 — 避免
        保存了不可达的 DSN,下次启动 bootstrap 直接挂掉。

        2026-09-07 v1.6.8:成功应用后,若语言字段变更,调
        `install_translator(...)` 重装翻译器并 post LanguageChange 让所
        有 top-level widget 立即重译。
        """
        try:
            e = self._collect()
        except Exception as exc:  # noqa: BLE001 — Qt 槽内异常只打 stderr,用户无感知
            log.exception("collect settings failed")
            QMessageBox.critical(self, self.tr("保存失败"), self.tr(f"读取表单失败: {exc}"))
            return
        # 2026-09-07 v1.6.9:快捷键冲突校验 — 两 action 绑同一键 → 弹
        # warning,保留旧值,不落盘。`find_duplicates` 走 keybinding 模
        # 块的「toString().lower()」比较,与 Qt 自身解析路径一致。
        if hasattr(self, "_keybinding_edits"):
            from tgmonitor.core.keybinding import find_duplicates

            proposed = {
                a: edit.keySequence().toString() for a, edit in self._keybinding_edits.items()
            }
            dupes = find_duplicates(proposed)
            if dupes:
                pretty = ", ".join(f"{a1} ↔ {a2}" for a1, a2 in dupes)
                QMessageBox.warning(
                    self,
                    self.tr("快捷键冲突"),
                    self.tr(f"以下快捷键重复绑定:\n{pretty}\n\n请修改后重试。"),
                )
                return
        errs = e.validate()
        if errs:
            QMessageBox.warning(self, self.tr("校验失败"), "\n".join(errs))
            return
        new_settings = e.to_settings()
        # 2026-09-07 v1.6.8:语言切换标记 — _applied 时据比决定是否装翻译器。
        prev_lang = self._app.settings.lang
        new_lang = new_settings.lang

        async def _validate_and_apply() -> None:
            # reconfigure:存储/对象存储变更时先建新库验证,失败上抛(不落盘)
            await self._app.reconfigure(new_settings)
            # 后端就绪后才写 .env(重启 bootstrap 用的就是这份配置)
            update_env_with_settings(self._env_path, new_settings)

        def _applied(_result: object) -> None:
            # 2026-09-07 v1.6.8:语言切换 → 重装翻译器 + 触发 LanguageChange
            if new_lang != prev_lang:
                qt_app = QApplication.instance()
                if qt_app is not None:
                    install_translator(qt_app, locale=new_lang)
            # 2026-09-07 v1.6.9:快捷键改 → MainWindow 热重绑。`diff_settings`
            # 不看 key_* 字段(纯 UI),reconfigure 走 cheap path 不重 backend,
            # 但 `self.app.settings` 实例已替换,这里直接传新对象。
            main_win = self.window()
            if main_win is not None and hasattr(main_win, "reload_shortcuts"):
                try:
                    main_win.reload_shortcuts(new_settings)
                except Exception:  # noqa: BLE001
                    log.exception("reload_shortcuts failed")
            QMessageBox.information(self, self.tr("已应用"), self.tr("设置已保存并热重载"))

        def _apply_failed(exc: BaseException) -> None:
            if isinstance(exc, OSError):
                QMessageBox.critical(self, self.tr(".env 写入失败"), str(exc))
                return
            QMessageBox.critical(
                self,
                self.tr("保存失败"),
                self.tr(
                    f"后端配置未通过校验,已放弃保存(设置未写入 .env):\n\n{exc}\n\n"
                    "请检查数据库 / 对象存储配置与对应服务是否可达后重试。"
                ),
            )

        run_coro(
            self._loop,
            _validate_and_apply(),
            on_success=_applied,
            on_error=_apply_failed,
            error_label="reconfigure",
        )

    def _on_test_proxy(self) -> None:
        """测试 SOCKS5 代理的 TCP 可达性。"""
        url = self.in_proxy.text().strip()
        if not url:
            QMessageBox.warning(self, self.tr("测试连接"), self.tr("请先填写代理 URL"))
            return
        self.btn_test_proxy.setEnabled(False)
        self.btn_test_proxy.setText(self.tr("测试中…"))

        async def _test() -> str:
            try:
                from urllib.parse import urlparse

                parsed = urlparse(url)
                host = parsed.hostname or "127.0.0.1"
                port = parsed.port or 1080
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=3.0,
                )
                writer.close()
                await writer.wait_closed()
                return self.tr(f"✅ 可达: {host}:{port}")
            except TimeoutError:
                return self.tr("❌ 超时: 3 秒未响应")
            except Exception as exc:
                return self.tr(f"❌ 失败: {exc}")

        def _show(msg: str) -> None:
            """on_success / on_error 共用:恢复按钮 + 弹窗。"""
            self.btn_test_proxy.setEnabled(True)
            self.btn_test_proxy.setText(self.tr("测试连接"))
            QMessageBox.information(self, self.tr("测试结果"), msg)

        run_coro(
            self._loop,
            _test(),
            on_success=_show,
            on_error=lambda e: _show(self.tr(f"❌ 异常: {e}")),
            error_label="test_proxy",
        )

    @staticmethod
    def _set_default(line_edit: QLineEdit, subdir: str) -> None:
        """v1.0.1:把字段重置为 platform-native 默认路径。

        `subdir=""` → 直接是 user_data_dir 本身(`data_root` 用);
        其他(`session` / `messages` / `media`)→ user_data_dir / subdir。
        """
        target = _user_data_dir() / subdir if subdir else _user_data_dir()
        line_edit.setText(str(target))

    # ---- 2026-09-07 v1.6.8:retranslateUi + changeEvent ----
    #
    # 模式:切换语言时,MainWindow 收到 LanguageChange → 转发给所有 stack
    # page(包括 SettingsPage)→ 本类 changeEvent 接住 → 调 retranslateUi
    # 重建所有可见文字。combo / spin 之类需要清空 items 再 add(显示文本
    # 是 tr 出来的),所以一并重建;data 值不变,不影响选择。

    def retranslateUi(self) -> None:  # noqa: N802 — Qt 命名约定
        """2026-09-07 v1.6.8:重新翻译页面所有可见文字。

        关键点:combo items 的显示文本需重建,保留 `currentData()` 不变。
        """
        # 标题
        self.header_label.setText(self.tr("设置"))

        # 底部按钮
        self.btn_save_env.setText(self.tr("仅保存到 .env"))
        self.btn_apply.setText(self.tr("保存并应用"))

        # 语言分组(单独一个组,需要重建 cmb_lang items)
        if hasattr(self, "cmb_lang"):
            cur_lang = self.cmb_lang.currentData() or "zh_CN"
            self.cmb_lang.blockSignals(True)
            self.cmb_lang.clear()
            self.cmb_lang.addItem(self.tr("简体中文"), "zh_CN")
            self.cmb_lang.addItem(self.tr("English"), "en_US")
            idx = self.cmb_lang.findData(cur_lang)
            if idx >= 0:
                self.cmb_lang.setCurrentIndex(idx)
            self.cmb_lang.blockSignals(False)

        # 主题 cmb — 同样需要重建 items
        if hasattr(self, "cmb_theme"):
            from tgmonitor.ui.theme import Theme

            cur_theme = self.cmb_theme.currentData() or Theme.SYSTEM.value
            self.cmb_theme.blockSignals(True)
            self.cmb_theme.clear()
            self.cmb_theme.addItem(self.tr("浅色"), Theme.LIGHT.value)
            self.cmb_theme.addItem(self.tr("暗色"), Theme.DARK.value)
            self.cmb_theme.addItem(self.tr("跟随系统"), Theme.SYSTEM.value)
            idx = self.cmb_theme.findData(cur_theme)
            if idx >= 0:
                self.cmb_theme.setCurrentIndex(idx)
            self.cmb_theme.blockSignals(False)

        # checkbox 文本
        if hasattr(self, "chk_theme_persist"):
            self.chk_theme_persist.setText(
                self.tr("持久化主题(写 .env `TG_KEY_THEME`,重启应用仍生效)")
            )

        # 快捷键帮助 — 2026-09-07 v1.6.9:现已通过下方「快捷键」分组持久化,
        # help_shortcuts 文案简化为「编辑见下方」,不再列硬编码。
        if hasattr(self, "help_shortcuts"):
            self.help_shortcuts.setText(
                self.tr(
                    "快捷键: Ctrl+1..5 切页 · Ctrl+F 搜索 · Ctrl+E 导出 · Ctrl+T 主题切换\n"
                    "        Ctrl+R 刷新频道 · Ctrl+Q 退出 · Ctrl+, 设置 · Esc 取消\n"
                    "        Ctrl+C 复制当前消息文本\n"
                    "\n"
                    "完整可编辑列表见下方「⌨ 快捷键」分组。"
                )
            )

        # 2026-09-07 v1.6.9:快捷键分组的 14 行 label + 提示文本走 tr()
        # 重译。edit widget 本身无 tr() 文本,保留不变。
        if hasattr(self, "_keybinding_labels"):
            for action, lbl in self._keybinding_labels.items():
                lbl.setText(self._keybinding_label(action) + ":")

        # 测试代理按钮文字(可能在 "测试中…" 状态)
        if hasattr(self, "btn_test_proxy"):
            # 若正在测试,保持 "测试中…";否则恢复 "测试连接"。
            # 用 disabled 状态判断——_on_test_proxy 把它 disable 时改成
            # "测试中…",完成后 enable + 改回 "测试连接"。
            if self.btn_test_proxy.isEnabled():
                self.btn_test_proxy.setText(self.tr("测试连接"))
            else:
                self.btn_test_proxy.setText(self.tr("测试中…"))

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 — Qt 命名
        """2026-09-07 v1.6.8:LanguageChange → retranslateUi 全文重译。

        其它事件类型透传给 super。
        """
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)


def _is_child_of(child: QWidget, parent: QWidget) -> bool:
    """检查 child 是否是 parent 的后代。"""
    p = child.parentWidget()
    while p is not None:
        if p is parent:
            return True
        p = p.parentWidget()
    return False
