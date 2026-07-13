"""SettingsDialog — 运行时编辑 Telegram / DB / ObjectStore / Media 配置。

行为:
- 加载当前 `AppService.settings`,渲染到表单
- 表单按 backend 动态显示/隐藏相关字段(S3 凭证、JSONL 目录 等)
- "保存并应用" → 写 .env → 调 `AppService.reconfigure()` → 触发 `SettingsChanged` 事件
- "仅保存" → 只写 .env,下次启动生效
- "取消" → 丢弃

UI 只依赖:AppService、EditableSettings(SettingsStore)— 不直接 import core internals。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend
from tgmonitor.core.settings_store import EditableSettings, update_env_with_settings

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService


class SettingsDialog(QDialog):
    def __init__(self, app: "AppService", loop: asyncio.AbstractEventLoop, env_path: Path, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.loop = loop
        self.env_path = env_path
        self.editable = EditableSettings.from_settings(app.settings)
        self._build()
        self._set_initial_values()

    # ---- UI 装配 ----

    def _build(self) -> None:
        self.setWindowTitle("设置")
        self.setMinimumWidth(560)
        root = QVBoxLayout(self)

        # === Telegram 区 ===
        gb_tg = QGroupBox("Telegram")
        f_tg = QFormLayout(gb_tg)
        self.in_api_id = QSpinBox()
        self.in_api_id.setRange(0, 2_000_000_000)
        self.in_api_hash = QLineEdit()
        self.in_api_hash.setEchoMode(QLineEdit.Password)
        self.in_api_hash.setPlaceholderText("32 位 hash,来自 my.telegram.org")
        self.in_phone = QLineEdit()
        self.in_phone.setPlaceholderText("+8613800000000")
        self.in_session_dir = QLineEdit()
        self.in_session_dir.setPlaceholderText("./data/session")
        f_tg.addRow("API ID:", self.in_api_id)
        f_tg.addRow("API Hash:", self.in_api_hash)
        f_tg.addRow("手机号:", self.in_phone)
        f_tg.addRow("Session 目录:", self.in_session_dir)
        self.lbl_tg_warn = QLabel("⚠ 修改 Telegram 凭据后需重新登录")
        self.lbl_tg_warn.setStyleSheet("color: #cc8800;")
        self.lbl_tg_warn.setVisible(False)
        f_tg.addRow("", self.lbl_tg_warn)
        # 凭据改动提示
        self.in_api_id.valueChanged.connect(self._maybe_warn_credentials)
        self.in_api_hash.textChanged.connect(self._maybe_warn_credentials)
        self.in_phone.textChanged.connect(self._maybe_warn_credentials)
        root.addWidget(gb_tg)

        # === 数据库区 ===
        gb_db = QGroupBox("消息存储 (Database)")
        f_db = QFormLayout(gb_db)
        self.cmb_db = QComboBox()
        for b in DBBackend:
            self.cmb_db.addItem(_db_label(b), b.value)
        self.in_db_dsn = QLineEdit()
        self.in_db_dsn.setPlaceholderText("postgresql://user:pwd@host:5432/db")
        self.in_db_root = QLineEdit()
        self.in_db_root.setPlaceholderText("./data/messages  (JSONL 后端用)")
        self.btn_db_root = QPushButton("浏览…")
        self.btn_db_root.clicked.connect(lambda: self._browse_dir(self.in_db_root))
        db_root_row = QWidget(); drl = QHBoxLayout(db_root_row); drl.setContentsMargins(0, 0, 0, 0)
        drl.addWidget(self.in_db_root, 1); drl.addWidget(self.btn_db_root)
        f_db.addRow("后端:", self.cmb_db)
        f_db.addRow("DSN / 目录:", self.in_db_dsn)
        f_db.addRow("文件根:", db_root_row)
        self.cmb_db.currentIndexChanged.connect(self._update_db_fields)
        root.addWidget(gb_db)

        # === 对象存储区 ===
        gb_obj = QGroupBox("对象存储 (ObjectStore)")
        f_obj = QFormLayout(gb_obj)
        self.cmb_obj = QComboBox()
        for b in ObjectStoreBackend:
            self.cmb_obj.addItem(_obj_label(b), b.value)
        self.in_obj_root = QLineEdit()
        self.in_obj_root.setPlaceholderText("./data/media")
        self.btn_obj_root = QPushButton("浏览…")
        self.btn_obj_root.clicked.connect(lambda: self._browse_dir(self.in_obj_root))
        obj_root_row = QWidget(); orl = QHBoxLayout(obj_root_row); orl.setContentsMargins(0, 0, 0, 0)
        orl.addWidget(self.in_obj_root, 1); orl.addWidget(self.btn_obj_root)

        self.in_s3_endpoint = QLineEdit()
        self.in_s3_endpoint.setPlaceholderText("http://localhost:9000")
        self.in_s3_region = QLineEdit()
        self.in_s3_region.setPlaceholderText("us-east-1")
        self.in_s3_ak = QLineEdit()
        self.in_s3_ak.setEchoMode(QLineEdit.Password)
        self.in_s3_sk = QLineEdit()
        self.in_s3_sk.setEchoMode(QLineEdit.Password)
        self.in_s3_bucket = QLineEdit()
        self.in_s3_bucket.setPlaceholderText("tgmonitor")
        f_obj.addRow("后端:", self.cmb_obj)
        f_obj.addRow("本地根目录:", obj_root_row)
        f_obj.addRow("S3 Endpoint:", self.in_s3_endpoint)
        f_obj.addRow("Region:", self.in_s3_region)
        f_obj.addRow("Access Key:", self.in_s3_ak)
        f_obj.addRow("Secret Key:", self.in_s3_sk)
        f_obj.addRow("Bucket:", self.in_s3_bucket)
        self.cmb_obj.currentIndexChanged.connect(self._update_obj_fields)
        root.addWidget(gb_obj)

        # === 业务策略 ===
        gb_pol = QGroupBox("策略")
        f_pol = QFormLayout(gb_pol)
        self.cmb_media = QComboBox()
        for p in MediaPolicy:
            self.cmb_media.addItem(_policy_label(p), p.value)
        f_pol.addRow("媒体下载:", self.cmb_media)
        root.addWidget(gb_pol)

        # === 按钮 ===
        bb = QDialogButtonBox()
        self.btn_apply = bb.addButton("保存并应用", QDialogButtonBox.AcceptRole)
        self.btn_save_only = bb.addButton("仅保存到 .env", QDialogButtonBox.ActionRole)
        self.btn_cancel = bb.addButton("取消", QDialogButtonBox.RejectRole)
        self.btn_apply.clicked.connect(self._on_apply)
        self.btn_save_only.clicked.connect(self._on_save_only)
        self.btn_cancel.clicked.connect(self.reject)
        root.addWidget(bb)

    # ---- 初始值与字段联动 ----

    def _set_initial_values(self) -> None:
        e = self.editable
        self.in_api_id.setValue(int(e.api_id))
        self.in_api_hash.setText(e.api_hash)
        self.in_phone.setText(e.phone)
        self.in_session_dir.setText(e.session_dir)
        self._set_combo(self.cmb_db, e.db_backend)
        self.in_db_dsn.setText(e.db_dsn)
        self.in_db_root.setText(e.db_root)
        self._set_combo(self.cmb_obj, e.objectstore_backend)
        self.in_obj_root.setText(e.objectstore_root)
        self.in_s3_endpoint.setText(e.objectstore_endpoint)
        self.in_s3_region.setText(e.objectstore_region)
        self.in_s3_ak.setText(e.objectstore_access_key)
        self.in_s3_sk.setText(e.objectstore_secret_key)
        self.in_s3_bucket.setText(e.objectstore_bucket)
        self._set_combo(self.cmb_media, e.media_policy)
        self._update_db_fields()
        self._update_obj_fields()

    @staticmethod
    def _set_combo(cmb: QComboBox, value: str) -> None:
        for i in range(cmb.count()):
            if cmb.itemData(i) == value:
                cmb.setCurrentIndex(i)
                return

    def _maybe_warn_credentials(self) -> None:
        e = self._collect()
        changed = (
            e.api_id != self.app.settings.api_id
            or e.api_hash != self.app.settings.api_hash
            or e.phone != self.app.settings.phone
        )
        self.lbl_tg_warn.setVisible(changed)

    def _update_db_fields(self) -> None:
        backend = self.cmb_db.currentData() or DBBackend.POSTGRES.value
        is_jsonl = backend == DBBackend.JSONL.value
        is_pg_or_mongo = backend in (DBBackend.POSTGRES.value, DBBackend.MONGO.value)
        self.in_db_dsn.setEnabled(is_pg_or_mongo)
        self.in_db_root.setEnabled(is_jsonl)
        self.in_db_dsn.setPlaceholderText(
            {
                DBBackend.POSTGRES.value: "postgresql://user:pwd@host:5432/db",
                DBBackend.MONGO.value: "mongodb://user:pwd@host:27017/db",
            }.get(backend, "")
        )

    def _update_obj_fields(self) -> None:
        backend = self.cmb_obj.currentData() or ObjectStoreBackend.LOCAL.value
        is_s3 = backend == ObjectStoreBackend.S3.value
        is_local = backend in (
            ObjectStoreBackend.LOCAL.value,
            ObjectStoreBackend.FOLDER.value,
        )
        self.in_obj_root.setEnabled(is_local)
        for w in (
            self.in_s3_endpoint,
            self.in_s3_region,
            self.in_s3_ak,
            self.in_s3_sk,
            self.in_s3_bucket,
        ):
            w.setEnabled(is_s3)

    # ---- 收集 / 验证 / 保存 ----

    def _collect(self) -> EditableSettings:
        e = EditableSettings(
            api_id=self.in_api_id.value(),
            api_hash=self.in_api_hash.text().strip(),
            phone=self.in_phone.text().strip(),
            session_dir=self.in_session_dir.text().strip() or "./data/session",
            db_backend=self.cmb_db.currentData() or DBBackend.POSTGRES.value,
            db_dsn=self.in_db_dsn.text().strip(),
            db_root=self.in_db_root.text().strip() or "./data/messages",
            objectstore_backend=self.cmb_obj.currentData() or ObjectStoreBackend.LOCAL.value,
            objectstore_root=self.in_obj_root.text().strip() or "./data/media",
            objectstore_endpoint=self.in_s3_endpoint.text().strip(),
            objectstore_region=self.in_s3_region.text().strip() or "us-east-1",
            objectstore_access_key=self.in_s3_ak.text().strip(),
            objectstore_secret_key=self.in_s3_sk.text().strip(),
            objectstore_bucket=self.in_s3_bucket.text().strip() or "tgmonitor",
            media_policy=self.cmb_media.currentData() or MediaPolicy.THUMBNAIL.value,
            data_root="./data",
        )
        return e

    def _on_save_only(self) -> None:
        e = self._collect()
        errs = e.validate()
        if errs:
            QMessageBox.warning(self, "校验失败", "\n".join(errs))
            return
        try:
            update_env_with_settings(self.env_path, e.to_settings())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "写 .env 失败", str(exc))
            return
        QMessageBox.information(self, "已保存", f"配置已写入 {self.env_path}\n下次启动生效。")
        self.accept()

    def _on_apply(self) -> None:
        e = self._collect()
        errs = e.validate()
        if errs:
            QMessageBox.warning(self, "校验失败", "\n".join(errs))
            return
        # 1) 写 .env(失败也允许仅热重载)
        try:
            update_env_with_settings(self.env_path, e.to_settings())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "写 .env 失败", str(exc))
            return
        # 2) 异步热重载
        new_settings = e.to_settings()
        fut = asyncio.run_coroutine_threadsafe(self.app.reconfigure(new_settings), self.loop)

        def _on_done(f) -> None:
            try:
                f.result()
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "热重载失败", str(exc))
                return
            relogin = (
                new_settings.api_id != self.app.settings.api_id
                or new_settings.api_hash != self.app.settings.api_hash
                or new_settings.phone != self.app.settings.phone
            )
            if relogin:
                QMessageBox.information(
                    self,
                    "已应用",
                    f"存储/对象存储已热重载。\nTelegram 凭据已变更 — 请重新登录。\n(.env: {self.env_path})",
                )
            else:
                QMessageBox.information(
                    self,
                    "已应用",
                    f"配置已热重载。\n(.env: {self.env_path})",
                )
            self.accept()

        fut.add_done_callback(_on_done)

    def _browse_dir(self, target: QLineEdit) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择目录", target.text())
        if d:
            target.setText(d)


# ---- 显示标签(中英) ----

def _db_label(b: DBBackend) -> str:
    return {
        DBBackend.POSTGRES: "PostgreSQL",
        DBBackend.MONGO: "MongoDB",
        DBBackend.JSONL: "JSONL 文件(无需 DB 服务)",
    }[b]


def _obj_label(b: ObjectStoreBackend) -> str:
    return {
        ObjectStoreBackend.LOCAL: "本地(平铺)",
        ObjectStoreBackend.FOLDER: "本地(两级分片)",
        ObjectStoreBackend.S3: "S3 / MinIO / 阿里 OSS",
    }[b]


def _policy_label(p: MediaPolicy) -> str:
    return {
        MediaPolicy.METADATA: "仅元数据",
        MediaPolicy.THUMBNAIL: "元数据 + 缩略图",
        MediaPolicy.FULL: "元数据 + 缩略图 + 原文件",
    }[p]
