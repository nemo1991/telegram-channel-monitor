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

from PySide6.QtWidgets import (
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
    QVBoxLayout,
    QWidget,
)

from tgmonitor.core.config import DBBackend, MediaPolicy, ObjectStoreBackend
from tgmonitor.core.settings_store import EditableSettings, update_env_with_settings

if TYPE_CHECKING:
    from tgmonitor.core.app_service import AppService


class SettingsDialog(QDialog):
    def __init__(self, app: AppService, loop: asyncio.AbstractEventLoop, env_path: Path, parent=None) -> None:
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

        # 顶部提示:Telegram 凭据已迁到主窗口左栏
        tip = QLabel(
            "💡 Telegram 凭据(API ID / Hash / 手机号)在<b>主窗口左侧的「账户」面板</b>里。\n"
            "本对话框专注于后端 / 对象存储 / 媒体策略 / 代理等低频配置。"
        )
        tip.setWordWrap(True)
        tip.setProperty("role", "hint")
        root.addWidget(tip)

        # === 代理(目前只 SOCKS5)— 在策略之前,影响 TDLib 连通性 ===
        gb_proxy = QGroupBox("网络代理 (Proxy)")
        f_p = QFormLayout(gb_proxy)
        self.in_proxy = QLineEdit()
        self.in_proxy.setPlaceholderText("socks5://[user:pass@]host:port")
        self.in_proxy.setToolTip(
            "国内直连 Telegram 通常不通,需要一个 SOCKS5 代理。\n"
            "示例:socks5://user:pass@127.0.0.1:1080\n"
            "(留空 = 不走代理)"
        )
        proxy_row = QHBoxLayout()
        proxy_row.addWidget(self.in_proxy, 1)
        self.btn_proxy_test = QPushButton("测试连接")
        self.btn_proxy_test.clicked.connect(self._on_test_proxy)
        proxy_row.addWidget(self.btn_proxy_test)
        f_p.addRow("代理 URL:", proxy_row)
        root.addWidget(gb_proxy)

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
        db_root_row = QWidget()
        drl = QHBoxLayout(db_root_row)
        drl.setContentsMargins(0, 0, 0, 0)
        drl.addWidget(self.in_db_root, 1)
        drl.addWidget(self.btn_db_root)
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
        obj_root_row = QWidget()
        orl = QHBoxLayout(obj_root_row)
        orl.setContentsMargins(0, 0, 0, 0)
        orl.addWidget(self.in_obj_root, 1)
        orl.addWidget(self.btn_obj_root)

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
        self.in_proxy.setText(e.proxy)
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
        # 复用现有 app.settings 的凭据 / session_dir / data_root,只覆盖表单字段
        cur = self.app.settings
        return EditableSettings(
            api_id=cur.api_id,
            api_hash=cur.api_hash,
            phone=cur.phone,
            session_dir=str(cur.session_dir),
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
            data_root=str(cur.data_root),
            proxy=self.in_proxy.text().strip(),
        )

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

    def _on_test_proxy(self) -> None:
        """异步测代理 TCP 可达性,在事件循环里执行,socketserver 超时 3 秒。"""
        url = self.in_proxy.text().strip()
        if not url:
            QMessageBox.information(self, "代理", "代理 URL 为空")
            return
        # 在线程里别阻塞 UI:用 run_in_executor
        import socket

        try:
            rest = url.split("://", 1)[1]
            if "@" in rest:
                _, hp = rest.rsplit("@", 1)
            else:
                hp = rest
            host, _, port_s = hp.rpartition(":")
            port = int(port_s)
        except Exception as exc:
            QMessageBox.warning(self, "代理格式错误", str(exc))
            return

        loop = asyncio.get_event_loop()

        def _probe() -> str:
            with socket.create_connection((host, port), timeout=3.0):
                return "ok"

        async def _go() -> str:
            try:
                return await loop.run_in_executor(None, _probe)
            except Exception as exc:  # noqa: BLE001
                return f"failed: {exc}"

        fut = asyncio.run_coroutine_threadsafe(_go(), self.loop)

        def _done(f) -> None:
            res = f.result()
            if res == "ok":
                QMessageBox.information(self, "代理", f"可达 {host}:{port} ✓")
            else:
                QMessageBox.warning(self, "代理不可达", res)

        fut.add_done_callback(_done)


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
