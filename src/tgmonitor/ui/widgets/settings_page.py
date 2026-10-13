# mypy: disable-error-code="attr-defined"
"""SettingsPage — 整页设置(不再是模态对话框)。

囊括原 settings_dialog.py 的全部配置项 + account_widget.py 的凭据编辑。
以 QScrollArea 内分组排列,底部固定「保存到 .env」+「保存并应用」按钮。

分组(plan-batch2 目标 4:8 个走 FormSection + 2 个保留老路径):
  1. 📱 账户凭证   — API ID / Hash / Phone(FormSection: account)
  2. 🌐 网络代理   — SOCKS5 URL + 测试连接(FormSection: proxy)
  3. 💾 消息存储   — DB 后端 + DSN / 目录(FormSection: storage)
  4. 📁 对象存储   — 后端 + 本地目录 / S3 凭据(FormSection: objectstore)
  5. ⚙️ 策略       — 媒体下载策略(FormSection: policy)
  6. 🔄 同步参数   — chat_delay / page_delay / resume_from_saved(FormSection: sync)
  7. 🌐 语言       — 2026-09-07 v1.6.8 新增(FormSection: language)
  8. 🎨 外观       — 主题 3 选(FormSection: appearance)
  9. ⌨ 快捷键      — 2026-09-07 v1.6.9 新增(**老路径,14 个 QKeySequenceEdit**)
 10. 📦 TDLib 缓存 — 2026-09-24 v1.8.3(**老路径**,文件 / database 大小 + 清理)
 11. 储存按钮栏

## 2026-10-10 plan-batch2 目标 4 重写说明

8 个标 FormSection 的分组用 form_section.py 抽象拼装,2 个保留老路径:

- **cache 保留老路径**:`_cache_label` / `_btn_clear_cache` / `_refresh_cache_label()`
  三个属性测试直接 getattr,以及「off-loop 异步填充 + 动态大小」模式 FormSection
  难表达。Phase C 之后可改 ActionDef,但属性名必保留。
- **keybindings 保留老路径**:14 个 `QKeySequenceEdit` 走 `DEFAULT_BINDINGS` 动态
  循环,`_keybinding_label(action)` 内 14 个字面 `self.tr(<literal>)` 是 lupdate
  静态扫描的关键(挪走会漏抽到翻译)。

i18n 约束:`retranslateUi` / `changeEvent` / 所有 `self.tr(<literal>)` 留在本类
(FormSection 不二次 tr,context 保持 "SettingsPage" — test_i18n_runtime.py:75 守门)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

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
from tgmonitor.ui.async_bridge import run_coro
from tgmonitor.ui.widgets.form_section import (
    ActionDef,
    FieldDef,
    FieldKind,
    FormSection,
    find_form_row,
)

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
        """建 10 个分组(8 FormSection + cache/keybindings 老路径)+ 底部按钮 + 加载当前设置。

        `env_path` 是「保存到 .env」按钮的写入路径(platform-native 由 app.py 注入)。
        """
        super().__init__(parent)
        self._app = app
        self._loop = loop
        self._env_path = env_path
        # 2026-09-24 v1.8.3:TDLib 缓存组 — tdlib 子目录绝对路径,refresh / 清理用
        self._td_dir = app.settings.session_dir / "tdlib"
        # 8 个 FormSection + 持有 self 引用,collect / load 通用循环
        self._sections: list[FormSection] = []

        self._build()
        self._load_from_settings()

    # ============================================================
    # _build 装配 — 8 FormSection + 2 老路径 + 顶部/底部
    # ============================================================

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 固定标题 — 走 tr() 一次性
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

        # 8 个 FormSection(标题已 tr)
        self._build_account(form_root)
        self._build_proxy(form_root)
        self._build_storage(form_root)
        self._build_objectstore(form_root)
        self._build_policy(form_root)
        self._build_sync(form_root)
        self._build_language(form_root)
        self._build_appearance(form_root)
        # 2 个保留老路径(cache / keybindings)
        self._build_keybindings(form_root)
        self._build_cache(form_root)

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

    # ============================================================
    # 8 个 FormSection 装配 — 字段描述 + build
    # ============================================================

    def _build_account(self, root: QVBoxLayout) -> None:
        """📱 账户凭证 — API ID / Hash / Phone / Session 目录。"""
        s = FormSection(
            title=self.tr("📱 账户凭证"),
            fields=(
                FieldDef(
                    key="api_id",
                    kind=FieldKind.INT,
                    label=self.tr("API ID:"),
                    min=0,
                    max=2_000_000_000,
                ),
                FieldDef(
                    key="api_hash",
                    kind=FieldKind.SECRET,
                    label=self.tr("API Hash:"),
                    placeholder=self.tr("32 位 hash · my.telegram.org"),
                ),
                FieldDef(
                    key="phone",
                    kind=FieldKind.TEXT,
                    label=self.tr("手机号:"),
                    placeholder="+8613800000000",
                ),
                FieldDef(
                    key="session_dir",
                    kind=FieldKind.PATH,
                    label=self.tr("Session 目录:"),
                    placeholder=str(_user_data_dir() / "session"),
                    on_default=lambda: self._set_default(self.in_session_dir, "session"),
                    default_tooltip=self.tr("恢复为 platform-native 默认目录"),
                ),
            ),
        )
        gb = s.build(self)
        root.addWidget(gb)
        self._sections.append(s)
        # 保留直接属性引用(测试 + 上层 _on_* 用)
        self.in_api_id = s._widgets["api_id"]
        self.in_api_hash = s._widgets["api_hash"]
        self.in_phone = s._widgets["phone"]
        self.in_session_dir = s._widgets["session_dir"]

    def _build_proxy(self, root: QVBoxLayout) -> None:
        """🌐 网络代理 — SOCKS5 URL + 测试连接按钮(走 ActionDef 双态重译)。

        ActionDef 提供 enabled→「测试连接」/ disabled→「测试中…」自动切。
        """
        s = FormSection(
            title=self.tr("🌐 网络代理 (Proxy)"),
            fields=(
                FieldDef(
                    key="proxy",
                    kind=FieldKind.TEXT,
                    label=self.tr("代理 URL:"),
                    placeholder="socks5://[user:pass@]host:port",
                ),
            ),
            actions=(
                ActionDef(
                    key="test_proxy",
                    label_idle=self.tr("测试连接"),
                    label_busy=self.tr("测试中…"),
                ),
            ),
        )
        gb = s.build(self)
        root.addWidget(gb)
        self._sections.append(s)
        self.in_proxy = s._widgets["proxy"]
        self.btn_test_proxy = s._actions["test_proxy"]
        self.btn_test_proxy.clicked.connect(self._on_test_proxy)

    def _build_storage(self, root: QVBoxLayout) -> None:
        """💾 消息存储 — DB 后端 + DSN。

        删 JSONL 后端后,`db_root` 字段(原 JSONL 目录)不再需要;
        `db_dsn` 永远显(POSTGRES / MONGO 都用 DSN)。
        显隐逻辑 `_on_db_backend_changed` 整个方法删,后端切不再有字段变化。
        """
        s = FormSection(
            title=self.tr("💾 消息存储 (Database)"),
            fields=(
                FieldDef(
                    key="db_backend",
                    kind=FieldKind.CHOICE,
                    label=self.tr("后端:"),
                    options=tuple(DBBackend),
                ),
                FieldDef(
                    key="db_dsn",
                    kind=FieldKind.TEXT,
                    label=self.tr("DSN:"),
                    placeholder="postgresql://user:pass@host/db",
                ),
            ),
        )
        gb = s.build(self)
        root.addWidget(gb)
        self._sections.append(s)

    def _build_objectstore(self, root: QVBoxLayout) -> None:
        """📁 对象存储 — 后端 + 本地目录 / S3 凭据(后端切时显隐)。"""
        s = FormSection(
            title=self.tr("📁 对象存储 (ObjectStore)"),
            fields=(
                FieldDef(
                    key="os_backend",
                    kind=FieldKind.CHOICE,
                    label=self.tr("后端:"),
                    options=tuple(ObjectStoreBackend),
                ),
                FieldDef(
                    key="os_root",
                    kind=FieldKind.PATH,
                    label=self.tr("本地目录:"),
                    placeholder=str(_user_data_dir() / "media"),
                    on_default=lambda: self._set_default(self.in_os_root, "media"),
                    default_tooltip=self.tr("恢复为 platform-native 默认目录"),
                ),
                FieldDef(
                    key="os_endpoint",
                    kind=FieldKind.TEXT,
                    label=self.tr("S3 Endpoint:"),
                    placeholder="https://s3.<region>.amazonaws.com",
                ),
                FieldDef(
                    key="os_region",
                    kind=FieldKind.TEXT,
                    label=self.tr("Region:"),
                    placeholder="us-east-1",
                ),
                FieldDef(
                    key="os_access_key",
                    kind=FieldKind.SECRET,
                    label=self.tr("Access Key:"),
                ),
                FieldDef(
                    key="os_secret_key",
                    kind=FieldKind.SECRET,
                    label=self.tr("Secret Key:"),
                ),
                FieldDef(
                    key="os_bucket",
                    kind=FieldKind.TEXT,
                    label=self.tr("Bucket:"),
                    placeholder="tgmonitor",
                ),
                FieldDef(
                    key="os_s3_hint",
                    kind=FieldKind.STATIC_LABEL,
                    label=self.tr(
                        "提示:AWS 填 s3.<region>.amazonaws.com(留空走默认);"
                        "MinIO / 阿里 OSS 填各自 API 地址;勿填控制台 / 网页地址"
                    ),
                ),
            ),
        )
        gb = s.build(self)
        root.addWidget(gb)
        self._sections.append(s)
        self.cmb_os = s._widgets["os_backend"]
        self.in_os_root = s._widgets["os_root"]
        self.in_os_endpoint = s._widgets["os_endpoint"]
        self.in_os_region = s._widgets["os_region"]
        self.in_os_access_key = s._widgets["os_access_key"]
        self.in_os_secret_key = s._widgets["os_secret_key"]
        self.in_os_bucket = s._widgets["os_bucket"]
        self.lbl_os_s3_hint = s._widgets["os_s3_hint"]
        # 后端切换显隐
        self.cmb_os.currentIndexChanged.connect(self._on_os_backend_changed)
        self._on_os_backend_changed()

    def _build_policy(self, root: QVBoxLayout) -> None:
        """⚙️ 策略 — 媒体下载策略 + 单文件大小 + 数据根目录。"""
        s = FormSection(
            title=self.tr("⚙️ 策略"),
            fields=(
                FieldDef(
                    key="media_policy",
                    kind=FieldKind.CHOICE,
                    label=self.tr("媒体下载:"),
                    options=tuple(MediaPolicy),
                ),
                FieldDef(
                    key="media_max_mb",
                    kind=FieldKind.INT,
                    label=self.tr("单文件大小上限:"),
                    min=0,
                    max=10240,  # 0 = 无限制,10 GB 上限
                    suffix=" MB",
                    single_step=10,
                    tooltip=self.tr(
                        "单文件下载上限。0 = 无限制(慎用,可能下载 GB 级视频把磁盘占满)。"
                    ),
                ),
                FieldDef(
                    key="data_root",
                    kind=FieldKind.PATH,
                    label=self.tr("数据根目录:"),
                    placeholder=str(_user_data_dir()),
                    on_default=lambda: self._set_default(self.in_data_root, ""),
                    default_tooltip=self.tr("恢复为 platform-native 默认目录"),
                ),
            ),
        )
        gb = s.build(self)
        root.addWidget(gb)
        self._sections.append(s)
        self.cmb_media = s._widgets["media_policy"]
        self.in_media_max = s._widgets["media_max_mb"]
        self.in_data_root = s._widgets["data_root"]

    def _build_sync(self, root: QVBoxLayout) -> None:
        """🔄 同步参数 — 频道间隔 / 分页间隔 / 续拉 checkbox。"""
        s = FormSection(
            title=self.tr("🔄 同步参数"),
            fields=(
                FieldDef(
                    key="chat_delay_ms",
                    kind=FieldKind.INT,
                    label=self.tr("频道间间隔:"),
                    min=50,
                    max=60000,
                    suffix=" ms",
                    single_step=50,
                ),
                FieldDef(
                    key="page_delay_ms",
                    kind=FieldKind.INT,
                    label=self.tr("分页间隔:"),
                    min=100,
                    max=60000,
                    suffix=" ms",
                    single_step=100,
                ),
                FieldDef(
                    key="resume_from_saved",
                    kind=FieldKind.BOOL,
                    label=self.tr("续拉(从已保存位置继续)"),
                ),
            ),
        )
        gb = s.build(self)
        root.addWidget(gb)
        self._sections.append(s)
        self.in_chat_delay = s._widgets["chat_delay_ms"]
        self.in_page_delay = s._widgets["page_delay_ms"]
        self.chk_resume = s._widgets["resume_from_saved"]

    def _build_language(self, root: QVBoxLayout) -> None:
        """🌐 语言 — zh_CN / en_US 切换。

        走 choice_pairs 形态 — data = locale code,显示文本 tr() 过。
        retranslateUi 时需重建 items,所以这里手动 addItem(不走 combo_field)
        跟原 _build_language 一致,retranslate 时也清空 addItem。
        """
        g = QGroupBox(self.tr("🌐 语言"))
        fl = QFormLayout(g)
        fl.setSpacing(6)
        self.cmb_lang = QComboBox()
        # 显示文本用 tr() 翻译;data 仍是 locale code(zh_CN / en_US)
        self.cmb_lang.addItem(self.tr("简体中文"), "zh_CN")
        self.cmb_lang.addItem(self.tr("English"), "en_US")
        fl.addRow(self.tr("界面语言:"), self.cmb_lang)

        help_lang = QLabel(self.tr("切换后立即生效,所有界面文字立即重译。重启后保留。"))
        help_lang.setProperty("role", "hint")
        help_lang.setWordWrap(True)
        fl.addRow(help_lang)

        root.addWidget(g)
        # 不入 self._sections(因为 combo items 重建走 retranslateUi 旧路径,
        # 不靠 FormSection.collect 通用化;_collect 显式读 cmb_lang.currentData())

    def _build_appearance(self, root: QVBoxLayout) -> None:
        """🎨 外观 — 主题 3 选 + 持久化 checkbox(checkbox 走 BOOLEAN 老路径)。

        theme cmb 同样需要 retranslate 重建 items — 走原 _build_appearance
        模式,不入 self._sections,checkbox 走 inline QCheckBox 不入 FormSection。
        """
        from tgmonitor.ui.theme import Theme, ThemeManager

        g = QGroupBox(self.tr("🎨 外观"))
        fl = QFormLayout(g)
        fl.setSpacing(6)

        self.cmb_theme = QComboBox()
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
        fl.addRow(self.tr("主题:"), self.cmb_theme)

        self.chk_theme_persist = QCheckBox(
            self.tr("持久化主题(写 .env `TG_KEY_THEME`,重启应用仍生效)")
        )
        self.chk_theme_persist.toggled.connect(self._on_theme_persist_toggled)
        fl.addRow("", self.chk_theme_persist)

        self.help_shortcuts = QLabel(
            self.tr(
                "快捷键: Ctrl+1..5 切页 · Ctrl+F 搜索 · Ctrl+E 导出 · Ctrl+T 主题切换\n"
                "        Ctrl+R 刷新频道 · Ctrl+Q 退出 · Ctrl+, 设置 · Esc 取消\n"
                "        Ctrl+C 复制当前消息文本\n"
                "\n"
                "快捷键为 session 内绑定,重启后不持久化(在 v1.5.0 已实装)。"
            )
        )
        self.help_shortcuts.setObjectName("helpText")
        self.help_shortcuts.setWordWrap(True)
        fl.addRow(self.help_shortcuts)

        root.addWidget(g)
        # 不入 self._sections(cmbs / checkbox / hint 都走 retranslateUi 旧路径)

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

    # ============================================================
    # 2 个保留老路径 — cache / keybindings
    # ============================================================

    def _build_keybindings(self, root: QVBoxLayout) -> None:
        """2026-09-07 v1.6.9:⌨ 快捷键 — 14 个 QKeySequenceEdit 走老路径。

        原因:`_keybinding_label(action)` 14 个字面 `self.tr(<literal>)` 是
        lupdate 静态扫描的关键,挪走会漏抽;且 `find_duplicates` 校验读
        `self._keybinding_edits` 字典 — 保留原结构最简单。
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
            edit.setKeySequence(
                QKeySequence(settings_val) if settings_val else QKeySequence(default_for(action))
            )
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

    def _build_cache(self, root: QVBoxLayout) -> None:
        """2026-09-24 v1.8.3:📦 TDLib 缓存 — 走老路径,属性名测试直接读。

        `_cache_label` / `_btn_clear_cache` / `_refresh_cache_label()` 必保留。
        """
        g = QGroupBox(self.tr("📦 TDLib 缓存"))
        f = QFormLayout(g)
        f.setSpacing(6)

        # 当前占用 label — 初始占位「计算中…」
        self._cache_label = QLabel(self.tr("计算中…"))
        self._cache_label.setObjectName("cacheSizeLabel")
        f.addRow(self.tr("当前占用:"), self._cache_label)

        # 按钮 — 同一行右侧
        self._btn_clear_cache = QPushButton(self.tr("🧹 清理缓存"))
        self._btn_clear_cache.setObjectName("btnClearCache")
        self._btn_clear_cache.setToolTip(
            self.tr(
                "删除 TDLib 文件分片缓存 + 触发 TDLib optimizeStorage 压缩数据库。"
                "session/auth_key 不丢,无需重新登录。"
            )
        )
        self._btn_clear_cache.clicked.connect(self._on_clear_cache)
        f.addRow(self.tr("操作:"), self._btn_clear_cache)

        root.addWidget(g)

        # 首次填充 label(异步)
        self._refresh_cache_label()

    def _refresh_cache_label(self) -> None:
        """2026-09-24 v1.8.3:off-loop 计算 files/database 子目录大小,setText。

        走 `asyncio.to_thread` 避免大目录 stat 阻塞 qasync 事件循环。
        失败时显示「(无法读取)」,不弹窗(用户没主动操作,静默降级)。
        """
        td_dir = self._td_dir

        async def _go() -> str:
            from tgmonitor.core._fs_utils import dir_size, format_bytes

            files = await asyncio.to_thread(dir_size, td_dir / "files")
            db = await asyncio.to_thread(dir_size, td_dir / "database")
            return f"files: {format_bytes(files)} / database: {format_bytes(db)}"

        def _done(text: str) -> None:
            self._cache_label.setText(text)

        run_coro(
            self._loop,
            _go(),
            on_success=_done,
            on_error=lambda e: self._cache_label.setText(self.tr("(无法读取)")),
            error_label="cache_size_refresh",
        )

    def _on_clear_cache(self) -> None:
        """2026-09-24 v1.8.3:清理缓存按钮 slot,触发两动作 + 弹窗反馈。"""
        self._btn_clear_cache.setEnabled(False)
        self._cache_label.setText(self.tr("清理中…"))

        client = self._app.client

        async def _go() -> tuple[int, int]:
            freed_files = await client.clean_files()
            freed_db = await client.optimize_storage()
            return freed_files, freed_db

        def _done(result: tuple[int, int]) -> None:
            from tgmonitor.core._fs_utils import format_bytes

            f, d = result
            QMessageBox.information(
                self,
                self.tr("清理完成"),
                self.tr("已释放:files {0}(optimizeStorage 报告 {1})").format(
                    format_bytes(f), format_bytes(d)
                ),
            )
            self._refresh_cache_label()
            self._btn_clear_cache.setEnabled(True)

        def _fail(exc: BaseException) -> None:
            log.exception("cache clear failed")
            QMessageBox.critical(
                self,
                self.tr("清理失败"),
                self.tr("操作未完成:{0}").format(exc),
            )
            self._refresh_cache_label()
            self._btn_clear_cache.setEnabled(True)

        run_coro(
            self._loop,
            _go(),
            on_success=_done,
            on_error=_fail,
            error_label="cache_clear",
        )

    # ============================================================
    # 后端切换显隐 — 用 form_section.find_form_row 走跨 group 隔离
    # ============================================================
    # DB 后端切(POSTGRES ↔ MONGO)不再有字段显隐变化(都用 DSN),
    # `_on_db_backend_changed` 整个方法删除。OS 后端切仍需(本地/S3 字段
    # 互斥),保留 `_on_os_backend_changed`。

    def _on_os_backend_changed(self) -> None:
        is_local = self.cmb_os.currentData() in (
            ObjectStoreBackend.LOCAL,
            ObjectStoreBackend.FOLDER,
        )
        is_s3 = self.cmb_os.currentData() == ObjectStoreBackend.S3
        # 本地目录:local/folder 时显示
        self._set_field_visible("os_root", is_local)
        # S3 字段 + 提示:S3 时显示
        for key in (
            "os_endpoint",
            "os_region",
            "os_access_key",
            "os_secret_key",
            "os_bucket",
            "os_s3_hint",
        ):
            self._set_field_visible(key, is_s3)

    def _set_field_visible(self, key: str, visible: bool) -> None:
        """跨整个 page 找 key 对应 row,显隐。走 form_section.find_form_row 隔离。"""
        hit = find_form_row(self, key)
        if hit is None:
            return
        gb, idx = hit
        form = gb.findChild(QFormLayout)
        if form is None or idx >= form.rowCount():
            return
        for role in (QFormLayout.LabelRole, QFormLayout.FieldRole):
            item = form.itemAt(idx, role)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.setVisible(visible)

    # ============================================================
    # 存/取 — 通用化:section.collect() + 老路径特判
    # ============================================================

    def _collect(self) -> EditableSettings:
        """收集当前表单值 → EditableSettings。

        8 个 FormSection 走 section.collect() 通用循环;lang / theme / keybindings
        走老路径(因为 cmb_lang / cmb_theme items 重建不在 FormSection 控制;
        keybindings 是 dict 持有)。
        """
        ud = _user_data_dir()
        # 聚合 8 section 的字段
        collected: dict[str, Any] = {}
        for sec in self._sections:
            collected.update(sec.collect())

        # lang 走老路径 — cmb_lang.data() 是 locale code,不依赖显示文本
        lang_value = self.cmb_lang.currentData() or "zh_CN"
        # 14 个 key_<action> — 走 _keybinding_edits
        kb = {a: e.keySequence().toString() for a, e in self._keybinding_edits.items()}

        # 字段名映射:FormSection 用的 key → EditableSettings 字段名
        # (e.g. "db_backend" → "db_backend"; "chat_delay_ms" → "sync_chat_delay_ms")
        return EditableSettings(
            api_id=int(collected.get("api_id", 0)),
            api_hash=str(collected.get("api_hash", "")).strip(),
            phone=str(collected.get("phone", "")).strip(),
            session_dir=str(collected.get("session_dir", "")).strip() or str(ud / "session"),
            db_backend=collected.get("db_backend", DBBackend.POSTGRES),
            db_dsn=str(collected.get("db_dsn", "")).strip(),
            # 2026-10-12 plan-batch3 目标 3 step 3.4:db_root 字段已删,
            # EditableSettings.to_settings() 不再传 db_root=Path(...),
            # Settings.db_root 走 pydantic default_factory 兜底。
            objectstore_backend=collected.get("os_backend", ObjectStoreBackend.LOCAL),
            objectstore_root=str(collected.get("os_root", "")).strip() or str(ud / "media"),
            objectstore_endpoint=str(collected.get("os_endpoint", "")).strip(),
            objectstore_region=str(collected.get("os_region", "")).strip() or "us-east-1",
            objectstore_access_key=str(collected.get("os_access_key", "")).strip(),
            objectstore_secret_key=str(collected.get("os_secret_key", "")).strip(),
            objectstore_bucket=str(collected.get("os_bucket", "")).strip() or "tgmonitor",
            media_policy=collected.get("media_policy", MediaPolicy.THUMBNAIL),
            media_max_mb=int(collected.get("media_max_mb", 200)),
            data_root=str(collected.get("data_root", "")).strip() or str(ud),
            proxy=str(collected.get("proxy", "")).strip(),
            sync_chat_delay_ms=int(collected.get("chat_delay_ms", 500)),
            sync_page_delay_ms=int(collected.get("page_delay_ms", 1000)),
            sync_resume_from_saved=bool(collected.get("resume_from_saved", True)),
            lang=lang_value,
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
        """从 app.settings 加载当前值到表单。

        8 个 FormSection 走通用 load();lang / theme_persist / keybindings 走老路径。
        """
        s = self._app.settings

        # 8 section 通用 load
        section_values = {
            # account
            "api_id": s.api_id,
            "api_hash": s.api_hash,
            "phone": s.phone,
            "session_dir": str(s.session_dir),
            # proxy
            "proxy": s.proxy or "",
            # storage
            "db_backend": s.db_backend,
            "db_dsn": s.db_dsn or "",
            # 2026-10-12 plan-batch3 目标 3 step 3.4:db_root 字段已删,
            # UI 不再编辑/显示
            # objectstore
            "os_backend": s.objectstore_backend,
            "os_root": str(s.objectstore_root),
            "os_endpoint": s.objectstore_endpoint or "",
            "os_region": s.objectstore_region or "",
            "os_access_key": s.objectstore_access_key or "",
            "os_secret_key": s.objectstore_secret_key or "",
            "os_bucket": s.objectstore_bucket or "",
            # policy
            "media_policy": s.media_policy,
            "media_max_mb": s.media_max_bytes // (1024 * 1024),
            "data_root": str(s.data_root),
            # sync
            "chat_delay_ms": s.sync_chat_delay_ms,
            "page_delay_ms": s.sync_page_delay_ms,
            "resume_from_saved": s.sync_resume_from_saved,
        }
        for sec in self._sections:
            sec.load(section_values)

        # theme persist checkbox — 主题不是 FormSection 字段(走 cmb_theme)
        self.chk_theme_persist.setChecked(bool(s.key_theme))

        # lang 回填 — findData 走 locale code,不依赖显示文本
        idx_lang = self.cmb_lang.findData(s.lang)
        if idx_lang >= 0:
            self.cmb_lang.setCurrentIndex(idx_lang)

        # keybindings 回填
        from tgmonitor.core.keybinding import binding_for

        for action, edit in self._keybinding_edits.items():
            settings_val = getattr(s, f"key_{action}", "")
            edit.setKeySequence(QKeySequence(binding_for(action, settings_val)))

    # ============================================================
    # 槽 — 跟原版一致(行为零变化)
    # ============================================================

    def _on_save_env(self) -> None:
        """仅写 .env,不热重载;写前同样做后端连通性校验。"""
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
        """保存并应用:后端校验通过后才写 .env + 热重载。"""
        try:
            e = self._collect()
        except Exception as exc:  # noqa: BLE001 — Qt 槽内异常只打 stderr,用户无感知
            log.exception("collect settings failed")
            QMessageBox.critical(self, self.tr("保存失败"), self.tr(f"读取表单失败: {exc}"))
            return
        # 2026-09-07 v1.6.9:快捷键冲突校验
        from tgmonitor.core.keybinding import find_duplicates

        proposed = {a: edit.keySequence().toString() for a, edit in self._keybinding_edits.items()}
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
            # 2026-09-07 v1.6.9:快捷键改 → MainWindow 热重绑
            main_win = self.window()
            # noqa: BLE001 — capability-probe,reload_shortcuts 是 MainWindow 可选扩展能力(测试注入 fake 不一定有)
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
    def _set_default(line_edit: QWidget, subdir: str) -> None:
        """v1.0.1:把字段重置为 platform-native 默认路径。

        `subdir=""` → 直接是 user_data_dir 本身(`data_root` 用);
        其他(`session` / `messages` / `media`)→ user_data_dir / subdir。

        `line_edit` 形参收 QWidget(原 QLineEdit)— FormSection 改造后
        path 字段的 `_widgets[key]` 是 path_field 的 wrapper QWidget,内部
        才包 QLineEdit。这里用 `findChild(QLineEdit)` 反向拿;若 caller
        传的是裸 QLineEdit,findChild 找自己返 self,等价。
        """
        target = _user_data_dir() / subdir if subdir else _user_data_dir()
        inner = line_edit.findChild(QLineEdit)
        if inner is None and isinstance(line_edit, QLineEdit):
            inner = line_edit
        if inner is not None:
            inner.setText(str(target))

    # ============================================================
    # 2026-09-07 v1.6.8:retranslateUi + changeEvent
    # ============================================================
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

        # 8 个 FormSection 走通用 retranslate(刷 groupbox title + action 按钮双态)
        for sec in self._sections:
            sec.retranslate()

        # 语言分组(单独一个组,需要重建 cmb_lang items)
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

        # 主题持久化 checkbox
        self.chk_theme_persist.setText(self.tr("持久化主题(写 .env `TG_KEY_THEME`,重启应用仍生效)"))

        # 快捷键帮助 — help_shortcuts 整体刷
        self.help_shortcuts.setText(
            self.tr(
                "快捷键: Ctrl+1..5 切页 · Ctrl+F 搜索 · Ctrl+E 导出 · Ctrl+T 主题切换\n"
                "        Ctrl+R 刷新频道 · Ctrl+Q 退出 · Ctrl+, 设置 · Esc 取消\n"
                "        Ctrl+C 复制当前消息文本\n"
                "\n"
                "快捷键为 session 内绑定,重启后不持久化(在 v1.5.0 已实装)。"
            )
        )

        # 14 行 keybinding label 文字(走 tr() 重译,edit widget 自身无 tr() 文本,保留)
        for action, lbl in self._keybinding_labels.items():
            lbl.setText(self._keybinding_label(action) + ":")

        # 测试代理按钮(双态 — ActionDef 已刷过 idle/busy,这里再确认 enabled 时 idle)
        if self.btn_test_proxy.isEnabled():
            self.btn_test_proxy.setText(self.tr("测试连接"))
        else:
            self.btn_test_proxy.setText(self.tr("测试中…"))

        # TDLib 缓存按钮(双态)
        if self._btn_clear_cache.isEnabled():
            self._btn_clear_cache.setText(self.tr("🧹 清理缓存"))
        else:
            self._btn_clear_cache.setText(self.tr("🧹 清理中…"))
        # 静默刷新:retranslate 后大小仍是真实数字,无需弹窗
        self._refresh_cache_label()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 — Qt 命名
        """2026-09-07 v1.6.8:LanguageChange → retranslateUi 全文重译。

        其它事件类型透传给 super。
        """
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)
