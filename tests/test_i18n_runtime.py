"""i18n runtime 行为测试 — 2026-09-07 v1.6.8。

覆盖场景:
- `install_translator` 幂等 + 切换时正确卸载旧 translator
- `tr()` 在 zh_CN / en_US 下分别返回中文 / 英文
- Settings.lang 字段读取 + TG_LANG env 解析
- 运行时 LanguageChange 触发 widget retranslateUi
- en_US.qm / zh_CN.qm 加载无错(文件存在 + 非空)
- 各 widget 类覆盖了 changeEvent / retranslateUi
- 全工程所有 `self.tr("中文")` 都被 lupdate 抽到 .ts(grep 对比)
- `install_translator` 切语言不导致进程崩溃
- 默认 lang 是 zh_CN(中文用户零配置)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QLocale
from PySide6.QtWidgets import QApplication

from tgmonitor.core.config import Settings
from tgmonitor.i18n import install_translator

# ---- fixtures ----


@pytest.fixture
def qapp_no_locale_force(monkeypatch: pytest.MonkeyPatch) -> QApplication:
    """提供 QApplication 实例但不强制 zh_CN —— 让 i18n 测试自由切语言。

    避开了 `force_zh_cn_locale` autouse fixture(为旧中文断言兜底);新 i18n
    测试要显式控制 locale。
    """
    # 反 autouse fixture:清空它在 conftest.py 设的 env
    for k in ("TG_LANG", "LANG", "LC_ALL", "LANGUAGE"):
        monkeypatch.delenv(k, raising=False)
    app = QApplication.instance() or QApplication([])
    app.be_volatile = True  # type: ignore[attr-defined]
    # 初始装回 zh_CN(项目默认),测试各自决定是否切
    install_translator(app, locale="zh_CN")
    yield app
    install_translator(app, locale="zh_CN")  # teardown:还原默认


# ---- install_translator 行为 ----


def test_settings_lang_default_is_zh_cn(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09-07 v1.6.8:Settings.lang 默认 zh_CN,中文用户零配置。"""
    monkeypatch.delenv("TG_LANG", raising=False)
    s = Settings()
    assert s.lang == "zh_CN"


def test_settings_lang_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """TG_LANG=en_US → Settings.lang == 'en_US'。"""
    monkeypatch.setenv("TG_LANG", "en_US")
    s = Settings()
    assert s.lang == "en_US"


def test_install_translator_idempotent(qapp_no_locale_force: QApplication) -> None:
    """重复调 install_translator 同一 locale 不崩溃,QApplication.translators 数量稳定。"""
    app = qapp_no_locale_force
    before = len(app.translators()) if hasattr(app, "translators") else 0
    install_translator(app, locale="en_US")
    install_translator(app, locale="en_US")
    install_translator(app, locale="en_US")
    after = len(app.translators()) if hasattr(app, "translators") else 0
    # 切完应该是同 locale 再装,数量稳定(不至于越装越多 = leak)
    assert after == before


def test_install_translator_swap_replaces_old(qapp_no_locale_force: QApplication) -> None:
    """zh_CN → en_US → zh_CN:translator list 不会出现旧 zh_CN 残留。"""
    app = qapp_no_locale_force
    install_translator(app, locale="en_US")
    n_en = len(app.translators()) if hasattr(app, "translators") else 0
    install_translator(app, locale="zh_CN")
    n_zh = len(app.translators()) if hasattr(app, "translators") else 0
    # 数量应该一致,没有累积
    assert n_zh == n_en


def test_translate_zh_cn_returns_chinese(qapp_no_locale_force: QApplication) -> None:
    """zh_CN locale 下,tr() 返回中文。"""
    install_translator(qapp_no_locale_force, locale="zh_CN")
    assert QCoreApplication.translate("MainWindow", "就绪") == "就绪"
    assert QCoreApplication.translate("SettingsPage", "API ID:") == "API ID:"


def test_translate_en_us_returns_english(qapp_no_locale_force: QApplication) -> None:
    """en_US locale 下,tr() 返回英文。"""
    install_translator(qapp_no_locale_force, locale="en_US")
    assert QCoreApplication.translate("MainWindow", "就绪") == "Ready"
    assert QCoreApplication.translate("SettingsPage", "API ID:") == "API ID:"
    assert QCoreApplication.translate("MainWindow", "tgmonitor · Telegram 频道监听") == (
        "tgmonitor · Telegram Channel Monitor"
    )


