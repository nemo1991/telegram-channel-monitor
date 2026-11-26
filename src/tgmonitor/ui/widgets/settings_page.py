# mypy: disable-error-code="attr-defined"
"""SettingsPage — 整页设置(不再是模态对话框)。

囊括原 settings_dialog.py 的全部配置项 + account_widget.py 的凭据编辑。
以 QScrollArea 内分组排列,底部固定「保存」按钮(v2.0.7 FR-09)。

2026-11-17 v2.0.3 D3:登录/登出入口从右上 HeaderBar 迁到「账户凭证」
分组,头栏退化为纯标题。
2026-11-18 v2.0.6:独立频道页撤销,ChannelWidget 单实例迁入本页
「监听频道管理」分组(不走 FormSection,白名单进 core 存储不写 .env)。
2026-11-21:手机号+验证码登录因下发渠道问题在本版本移除,登录方式只保留
二维码扫码(QrLoginDialog);phone 字段及 .env 的 TG_PHONE 一并删除。

7 个分组(前 6 个走 FormSection,监听频道为自定义 QGroupBox):
  1. 账户凭证       — API ID / Hash / Session 目录 + 二维码登录/登出入口
  2. 监听频道管理   — 已加入 / 已监听双栏 + 搜索 + 全量同步(2.0.6 迁入)
  3. 网络代理       — SOCKS5 URL + 测试连接
  4. 消息存储       — DB 后端 + DSN / 目录
  5. 对象存储       — 后端 + 本地目录 / S3 凭据
  6. 策略           — 媒体下载策略
  7. 同步参数       — chat_delay / page_delay / resume_from_saved
  底部固定按钮栏
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend, _user_data_dir
from tgmonitor.core.ops_gate import READY_STATE
from tgmonitor.core.settings_store import (
    EditableSettings,
    update_env_credentials,
    update_env_with_settings,
)
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
    from tgmonitor.ui.widgets.channel_widget import ChannelWidget

log = logging.getLogger(__name__)


class SettingsPage(QWidget):
    """整页设置。在 QStackedWidget 中作为一页使用。

    构造后自动从 app.settings 加载当前值。
    UI 改动不实时生效 — 用户点「保存」才写盘并热重载(v2.0.7 FR-09)。
    """

    # run_coro 的回调可能跑在后台 loop 线程 —— 只 emit,由 Qt 排到主线程槽
    # 里再弹窗 / exec 对话框,避免跨线程碰 widget(项目既有跨线程约定)。
    login_failed = Signal(str)
    qr_request_ok = Signal(str)  # payload = 二维码 URL(tg://login?token=…)

    def __init__(
        self,
        app: AppService,
        loop: asyncio.AbstractEventLoop,
        env_path: Path,
        parent: QWidget | None = None,
        channel_panel: ChannelWidget | None = None,
    ) -> None:
        """建 6 个 FormSection + 1 个监听分组 + 底部按钮 + 加载当前设置。

        `env_path` 是「保存」写入的 platform-native .env 路径(由 app.py 注入)。
        `channel_panel` 是 2.0.6 从频道页迁入的监听频道控件(单实例 reparent,
        MainWindow 仍持引用);不经 FormSection,不参与 collect() 写 .env。
        """
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self._app = app
        self._loop = loop
        self._env_path = env_path
        self._channel_panel = channel_panel
        # 6 个 FormSection + 持有 self 引用,collect / load 通用循环
        self._sections: list[FormSection] = []
        # 2026-11-24 v2.0.10 issue #131:左侧分组导航 → 右侧明细切换。
        # 每项 (标题, 分组 widget, 搜索命中文本);标题/字段标签/key 全入索引。
        self._nav_entries: list[tuple[str, QWidget, str]] = []

        self._build()
        self._load_from_settings()
        self.login_failed.connect(self._show_login_failed)
        self.qr_request_ok.connect(self._open_qr_dialog)
        # 初始按 core 真实状态设置入口可见性;桩环境 client.state 非字符串
        # 时退化为未登录(不把 MagicMock 当状态字符串显示)。
        initial_state = getattr(self._app.client, "state", None)
        self.set_login_state(initial_state if isinstance(initial_state, str) else "uninit")

    # ============================================================
    # _build 装配 — 6 FormSection + 顶部/底部
    # ============================================================

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # v2.0.7 FR-04:页面级「设置」标题删除(导航已指明当前页),
        # 顶部空间直接给第一张卡片。全局 #pageTitle QSS 规则保留给其它页。

        # 2026-11-24 v2.0.10 issue #131:左侧分组导航 + 搜索,右侧明细。
        # 左侧导航只做「显隐切换」——7 个分组仍是原 groupbox,全部留同一
        # scroll_content 里;不新增 QGroupBox(布局测试硬断言恰好 7 个)。
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        nav_pane = QWidget()
        nav_pane.setObjectName("settingsNavPane")
        nav_layout = QVBoxLayout(nav_pane)
        nav_layout.setContentsMargins(12, 16, 8, 16)
        nav_layout.setSpacing(10)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("settingsSearchInput")
        self.search_edit.setPlaceholderText(self.tr("搜索设置项…"))
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search_text)
        nav_layout.addWidget(self.search_edit)

        self.nav_list = QListWidget()
        self.nav_list.setObjectName("settingsNav")
        self.nav_list.currentRowChanged.connect(self._on_nav_row)
        nav_layout.addWidget(self.nav_list, 1)
        body.addWidget(nav_pane)

        detail = QWidget()
        detail.setObjectName("settingsDetailPane")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(0)

        # 滚动区域(现挂在右侧 detail 内)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("settingsScroll")

        scroll_content = QWidget()
        scroll_content.setObjectName("settingsScrollContent")
        form_root = QVBoxLayout(scroll_content)
        form_root.setContentsMargins(24, 16, 24, 24)
        form_root.setSpacing(16)

        # 6 个 FormSection(标题已 tr)
        self._build_account(form_root)
        # 2.0.6:监听频道管理从独立频道页迁入设置页(单实例,不进 FormSection)。
        # channel_panel 缺省时跳过分组 —— 保持独立 SettingsPage 单测可用。
        if self._channel_panel is not None:
            self._build_channel_monitor(form_root)
        self._build_proxy(form_root)
        self._build_storage(form_root)
        self._build_objectstore(form_root)
        self._build_policy(form_root)
        self._build_sync(form_root)

        # 尾部 stretch 保留:其它分组按 sizeHint 紧凑排列,余量落到这里。
        # 监听分组单独带大 stretch(见 _build_channel_monitor),选中时几乎
        # 吃掉全部余量,只留小段间距到按钮栏。
        form_root.addStretch(1)
        scroll.setWidget(scroll_content)
        detail_layout.addWidget(scroll, 1)
        body.addWidget(detail, 1)
        root.addLayout(body, 1)

        # 导航条目统一登记(顺序 = 视觉顺序):6 个 FormSection + 监听分组。
        # 监听分组插在第 2 项(账户之后),与 form_root 里的实际排列一致。
        self._register_form_section(self._sections[0])  # 账户凭证
        if self._channel_panel is not None:
            self._register_nav_entry(
                self._channel_gb.title(),
                self._channel_gb,
                "频道 白名单 订阅 监听 同步",
            )
        for section in self._sections[1:]:
            self._register_form_section(section)
        for title, _, _ in self._nav_entries:
            self.nav_list.addItem(QListWidgetItem(title))
        self.nav_list.setCurrentRow(0)  # 默认显示首组「账户凭证」

        # 后端字段初始显隐 — 必须在此(scroll.setWidget 已把 scroll_content
        # parent 到 self,find_form_row 才找得到各 groupbox)。build 期调
        # _on_*_backend_changed 是 no-op;且默认值恰为 combo index 0 时
        # _load_from_settings 的 setCurrentIndex 不 fire 信号,故统一在此刷一次。
        self._on_db_backend_changed()
        self._on_os_backend_changed()

        # 底部固定按钮 — v2.0.7 FR-09:「仅保存到 .env」删除,
        # 只保留「保存」(写盘 + 热重载),靠 stretch 推到右下。
        # 2026-11-24 v2.0.10 修正:按钮栏挂在 detail_layout(右侧内容区)而非
        # root —— 否则它独占 root 底部一行,把左侧导航压短、末尾露出灰底断层。
        btn_bar = QHBoxLayout()
        btn_bar.setContentsMargins(24, 12, 24, 16)

        btn_bar.addStretch(1)

        self.btn_apply = QPushButton(self.tr("保存"))
        self.btn_apply.setObjectName("primaryBtn")
        self.btn_apply.clicked.connect(self._on_apply)
        btn_bar.addWidget(self.btn_apply)

        detail_layout.addLayout(btn_bar)

    # ============================================================
    # 左侧导航 — 分组显隐 + 搜索过滤
    # ============================================================

    def _register_nav_entry(self, title: str, widget: QWidget, *keywords: str) -> None:
        """登记一个分组到左侧导航:标题 + 额外关键词(字段标签/key)供搜索。"""
        haystack = " ".join((title, *keywords)).lower()
        self._nav_entries.append((title, widget, haystack))

    def _register_form_section(self, section: FormSection) -> None:
        """FormSection 自动登记:标题 + 各字段 label / key + action label。"""
        gb = section._groupbox
        assert gb is not None
        keywords = [token for field in section.fields for token in (field.label, field.key)]
        keywords.extend(action.label_idle for action in section.actions)
        self._register_nav_entry(section.title, gb, *keywords)

    def _on_nav_row(self, row: int) -> None:
        """切换右侧明细:只显示选中分组,其余 setVisible(False)。

        只切 groupbox 顶层可见性 —— 不动字段行的 `set_visible`(后端互斥显隐
        由 `_set_field_visible` 独立管理),避免两套显隐逻辑打架。
        """
        if row < 0 or row >= len(self._nav_entries):
            return
        for idx, (_, widget, _) in enumerate(self._nav_entries):
            widget.setVisible(idx == row)

    def _on_search_text(self, text: str) -> None:
        """按关键词过滤导航条目;当前项被过滤掉时自动跳到首个可见项。"""
        q = text.strip().lower()
        first_visible = -1
        current = self.nav_list.currentRow()
        for row, (_, _, haystack) in enumerate(self._nav_entries):
            matched = not q or q in haystack
            self.nav_list.item(row).setHidden(not matched)
            if matched and first_visible < 0:
                first_visible = row
        current_item = self.nav_list.item(current)
        if q and first_visible >= 0 and current_item is not None and current_item.isHidden():
            self.nav_list.setCurrentRow(first_visible)

    # ============================================================
    # 6 个 FormSection 装配 — 字段描述 + build
    # ============================================================

    def _build_account(self, root: QVBoxLayout) -> None:
        """账户凭证 — API ID / Hash / Phone / Session 目录。"""
        s = FormSection(
            title=self.tr("账户凭证"),
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
                    key="session_dir",
                    kind=FieldKind.PATH,
                    label=self.tr("Session 目录:"),
                    placeholder=str(_user_data_dir() / "session"),
                    # 2.0.8:删除「默认」按钮 —— 该行的位置让给二维码登录/登出入口
                    # (见 _build_account 末尾按钮注入)。
                ),
            ),
        )
        gb = s.build(self)
        root.addWidget(gb)
        self._sections.append(s)
        # 2026-11-17 v2.0.4 FR-02:登录/登出入口挂在「账户凭证」分组内部,
        # 不再是 form_root 上与 groupbox 平级的兄弟节点(原先浮在块外左下)。
        # 用 gb.layout() (FormSection.build 内部是 QVBoxLayout) 追加按钮行。
        self._account_gb = gb
        # 保留直接属性引用(测试 + 上层 _on_* 用)
        self.in_api_id = s._widgets["api_id"]
        self.in_api_hash = s._widgets["api_hash"]
        self.in_session_dir = s._widgets["session_dir"]

        # 2026-11-19 v2.0.8 FR-01:登录/登出入口放到「Session 目录」行的
        # 「默认」按钮位置 —— 直接注入 path_field 行内的 QHBoxLayout(浏览…之后)。
        # 2026-11-21:登录只保留二维码扫码一种方式,删掉手机号+验证码「登录」按钮。
        # 两个按钮常驻(由 set_login_state 控制显隐),不经 FormSection ——
        # 它们不参与 _collect()/保存到 .env。
        #
        # 注意:path_field 的 `_widgets["session_dir"]` 是内层 QLineEdit,
        # 其 parent 是承载 [edit | 浏览…] 的 row_w。取该 row_w 的 QHBoxLayout
        # 追加即可,不需要额外 addLayout 到分组底部。
        self.btn_qr_login = QPushButton(self.tr("二维码登录"))
        self.btn_qr_login.setObjectName("settingsQrLoginBtn")
        self.btn_qr_login.clicked.connect(self._on_qr_login_clicked)
        self.btn_logout = QPushButton(self.tr("登出"))
        self.btn_logout.setObjectName("settingsLogoutBtn")
        self.btn_logout.clicked.connect(self._on_logout_clicked)

        session_edit = self.in_session_dir
        session_row_w = session_edit.parentWidget()
        assert session_row_w is not None  # path_field 必把 edit 收进 row_w
        session_row = session_row_w.layout()
        assert isinstance(session_row, QHBoxLayout)  # path_field 用 QHBoxLayout
        session_row.addWidget(self.btn_qr_login)
        session_row.addWidget(self.btn_logout)

    def _build_channel_monitor(self, root: QVBoxLayout) -> None:
        """监听频道管理 — 从频道页迁入的 ChannelWidget 单实例(2.0.6)。

        不走 FormSection:白名单写 core 存储,不进 EditableSettings/.env;
        `collect()` 通用循环不覆盖它。用 QGroupBox 与原各组视觉对齐。
        """
        panel = self._channel_panel
        assert panel is not None  # 调用方已判空
        box = QGroupBox(self.tr("监听频道管理"))
        box.setObjectName("settingsChannelMonitorGroup")
        box_layout = QVBoxLayout(box)
        # v2.0.7 FR-05/FR-10:与 FormSection 卡片同款内边距(顶部 40px 给标题)。
        box_layout.setContentsMargins(16, 40, 16, 16)
        box_layout.setSpacing(12)
        box_layout.addWidget(panel, 1)
        # 最小高度兜底小窗口;垂直 Expanding + 大 stretch 让选中本组时
        # 几乎吃掉 form_root 尾部 stretch 之外的全部余量,只留约 40px 到按钮栏。
        box.setMinimumHeight(560)
        box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        root.addWidget(box, 100)
        self._channel_gb = box  # 导航注册用(2.0.10)

    @property
    def channel_panel(self) -> ChannelWidget | None:
        """迁入的监听频道控件(单实例,MainWindow/ctx 亦持同一引用)。"""
        return self._channel_panel

    def set_login_state(self, state: str) -> None:
        """按 core 门控状态切换登录/登出入口(UI 适配,不重复 ready 判定)。

        与 `AppStateController` 的两条登录态路径(VM signal / EventBus)
        对接;真正的执行安全仍由 core 域服务入口保证。
        """
        logged_in = state == READY_STATE
        self.btn_qr_login.setVisible(not logged_in)
        self.btn_qr_login.setEnabled(not logged_in)
        self.btn_logout.setVisible(logged_in)
        self.btn_logout.setEnabled(logged_in)

    def _on_qr_login_clicked(self) -> None:
        """设置页「二维码登录」— 只校验/保存 api 凭据 → 生成二维码 → 弹扫码框。

        本版本唯一的登录方式:靠已登录的 Telegram App 扫码授权,凭据写盘
        api_id/api_hash 这 2 个 key。
        """
        api_id = int(self.in_api_id.value())
        api_hash = self.in_api_hash.text().strip()

        err = self._validate_login_credentials(api_id, api_hash)
        if err:
            QMessageBox.warning(self, self.tr("无法二维码登录"), err)
            return

        try:
            update_env_credentials(
                self._env_path,
                api_id=api_id,
                api_hash=api_hash,
            )
        except OSError as exc:
            QMessageBox.critical(self, self.tr("凭据保存失败"), str(exc))
            return
        current = self._app.settings
        current.api_id = api_id
        current.api_hash = api_hash

        self.btn_qr_login.setEnabled(False)
        self.btn_qr_login.setText(self.tr("生成中"))

        def _on_generated(result: tuple[str, str | None]) -> None:
            self.btn_qr_login.setText(self.tr("二维码登录"))
            self.btn_qr_login.setEnabled(True)
            state, detail = result
            if state == "error":
                self.login_failed.emit(detail or self.tr("生成二维码失败"))
            elif state == "qr_required" and detail:
                self.qr_request_ok.emit(detail)
            else:
                self.login_failed.emit(detail or self.tr(f"生成二维码失败(状态: {state})"))

        def _on_error(exc: BaseException) -> None:
            self.btn_qr_login.setText(self.tr("二维码登录"))
            self.btn_qr_login.setEnabled(True)
            self.login_failed.emit(str(exc))

        run_coro(
            self._loop,
            self._app.auth.start_qr_login(),
            on_success=_on_generated,
            on_error=_on_error,
            error_label="login_qr_generate",
        )

    @staticmethod
    def _validate_login_credentials(api_id: int, api_hash: str) -> str:
        """返回第一条错误消息;空串 = 校验通过。

        二维码登录只依赖 api_id/api_hash,与 `AuthService._check_credentials`
        / `_missing_credentials` 同一套规则;在 UI 层同步执行,便于点登录时
        先提示用户、不发起无效网络请求。
        """
        if api_id <= 0:
            return "TG_API_ID 未配置:请打开 设置… 填写正整数"
        if len(api_hash) < 16:
            return "TG_API_HASH 未配置或过短(需 ≥ 16 位):请打开 设置… 填写"
        return ""

    def _open_qr_dialog(self, url: str) -> None:
        """二维码生成成功 → 主线程弹扫码对话框(登录成功自动关闭)。"""
        from tgmonitor.ui.widgets.qr_login_dialog import QrLoginDialog

        dlg = QrLoginDialog(self._app, self._loop, url, self.window())
        dlg.exec()
        # 扫码成功后 core 状态转 ready —— 走 EventBus → AppStateController
        # 自动刷新入口显隐;这里兜底手动同步一次(exec 返回时可能事件已到)。
        try:
            state = self._app.client.state
            if isinstance(state, str):
                self.set_login_state(state)
        except Exception:
            log.debug("sync login state after QR dialog failed", exc_info=True)

    def _show_login_failed(self, msg: str) -> None:
        """发码失败 → 主线程明确提示,用户可改凭据后重试。"""
        QMessageBox.warning(self, self.tr("登录失败"), msg)

    def _on_logout_clicked(self) -> None:
        """设置页「登出」— 复用 app.auth.logout()。"""
        run_coro(self._loop, self._app.auth.logout(), error_label="logout")

    def _build_proxy(self, root: QVBoxLayout) -> None:
        """网络代理 — SOCKS5 URL + 测试连接按钮(走 ActionDef 双态重译)。

        ActionDef 提供 enabled→「测试连接」/ disabled→「测试中…」自动切。
        """
        s = FormSection(
            title=self.tr("网络代理 (Proxy)"),
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
        """消息存储 — DB 后端 + DSN(PG)/ SQLite 文件路径。

        字段显隐随后端切:POSTGRES 显 `db_dsn`,SQLITE 显 `sqlite_path`
        (见 `_on_db_backend_changed`,镜像 os_backend 的本地/S3 互斥模式)。
        """
        s = FormSection(
            title=self.tr("消息存储 (Database)"),
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
                FieldDef(
                    key="sqlite_path",
                    kind=FieldKind.PATH,
                    label=self.tr("SQLite 文件:"),
                    placeholder=str(_user_data_dir() / "tgmonitor.db"),
                    file_mode=True,
                    on_default=lambda: self._set_default(self.in_sqlite_path, "tgmonitor.db"),
                    default_tooltip=self.tr("恢复为 platform-native 默认路径"),
                ),
            ),
        )
        gb = s.build(self)
        root.addWidget(gb)
        self._sections.append(s)
        self.cmb_db = s._widgets["db_backend"]
        self.in_sqlite_path = s._widgets["sqlite_path"]
        # 后端切换显隐(DSN 仅 PG / SQLite 文件仅 SQLite)。初始显隐在 _build()
        # 末尾统一刷(此时 scroll_content 才 parent 到 self,find_form_row 可达)。
        self.cmb_db.currentIndexChanged.connect(self._on_db_backend_changed)

    def _build_objectstore(self, root: QVBoxLayout) -> None:
        """对象存储 — 后端 + 本地目录 / S3 凭据(后端切时显隐)。"""
        s = FormSection(
            title=self.tr("对象存储 (ObjectStore)"),
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
        # 后端切换显隐(初始显隐在 _build() 末尾统一刷,见该处注释)
        self.cmb_os.currentIndexChanged.connect(self._on_os_backend_changed)

    def _build_policy(self, root: QVBoxLayout) -> None:
        """策略 — 关窗行为 + 媒体下载策略 + 单文件大小 + 数据根目录。"""
        s = FormSection(
            title=self.tr("策略"),
            fields=(
                # 2026-11-24:关窗行为开关。默认勾选 = 点 X 真退出;
                # 关掉恢复 v1.5.0 起的「隐藏到托盘后台运行」。
                FieldDef(
                    key="close_on_exit",
                    kind=FieldKind.BOOL,
                    label=self.tr("关闭窗口时退出程序"),
                    default=True,
                    tooltip=self.tr(
                        "勾选时点击窗口关闭按钮会退出应用;取消勾选则只隐藏到托盘,"
                        "继续后台监听,可从托盘菜单恢复或退出。"
                    ),
                ),
                # 2026-11-22 v2.0.9 FR-05:三态 media_policy 下拉换成单一 BOOL。
                # 文案明确「媒体本身=原文件」,缩略图属元数据侧、不受此开关管辖。
                FieldDef(
                    key="media_download_on_sync",
                    kind=FieldKind.BOOL,
                    label=self.tr("同步时下载媒体本身"),
                    default=False,
                    tooltip=self.tr(
                        "仅控制是否下载原文件;元数据与缩略图始终保留。"
                        "关闭时不下原文件,开启后新消息中的媒体原文件将自动下载。"
                        "修改后重启生效。"
                    ),
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
        self.chk_close_on_exit = s._widgets["close_on_exit"]
        self.chk_media_download = s._widgets["media_download_on_sync"]
        self.in_media_max = s._widgets["media_max_mb"]
        self.in_data_root = s._widgets["data_root"]

    def _build_sync(self, root: QVBoxLayout) -> None:
        """同步参数 — 频道间隔 / 分页间隔 / 续拉 checkbox。"""
        s = FormSection(
            title=self.tr("同步参数"),
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

    # ============================================================
    # 后端切换显隐 — 用 form_section.find_form_row 走跨 group 隔离
    # ============================================================
    # DB 后端切(SQLITE ↔ POSTGRES):DSN 仅 PG / SQLite 文件仅 SQLite,互斥显隐。
    # OS 后端切(本地/S3 字段互斥),保留 `_on_os_backend_changed`。

    def _on_db_backend_changed(self) -> None:
        is_pg = self.cmb_db.currentData() == DBBackend.POSTGRES
        # DSN:POSTGRES 时显示
        self._set_field_visible("db_dsn", is_pg)
        # SQLite 文件:SQLITE 时显示
        self._set_field_visible("sqlite_path", not is_pg)

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
    # 存/取 — section.collect() 通用循环
    # ============================================================

    def _collect(self) -> EditableSettings:
        """收集当前表单值 → EditableSettings。

        6 个 FormSection 走 section.collect() 通用循环。
        """
        ud = _user_data_dir()
        # 聚合 6 个 FormSection 的字段
        collected: dict[str, Any] = {}
        for sec in self._sections:
            collected.update(sec.collect())

        # 字段名映射:FormSection 用的 key → EditableSettings 字段名
        # (e.g. "db_backend" → "db_backend"; "chat_delay_ms" → "sync_chat_delay_ms")
        return EditableSettings(
            api_id=int(collected.get("api_id", 0)),
            api_hash=str(collected.get("api_hash", "")).strip(),
            session_dir=str(collected.get("session_dir", "")).strip() or str(ud / "session"),
            db_backend=collected.get("db_backend", DBBackend.SQLITE),
            db_dsn=str(collected.get("db_dsn", "")).strip(),
            sqlite_path=str(collected.get("sqlite_path", "")).strip(),
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
            # FR-05:BOOL 开关是唯一真值源 —— 勾选→full(媒体本身侧),
            # 未勾选→thumbnail(元数据侧)。to_settings() 会按镜像字段再算一次,
            # 这里同时填 media_policy 保证直接读 Collected 的调用方也是规范值。
            media_download_on_sync=bool(collected.get("media_download_on_sync", False)),
            media_policy=(
                MediaPolicy.FULL.value
                if collected.get("media_download_on_sync", False)
                else MediaPolicy.THUMBNAIL.value
            ),
            media_max_mb=int(collected.get("media_max_mb", 200)),
            data_root=str(collected.get("data_root", "")).strip() or str(ud),
            proxy=str(collected.get("proxy", "")).strip(),
            sync_chat_delay_ms=int(collected.get("chat_delay_ms", 500)),
            sync_page_delay_ms=int(collected.get("page_delay_ms", 1000)),
            sync_resume_from_saved=bool(collected.get("resume_from_saved", True)),
            # 2026-11-24:关窗行为必须走表单 —— 之前漏掉这一项,取消勾选保存
            # 时仍按 EditableSettings 默认 True 写回 .env,设置永远生效不了。
            close_on_exit=bool(collected.get("close_on_exit", True)),
        )

    def _load_from_settings(self) -> None:
        """从 app.settings 加载当前值到表单。

        6 个 FormSection 走通用 load()。
        """
        s = self._app.settings

        # 7 个分组通用 load(6 个 FormSection + 监听频道由 MainWindow 单独刷)
        section_values = {
            # account
            "api_id": s.api_id,
            "api_hash": s.api_hash,
            "session_dir": str(s.session_dir),
            # proxy
            "proxy": s.proxy or "",
            # storage
            "db_backend": s.db_backend,
            "db_dsn": s.db_dsn or "",
            "sqlite_path": str(s.sqlite_path),
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
            # policy — FR-05:只有 FULL 是「开」,metadata/thumbnail 都是关侧。
            "media_download_on_sync": s.media_policy == MediaPolicy.FULL,
            "media_max_mb": s.media_max_bytes // (1024 * 1024),
            "data_root": str(s.data_root),
            # policy — 关窗行为也要回填,否则重开设置页看到的一直是默认勾选,
            # 再点保存会把 .env 里的 false 悄悄改回 true。
            "close_on_exit": s.close_on_exit,
            # sync
            "chat_delay_ms": s.sync_chat_delay_ms,
            "page_delay_ms": s.sync_page_delay_ms,
            "resume_from_saved": s.sync_resume_from_saved,
        }
        for sec in self._sections:
            sec.load(section_values)

    # ============================================================
    # 槽 — 跟原版一致(行为零变化)
    # ============================================================

    def _on_apply(self) -> None:
        """保存:后端校验通过后才写 .env + 热重载。"""
        try:
            e = self._collect()
        except Exception as exc:
            log.exception("collect settings failed")
            QMessageBox.critical(self, self.tr("保存失败"), self.tr(f"读取表单失败: {exc}"))
            return
        errs = e.validate()
        if errs:
            QMessageBox.warning(self, self.tr("校验失败"), "\n".join(errs))
            return
        new_settings = e.to_settings()

        async def _validate_and_apply() -> None:
            # reconfigure:存储/对象存储变更时先建新库验证,失败上抛(不落盘)
            await self._app.reconfigure(new_settings)
            # 后端就绪后才写 .env(重启 bootstrap 用的就是这份配置)
            update_env_with_settings(self._env_path, new_settings)

        def _applied(_result: object) -> None:
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
                return self.tr(f"可达: {host}:{port}")
            except TimeoutError:
                return self.tr("超时: 3 秒未响应")
            except Exception as exc:  # noqa: BLE001 — 连通性探测错误一律转用户可读文案
                return self.tr(f"失败: {exc}")

        def _show(msg: str) -> None:
            """on_success / on_error 共用:恢复按钮 + 弹窗。"""
            self.btn_test_proxy.setEnabled(True)
            self.btn_test_proxy.setText(self.tr("测试连接"))
            QMessageBox.information(self, self.tr("测试结果"), msg)

        run_coro(
            self._loop,
            _test(),
            on_success=_show,
            on_error=lambda e: _show(self.tr(f"异常: {e}")),
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
