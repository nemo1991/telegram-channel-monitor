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
  6. 🔄 同步参数   — chat_delay / page_delay / resume_from_saved
  7. 储存按钮栏
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
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
        header = QLabel("设置")
        header.setObjectName("pageTitle")
        header.setContentsMargins(24, 24, 24, 8)
        root.addWidget(header)

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
        self._build_sync(form_root)

        form_root.addStretch(1)
        scroll.setWidget(scroll_content)
        root.addWidget(scroll, 1)

        # 底部固定按钮
        btn_bar = QHBoxLayout()
        btn_bar.setContentsMargins(24, 12, 24, 16)

        self.btn_save_env = QPushButton("仅保存到 .env")
        self.btn_save_env.clicked.connect(self._on_save_env)
        btn_bar.addWidget(self.btn_save_env)

        btn_bar.addStretch(1)

        self.btn_apply = QPushButton("保存并应用")
        self.btn_apply.setObjectName("primaryBtn")
        self.btn_apply.clicked.connect(self._on_apply)
        btn_bar.addWidget(self.btn_apply)

        root.addLayout(btn_bar)

    # ------ 各分组装配 ------

    def _build_account(self, root: QVBoxLayout) -> None:
        g = QGroupBox("📱 账户凭证")
        f = QFormLayout(g)
        f.setSpacing(6)

        self.in_api_id = spin_field(
            f, "API ID:", min=0, max=2_000_000_000, value=0,
        )

        self.in_api_hash = text_field(
            f, "API Hash:", "32 位 hash · my.telegram.org", echo_password=True,
        )

        self.in_phone = text_field(f, "手机号:", "+8613800000000")

        # Session 目录(浏览 + 恢复默认)— path_field helper
        self.in_session_dir = path_field(
            f,
            "Session 目录:",
            str(_user_data_dir() / "session"),
            on_default=lambda: self._set_default(self.in_session_dir, "session"),
            default_tooltip="恢复为 platform-native 默认目录",
            parent=self,
        )

        root.addWidget(g)

    def _build_proxy(self, root: QVBoxLayout) -> None:
        g = QGroupBox("🌐 网络代理 (Proxy)")
        f = QFormLayout(g)
        f.setSpacing(6)

        proxy_row = QHBoxLayout()
        self.in_proxy = QLineEdit()
        self.in_proxy.setPlaceholderText("socks5://[user:pass@]host:port")
        proxy_row.addWidget(self.in_proxy, 1)
        self.btn_test_proxy = QPushButton("测试连接")
        self.btn_test_proxy.clicked.connect(self._on_test_proxy)
        proxy_row.addWidget(self.btn_test_proxy)
        f.addRow("代理 URL:", proxy_row)

        root.addWidget(g)

    def _build_storage(self, root: QVBoxLayout) -> None:
        g = QGroupBox("💾 消息存储 (Database)")
        f = QFormLayout(g)
        f.setSpacing(6)

        self.cmb_db = combo_field(f, "后端:", DBBackend)

        self.in_db_dsn = text_field(f, "DSN:", "postgresql://user:pass@host/db")

        self.in_db_root = path_field(
            f,
            "JSONL 目录:",
            str(_user_data_dir() / "messages"),
            on_default=lambda: self._set_default(self.in_db_root, "messages"),
            default_tooltip="恢复为 platform-native 默认目录",
            parent=self,
        )

        # DB 后端切换 → 显隐 DSN / 目录
        self.cmb_db.currentIndexChanged.connect(self._on_db_backend_changed)
        self._on_db_backend_changed()

        root.addWidget(g)

    def _build_objectstore(self, root: QVBoxLayout) -> None:
        g = QGroupBox("📁 对象存储 (ObjectStore)")
        f = QFormLayout(g)
        f.setSpacing(6)

        self.cmb_os = combo_field(f, "后端:", ObjectStoreBackend)

        # 本地
        self.in_os_root = path_field(
            f,
            "本地目录:",
            str(_user_data_dir() / "media"),
            on_default=lambda: self._set_default(self.in_os_root, "media"),
            default_tooltip="恢复为 platform-native 默认目录",
            parent=self,
        )

        # S3
        self.in_os_endpoint = text_field(f, "S3 Endpoint:", "https://s3.<region>.amazonaws.com")
        self.in_os_region = text_field(f, "Region:", "us-east-1")
        self.in_os_access_key = text_field(f, "Access Key:", "", echo_password=True)
        self.in_os_secret_key = text_field(f, "Secret Key:", "", echo_password=True)
        self.in_os_bucket = text_field(f, "Bucket:", "tgmonitor")
        self.lbl_os_s3_hint = QLabel(
            "提示:AWS 填 s3.<region>.amazonaws.com(留空走默认);"
            "MinIO / 阿里 OSS 填各自 API 地址;勿填控制台 / 网页地址"
        )
        self.lbl_os_s3_hint.setProperty("role", "hint")
        self.lbl_os_s3_hint.setWordWrap(True)
        f.addRow("", self.lbl_os_s3_hint)

        self.cmb_os.currentIndexChanged.connect(self._on_os_backend_changed)
        self._on_os_backend_changed()

        root.addWidget(g)

    def _build_policy(self, root: QVBoxLayout) -> None:
        g = QGroupBox("⚙️ 策略")
        f = QFormLayout(g)
        f.setSpacing(6)

        self.cmb_media = combo_field(f, "媒体下载:", MediaPolicy)

        self.in_media_max = spin_field(
            f, "单文件大小上限:",
            min=0, max=10240,                  # 0 = 无限制,10 GB 上限
            suffix=" MB", single_step=10,
            tooltip="单文件下载上限。0 = 无限制(慎用,可能下载 GB 级视频把磁盘占满)。",
        )

        self.in_data_root = path_field(
            f,
            "数据根目录:",
            str(_user_data_dir()),
            on_default=lambda: self._set_default(self.in_data_root, ""),
            default_tooltip="恢复为 platform-native 默认目录",
            parent=self,
        )

        root.addWidget(g)

    def _build_sync(self, root: QVBoxLayout) -> None:
        g = QGroupBox("🔄 同步参数")
        f = QFormLayout(g)
        f.setSpacing(6)

        self.in_chat_delay = spin_field(
            f, "频道间间隔:",
            min=50, max=60000, suffix=" ms", single_step=50,
        )

        self.in_page_delay = spin_field(
            f, "分页间隔:",
            min=100, max=60000, suffix=" ms", single_step=100,
        )

        self.chk_resume = QCheckBox("续拉(从已保存位置继续)")
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
        is_local = self.cmb_os.currentData() in (ObjectStoreBackend.LOCAL, ObjectStoreBackend.FOLDER)
        is_s3 = self.cmb_os.currentData() == ObjectStoreBackend.S3
        # 本地目录:local/folder 时显示
        hit = self._find_form_row(self.in_os_root)
        if hit is not None:
            g, idx = hit
            self._set_form_row_visible(g, idx, is_local)
        # S3 字段 + 提示:S3 时显示
        for w in (self.in_os_endpoint, self.in_os_region, self.in_os_access_key,
                  self.in_os_secret_key, self.in_os_bucket, self.lbl_os_s3_hint):
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
            QMessageBox.critical(self, "保存失败", f"读取表单失败: {exc}")
            return
        errs = e.validate()
        if errs:
            QMessageBox.warning(self, "校验失败", "\n".join(errs))
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
            QMessageBox.information(self, "已保存", f"设置已写入 {self._env_path}")

        def _save_failed(exc: BaseException) -> None:
            self.btn_save_env.setEnabled(True)
            self.btn_apply.setEnabled(True)
            if isinstance(exc, OSError):
                QMessageBox.critical(self, ".env 写入失败", str(exc))
                return
            QMessageBox.critical(
                self, "保存失败",
                f"后端配置未通过校验,已放弃保存(设置未写入 .env):\n\n{exc}\n\n"
                "请检查数据库 / 对象存储配置与对应服务是否可达后重试。",
            )

        run_coro(
            self._loop, _validate_and_save(),
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
        """
        try:
            e = self._collect()
        except Exception as exc:  # noqa: BLE001 — Qt 槽内异常只打 stderr,用户无感知
            log.exception("collect settings failed")
            QMessageBox.critical(self, "保存失败", f"读取表单失败: {exc}")
            return
        errs = e.validate()
        if errs:
            QMessageBox.warning(self, "校验失败", "\n".join(errs))
            return
        new_settings = e.to_settings()

        async def _validate_and_apply() -> None:
            # reconfigure:存储/对象存储变更时先建新库验证,失败上抛(不落盘)
            await self._app.reconfigure(new_settings)
            # 后端就绪后才写 .env(重启 bootstrap 用的就是这份配置)
            update_env_with_settings(self._env_path, new_settings)

        def _applied(_result: object) -> None:
            QMessageBox.information(self, "已应用", "设置已保存并热重载")

        def _apply_failed(exc: BaseException) -> None:
            if isinstance(exc, OSError):
                QMessageBox.critical(self, ".env 写入失败", str(exc))
                return
            QMessageBox.critical(
                self, "保存失败",
                f"后端配置未通过校验,已放弃保存(设置未写入 .env):\n\n{exc}\n\n"
                "请检查数据库 / 对象存储配置与对应服务是否可达后重试。",
            )

        run_coro(
            self._loop, _validate_and_apply(),
            on_success=_applied,
            on_error=_apply_failed,
            error_label="reconfigure",
        )

    def _on_test_proxy(self) -> None:
        """测试 SOCKS5 代理的 TCP 可达性。"""
        url = self.in_proxy.text().strip()
        if not url:
            QMessageBox.warning(self, "测试连接", "请先填写代理 URL")
            return
        self.btn_test_proxy.setEnabled(False)
        self.btn_test_proxy.setText("测试中…")

        async def _test() -> str:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                host = parsed.hostname or "127.0.0.1"
                port = parsed.port or 1080
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=3.0,
                )
                writer.close()
                await writer.wait_closed()
                return f"✅ 可达: {host}:{port}"
            except TimeoutError:
                return "❌ 超时: 3 秒未响应"
            except Exception as exc:
                return f"❌ 失败: {exc}"

        def _show(msg: str) -> None:
            """on_success / on_error 共用:恢复按钮 + 弹窗。"""
            self.btn_test_proxy.setEnabled(True)
            self.btn_test_proxy.setText("测试连接")
            QMessageBox.information(self, "测试结果", msg)

        run_coro(
            self._loop, _test(),
            on_success=_show,
            on_error=lambda e: _show(f"❌ 异常: {e}"),
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


def _is_child_of(child: QWidget, parent: QWidget) -> bool:
    """检查 child 是否是 parent 的后代。"""
    p = child.parentWidget()
    while p is not None:
        if p is parent:
            return True
        p = p.parentWidget()
    return False