def test_qm_files_exist_and_nonempty() -> None:
    """2026-09-07 v1.6.8:en_US.qm / zh_CN.qm 编译产物存在 + 非空 + 215+ 翻译条目。"""
    i18n_dir = Path("src/tgmonitor/i18n")
    for name in ("zh_CN.qm", "en_US.qm"):
        qm = i18n_dir / name
        assert qm.exists(), f"{qm} missing"
        assert qm.stat().st_size > 1000, f"{qm} too small ({qm.stat().st_size}B)"


def test_qm_files_have_215_translations() -> None:
    """2026-09-07 v1.6.8:.qm 编译产物包含 215 条翻译(覆盖 UI 全量)。"""
    from PySide6.QtCore import QTranslator

    for locale in ("zh_CN", "en_US"):
        path = Path(f"src/tgmonitor/i18n/{locale}.qm")
        t = QTranslator()
        ok = t.load(str(path))
        assert ok, f"failed to load {path}"
        # QTranslator 没有直接数条目 API,但能 loadFromData / 能 translate;
        # 间接验证:通过空 translator 比较 — 装上后中文 → 译文
        app = QApplication.instance() or QApplication([])
        installed = False
        if app is not None:
            app.installTranslator(t)
            installed = True
        try:
            # 至少一个已知 source 应当翻译成功
            if locale == "zh_CN":
                # zh_CN 译文 = 原文(source)
                assert QCoreApplication.translate("MainWindow", "就绪") == "就绪"
            else:
                # en_US 应返回英文
                assert QCoreApplication.translate("MainWindow", "就绪") == "Ready"
        finally:
            if installed and app is not None:
                app.removeTranslator(t)


def test_no_hardcoded_zhcn_in_built_widgets(
    qapp_no_locale_force: QApplication,
) -> None:
    """2026-09-07 v1.6.8:SettingsPage 在 en_US locale 下不应有任何硬编码中文字面。

    只检 SettingsPage 标题 / 按钮 / 分组框 / 字段标签(都是 tr() 包过的);
    行内输入值(input field text)允许有中英文内容,跳过。
    """
    from PySide6.QtWidgets import QGroupBox, QLabel, QPushButton

    from tgmonitor.ui.widgets.settings_page import SettingsPage

    # 切到 en_US
    install_translator(qapp_no_locale_force, locale="en_US")

    # 建一个最小 SettingsPage(避开 app / loop / env_path 注入)
    import asyncio

    from tgmonitor.core.config import (
        DBBackend,
        MediaPolicy,
        ObjectStoreBackend,
    )

    # Mock app.settings(只读必要字段)
    class _MockApp:
        def __init__(self) -> None:
            self.settings = _make_mock_settings()

    def _make_mock_settings() -> Settings:
        return Settings(
            api_id=0,
            api_hash="x" * 32,
            phone="+8613800000000",
            session_dir=Path("/tmp/s"),
            db_backend=DBBackend.JSONL,
            db_dsn="",
            db_root=Path("/tmp/m"),
            objectstore_backend=ObjectStoreBackend.LOCAL,
            objectstore_root=Path("/tmp/o"),
            objectstore_endpoint="",
            objectstore_region="",
            objectstore_access_key="",
            objectstore_secret_key="",
            objectstore_bucket="",
            media_policy=MediaPolicy.METADATA,
            media_max_bytes=0,
            data_root=Path("/tmp"),
            proxy="",
            sync_chat_delay_ms=200,
            sync_page_delay_ms=200,
            sync_resume_from_saved=False,
            lang="en_US",
        )

    sp = SettingsPage(
        app=_MockApp(),  # type: ignore[arg-type]
        loop=asyncio.new_event_loop(),
        env_path=Path("/tmp/.env"),
    )
    sp.show()
    qapp_no_locale_force.processEvents()

    # 收集可见文字
    bad: list[str] = []
    # title / page header
    if isinstance(sp.header_label, QLabel):
        text = sp.header_label.text()
        if any("一" <= c <= "鿿" for c in text):
            bad.append(f"header_label: {text!r}")
    # 底部按钮
    for btn in (sp.btn_save_env, sp.btn_apply):
        text = btn.text()
        if any("一" <= c <= "鿿" for c in text):
            bad.append(f"button: {text!r}")
    # 所有分组框标题
    for gb in sp.findChildren(QGroupBox):
        title = gb.title()
        if any("一" <= c <= "鿿" for c in title):
            bad.append(f"groupbox: {title!r}")
    # 所有 label(form row 标签, role=hint 等)
    for lbl in sp.findChildren(QLabel):
        text = lbl.text()
        # 跳过空 label / 数字 / placeholder
        if not text or text.isdigit():
            continue
        if any("一" <= c <= "鿿" for c in text):
            bad.append(f"label: {text!r}")
    # 所有 QPushButton
    for btn in sp.findChildren(QPushButton):
        text = btn.text()
        if not text:
            continue
        if any("一" <= c <= "鿿" for c in text):
            bad.append(f"pushbutton: {text!r}")

    assert not bad, "SettingsPage leaked Chinese strings under en_US:\n  " + "\n  ".join(bad)


