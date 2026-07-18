"""SettingsPage — 整页设置(不再是模态对话框)。

囊括 settings_dialog.py 的全部配置项 + account_widget.py 的凭据编辑。
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
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend
from tgmonitor.core.settings_store import EditableSettings, update_env_with_settings

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

        self.in_api_id = QSpinBox()
        self.in_api_id.setRange(0, 2_000_000_000)
        self.in_api_id.setValue(0)
        f.addRow("API ID:", self.in_api_id)

        self.in_api_hash = QLineEdit()
        self.in_api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.in_api_hash.setPlaceholderText("32 位 hash · my.telegram.org")
        f.addRow("API Hash:", self.in_api_hash)

        self.in_phone = QLineEdit()
        self.in_phone.setPlaceholderText("+8613800000000")
        f.addRow("手机号:", self.in_phone)

        self.in_session_dir = QLineEdit()
        self.in_session_dir.setPlaceholderText("./data/session")
        # 浏览按钮
        sdir_row = QHBoxLayout()
        sdir_row.addWidget(self.in_session_dir, 1)
        btn_sdir = QPushButton("浏览…")
        btn_sdir.clicked.connect(lambda: self._browse_dir(self.in_session_dir))
        sdir_row.addWidget(btn_sdir)
        f.addRow("Session 目录:", sdir_row)

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

        self.cmb_db = QComboBox()
        for b in DBBackend:
            self.cmb_db.addItem(b.value, b)
        f.addRow("后端:", self.cmb_db)

        self.in_db_dsn = QLineEdit()
        self.in_db_dsn.setPlaceholderText("postgresql://user:pass@host/db")
        f.addRow("DSN:", self.in_db_dsn)

        db_root_row = QHBoxLayout()
        self.in_db_root = QLineEdit()
        self.in_db_root.setPlaceholderText("./data/messages")
        db_root_row.addWidget(self.in_db_root, 1)
        btn_dbr = QPushButton("浏览…")
        btn_dbr.clicked.connect(lambda: self._browse_dir(self.in_db_root))
        db_root_row.addWidget(btn_dbr)
        f.addRow("JSONL 目录:", db_root_row)

        # DB 后端切换 → 显隐 DSN / 目录
        self.cmb_db.currentIndexChanged.connect(self._on_db_backend_changed)
        self._on_db_backend_changed()

        root.addWidget(g)

    def _build_objectstore(self, root: QVBoxLayout) -> None:
        g = QGroupBox("📁 对象存储 (ObjectStore)")
        f = QFormLayout(g)
        f.setSpacing(6)

        self.cmb_os = QComboBox()
        for b in ObjectStoreBackend:
            self.cmb_os.addItem(b.value, b)
        f.addRow("后端:", self.cmb_os)

        # 本地
        os_root_row = QHBoxLayout()
        self.in_os_root = QLineEdit()
        self.in_os_root.setPlaceholderText("./data/media")
        os_root_row.addWidget(self.in_os_root, 1)
        btn_osr = QPushButton("浏览…")
        btn_osr.clicked.connect(lambda: self._browse_dir(self.in_os_root))
        os_root_row.addWidget(btn_osr)
        f.addRow("本地目录:", os_root_row)

        # S3
        self.in_os_endpoint = QLineEdit()
        self.in_os_endpoint.setPlaceholderText("https://s3.amazonaws.com")
        f.addRow("S3 Endpoint:", self.in_os_endpoint)

        self.in_os_region = QLineEdit()
        self.in_os_region.setPlaceholderText("us-east-1")
        f.addRow("Region:", self.in_os_region)

        self.in_os_access_key = QLineEdit()
        self.in_os_access_key.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow("Access Key:", self.in_os_access_key)

        self.in_os_secret_key = QLineEdit()
        self.in_os_secret_key.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow("Secret Key:", self.in_os_secret_key)

        self.in_os_bucket = QLineEdit()
        self.in_os_bucket.setPlaceholderText("tgmonitor")
        f.addRow("Bucket:", self.in_os_bucket)

        self.cmb_os.currentIndexChanged.connect(self._on_os_backend_changed)
        self._on_os_backend_changed()

        root.addWidget(g)

    def _build_policy(self, root: QVBoxLayout) -> None:
        g = QGroupBox("⚙️ 策略")
        f = QFormLayout(g)
        f.setSpacing(6)

        self.cmb_media = QComboBox()
        for p in MediaPolicy:
            self.cmb_media.addItem(p.value, p)
        f.addRow("媒体下载:", self.cmb_media)

        self.in_data_root = QLineEdit()
        self.in_data_root.setPlaceholderText("./data")
        data_root_row = QHBoxLayout()
        data_root_row.addWidget(self.in_data_root, 1)
        btn_dr = QPushButton("浏览…")
        btn_dr.clicked.connect(lambda: self._browse_dir(self.in_data_root))
        data_root_row.addWidget(btn_dr)
        f.addRow("数据根目录:", data_root_row)

        root.addWidget(g)

    def _build_sync(self, root: QVBoxLayout) -> None:
        g = QGroupBox("🔄 同步参数")
        f = QFormLayout(g)
        f.setSpacing(6)

        self.in_chat_delay = QSpinBox()
        self.in_chat_delay.setRange(50, 60000)
        self.in_chat_delay.setSuffix(" ms")
        self.in_chat_delay.setSingleStep(50)
        f.addRow("频道间间隔:", self.in_chat_delay)

        self.in_page_delay = QSpinBox()
        self.in_page_delay.setRange(100, 60000)
        self.in_page_delay.setSuffix(" ms")
        self.in_page_delay.setSingleStep(100)
        f.addRow("分页间隔:", self.in_page_delay)

        self.chk_resume = QCheckBox("续拉(从已保存位置继续)")
        f.addRow("", self.chk_resume)

        root.addWidget(g)

    # ------ 后端切换显隐 ------

    def _on_db_backend_changed(self) -> None:
        is_jsonl = self.cmb_db.currentData() == DBBackend.JSONL
        # DSN 行:postgres/mongo 时启用,jsonl 时禁用
        idx_dsn = self._find_form_row(self.in_db_dsn)
        if idx_dsn is not None:
            self._set_form_row_visible(idx_dsn, not is_jsonl)
        # 目录行:jsonl 时启用
        idx_dir = self._find_form_row(self.in_db_root)
        if idx_dir is not None:
            self._set_form_row_visible(idx_dir, is_jsonl)

    def _on_os_backend_changed(self) -> None:
        is_local = self.cmb_os.currentData() in (ObjectStoreBackend.LOCAL, ObjectStoreBackend.FOLDER)
        is_s3 = self.cmb_os.currentData() == ObjectStoreBackend.S3
        # 本地目录:local/folder 时显示
        idx_root = self._find_form_row(self.in_os_root)
        if idx_root is not None:
            self._set_form_row_visible(idx_root, is_local)
        # S3 字段:S3 时显示
        for w in (self.in_os_endpoint, self.in_os_region, self.in_os_access_key,
                  self.in_os_secret_key, self.in_os_bucket):
            idx = self._find_form_row(w)
            if idx is not None:
                self._set_form_row_visible(idx, is_s3)

    def _find_form_row(self, widget: QWidget) -> int | None:
        """在 form layout 里找到 widget 所在行。"""
        for g in self.findChildren(QGroupBox):
            fl = g.findChild(QFormLayout)
            if fl is None:
                continue
            for i in range(fl.rowCount()):
                _, fw = fl.itemAt(i, QFormLayout.FieldRole)
                if fw and fw.widget() and (fw.widget() is widget or _is_child_of(widget, fw.widget())):
                    return i
        return None

    def _set_form_row_visible(self, row: int, visible: bool) -> None:
        for g in self.findChildren(QGroupBox):
            fl = g.findChild(QFormLayout)
            if fl is None or row >= fl.rowCount():
                continue
            for role in (QFormLayout.LabelRole, QFormLayout.FieldRole):
                item = fl.itemAt(row, role)
                if item and item.widget():
                    item.widget().setVisible(visible)

    # ------ 存/取 ------

    def _collect(self) -> EditableSettings:
        """收集当前表单值 → EditableSettings。"""
        return EditableSettings(  # type: ignore[call-arg]
            api_id=self.in_api_id.value(),
            api_hash=self.in_api_hash.text().strip(),
            phone=self.in_phone.text().strip(),
            session_dir=self.in_session_dir.text().strip() or "./data/session",
            db_backend=self.cmb_db.currentData().value,
            db_dsn=self.in_db_dsn.text().strip(),
            db_root=self.in_db_root.text().strip() or "./data/messages",
            objectstore_backend=self.cmb_os.currentData().value,
            objectstore_root=self.in_os_root.text().strip() or "./data/media",
            objectstore_endpoint=self.in_os_endpoint.text().strip(),
            objectstore_region=self.in_os_region.text().strip() or "us-east-1",
            objectstore_access_key=self.in_os_access_key.text().strip(),
            objectstore_secret_key=self.in_os_secret_key.text().strip(),
            objectstore_bucket=self.in_os_bucket.text().strip() or "tgmonitor",
            media_policy=self.cmb_media.currentData().value,
            data_root=self.in_data_root.text().strip() or "./data",
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
        self.in_data_root.setText(str(s.data_root))

        self.in_chat_delay.setValue(s.sync_chat_delay_ms)
        self.in_page_delay.setValue(s.sync_page_delay_ms)
        self.chk_resume.setChecked(s.sync_resume_from_saved)

    # ------ 槽 ------

    def _on_save_env(self) -> None:
        """仅写 .env,不热重载。"""
        e = self._collect()
        errs = e.validate()
        if errs:
            QMessageBox.warning(self, "校验失败", "\n".join(errs))
            return
        try:
            update_env_with_settings(self._env_path, e.to_settings())
            QMessageBox.information(self, "已保存", f"设置已写入 {self._env_path}")
        except OSError as exc:
            QMessageBox.critical(self, "写入失败", str(exc))

    def _on_apply(self) -> None:
        """写 .env + 热重载 AppService。"""
        e = self._collect()
        errs = e.validate()
        if errs:
            QMessageBox.warning(self, "校验失败", "\n".join(errs))
            return
        try:
            new_settings = e.to_settings()
            update_env_with_settings(self._env_path, new_settings)
        except OSError as exc:
            QMessageBox.critical(self, ".env 写入失败", str(exc))
            return

        # 热重载
        fut = asyncio.run_coroutine_threadsafe(
            self._app.reconfigure(new_settings), self._loop,
        )

        def _on_done(f) -> None:
            try:
                f.result()
                log.info("settings applied + hot-reloaded")
            except Exception as exc:  # noqa: BLE001
                log.exception("reconfigure failed: %s", exc)

        fut.add_done_callback(_on_done)
        QMessageBox.information(self, "已应用", "设置已保存并热重载")

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
            except asyncio.TimeoutError:
                return "❌ 超时: 3 秒未响应"
            except Exception as exc:
                return f"❌ 失败: {exc}"

        def _on_done(f) -> None:
            try:
                msg = f.result()
            except Exception as exc:
                msg = f"❌ 异常: {exc}"
            self.btn_test_proxy.setEnabled(True)
            self.btn_test_proxy.setText("测试连接")
            QMessageBox.information(self, "测试结果", msg)

        fut = asyncio.run_coroutine_threadsafe(_test(), self._loop)
        fut.add_done_callback(_on_done)

    @staticmethod
    def _browse_dir(line_edit: QLineEdit) -> None:
        dir_path = QFileDialog.getExistingDirectory(
            None, "选择目录", line_edit.text(),
        )
        if dir_path:
            line_edit.setText(dir_path)


def _is_child_of(child: QWidget, parent: QWidget) -> bool:
    """检查 child 是否是 parent 的后代。"""
    p = child.parentWidget()
    while p is not None:
        if p is parent:
            return True
        p = p.parentWidget()
    return False
