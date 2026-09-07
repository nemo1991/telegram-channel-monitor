"""i18n bootstrap — 2026-09-03 v1.5.3 PR #D3 + 2026-09-07 v1.6.8 二期。

职责:
- 默认 locale = `zh_CN`,翻译 = 原文 → 零用户可见行为变化
- 切到第二 locale(如 `en_US`)时加载对应 `.qm`
- `QTranslator` 加载失败 → log warning + 继续(原文 fallback,**不 raise**)
- 集成 Python `gettext` 域 "tgmonitor" 给 core 模块用(`AuthService` 等),
  `gettext.translation().install()` 后 `_()` 全局可用
- `install_translator` **idempotent + 支持 reinstall** — v1.6.8 SettingsPage
  切语言时会反复调;旧 translator 必须先 uninstall 再装新的,否则 Qt 内部
  list 累积 + gettext 域被后装的覆盖。

调用时机:`app.py:run()` 启动早期,QApplication 实例化后即装;SettingsPage
「语言」分组切语言时重装。`conftest.py` 加 `force_zh_cn_locale` fixture 强
制 zh_CN 让既有 30+ 处 widget.text() == "中文" 断言保持兼容(默认 zh_CN
翻译 = 原文)。
"""

from __future__ import annotations

import gettext
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtCore import QCoreApplication, QTranslator
    from PySide6.QtWidgets import QApplication

log = logging.getLogger(__name__)

# i18n 资源目录(本模块同目录:src/tgmonitor/i18n/)
_I18N_DIR = Path(__file__).parent
_DOMAIN = "tgmonitor"

# 模块级 single-instance — 2026-09-07 v1.6.8:支持 uninstall / reinstall。
# 之前每次 install_translator() 都 new QTranslator,旧实例被 Qt GC 但 QApplication
# 的 translators 列表里仍然残留 → 累积内存泄漏 + LanguageChange 多次 fire。
_qt_translator: QTranslator | None = None
_gettext_translations: gettext.NullTranslations | None = None
_current_locale: str = ""


def install_translator(
    qt_app: QCoreApplication | QApplication,
    locale: str = "zh_CN",
) -> None:
    """装 Qt `QTranslator` + Python `gettext` 域。

    行为:
    - `.qm` / `.mo` 找不到 → log + **不 raise**(原文 fallback)
    - 强制 `QLocale.setDefault(locale)`,防 CI 切 LANG 撞英文
    - v1.6.8:idempotent + 支持 reinstall(同 locale 跳过,异 locale 先卸旧再装新)
    """
    from PySide6.QtCore import QCoreApplication

    global _qt_translator, _gettext_translations, _current_locale

    # 1) idempotent:同 locale 重复调直接 return(避免测试 / hot-reload 抖动)
    if locale == _current_locale and _qt_translator is not None:
        log.debug("install_translator(%s) idempotent skip", locale)
        return

    # 2) reinstall:异 locale 先卸旧(避免 Qt 内部 translators 列表累积)
    if _qt_translator is not None:
        try:
            QCoreApplication.removeTranslator(_qt_translator)
        except Exception:  # noqa: BLE001
            log.debug("removeTranslator 旧实例失败,可忽略", exc_info=True)
        _qt_translator = None

    # 3) Python gettext 域 — core 模块(`AuthService` 等)走 `_("...")`
    try:
        _gettext_translations = gettext.translation(
            _DOMAIN,
            localedir=str(_I18N_DIR),
            languages=[locale],
            fallback=True,
        )
        _gettext_translations.install()  # 装到 builtins,_() 全局可用
        log.debug("gettext 域 '%s' 装好(locale=%s)", _DOMAIN, locale)
    except Exception as e:  # noqa: BLE001
        log.warning("gettext.translation 失败,fallback 到内置 _(): %s", e)
        _gettext_translations = None

    # 4) Qt `QTranslator` — UI 控件(QLabel / QPushButton 等)走 `self.tr(...)`
    from PySide6.QtCore import QLocale, QTranslator

    qt_translator = QTranslator(qt_app)
    qm_path = _I18N_DIR / f"{locale}.qm"
    if qm_path.exists():
        if qt_translator.load(str(qm_path)):
            qt_app.installTranslator(qt_translator)
            _qt_translator = qt_translator
            log.info("Qt 翻译器已装: %s", qm_path)
        else:
            log.warning("QTranslator.load 失败: %s", qm_path)
    else:
        # zh_CN 默认即原文(.qm 是 lrelease 编译产物,可有可无)
        log.info(
            "翻译文件 %s 不存在,UI 用原文(zh_CN 默认即原文)",
            qm_path,
        )

    # 5) 强制 locale(防测试/生产环境 LANG 撞英文撞坏 widget 文本断言)
    QLocale.setDefault(QLocale(locale))
    _current_locale = locale

    # 6) 触发 LanguageChange — 所有 top-level widget 收到信号后自动
    # retranslateUi 重译。Qt 不在 installTranslator 后自动 fire,需手动
    # post;SettingsPage 切语言时省去自己手动 sendEvent 的麻烦。
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.postEvent(qt_app, QEvent(QEvent.Type.LanguageChange))


def current_locale() -> str:
    """返回当前已装 locale(测试 / 调试用)。"""
    return _current_locale


def get_i18n_dir() -> Path:
    """测试用 — 返回 i18n 资源目录路径。"""
    return _I18N_DIR