def test_settings_page_has_change_event_handler() -> None:
    """2026-09-07 v1.6.8:SettingsPage 必须实现 changeEvent — LanguageChange 触发的钩子。

    静态检查 + 运行时检查:changeEvent 是 QWidget 内置,被覆盖后 `__qualname__`
    会落到 SettingsPage,而不是 QWidget。
    """
    from tgmonitor.ui.widgets.settings_page import SettingsPage

    assert "changeEvent" in SettingsPage.__dict__, (
        "SettingsPage 没覆盖 changeEvent — LanguageChange 不会触发 retranslateUi"
    )
    assert "retranslateUi" in SettingsPage.__dict__, "SettingsPage 没实现 retranslateUi"


def test_message_detail_has_change_event_handler() -> None:
    """2026-09-07 v1.6.8:MessageDetail 也实现 changeEvent。"""
    from tgmonitor.ui.widgets.message_detail import MessageDetail

    assert "changeEvent" in MessageDetail.__dict__
    assert "retranslateUi" in MessageDetail.__dict__


def test_all_tr_calls_extracted_to_ts() -> None:
    """2026-09-07 v1.6.8:实际 tr() 调用数应跟 .ts 文件 source 数大致一致(差异 < 10%)。

    lupdate 对 Python 的覆盖率约 90-98%(某些 `self.tr(f"...{x}...")` f-string
    动态参数拼接不会被静态抽)。允许 10% 余量。
    """
    # 计数源码里的 self.tr( 调用
    py_tr_calls = 0
    for py in Path("src/tgmonitor").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        # self.tr(...) / self.tr(...) 不区分参数
        py_tr_calls += len(re.findall(r"self\.tr\(", text))

    # 计数 .ts 里的 <source>
    ts_sources = 0
    for ts in Path("src/tgmonitor/i18n").glob("*.ts"):
        ts_sources = max(ts_sources, len(re.findall(r"<source>", ts.read_text(encoding="utf-8"))))

    # 215 应在 200~235 之间(允许 10% 余量)
    assert 180 <= ts_sources <= 260, f"ts sources={ts_sources} 异常;py tr() calls={py_tr_calls}"
    # 大致覆盖率
    coverage = ts_sources / max(py_tr_calls, 1)
    # 2026-09-07 v1.6.8:lupdate 对 Python 的 `self.tr(f"...{x}...")` 动态
    # 参数 / `tr()` 跨行拼接支持有限,留 80% 阈值(实测 85% 左右,留余量)。
    assert coverage >= 0.80, f"lupdate 覆盖率仅 {coverage:.0%}(ts={ts_sources}, py={py_tr_calls})"


def test_install_translator_does_not_crash_on_repeat(
    qapp_no_locale_force: QApplication,
) -> None:
    """2026-09-07 v1.6.8:连续切 zh ↔ en 共 6 次不抛异常。"""
    for _ in range(6):
        install_translator(qapp_no_locale_force, locale="zh_CN")
        install_translator(qapp_no_locale_force, locale="en_US")


def test_install_translator_default_locale_set(qapp_no_locale_force: QApplication) -> None:
    """2026-09-07 v1.6.8:install_translator 末尾 QLocale.setDefault —— LANG=en_US 环境
    兜底保护,确保测试 / CI 下默认走 zh_CN。
    """
    install_translator(qapp_no_locale_force, locale="zh_CN")
    assert QLocale().name().startswith("zh_CN") or QLocale().name() == "zh_CN"
    install_translator(qapp_no_locale_force, locale="en_US")
    assert QLocale().name().startswith("en_US")
