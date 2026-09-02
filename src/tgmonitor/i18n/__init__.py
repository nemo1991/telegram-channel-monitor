"""i18n bootstrap — 2026-09-03 v1.5.3 PR #D3。

职责:
- 默认 locale = `zh_CN`,翻译 = 原文 → 零用户可见行为变化
- 切到第二 locale(如 `en_US`)时加载对应 `.qm`
- `QTranslator` 加载失败 → log warning + 继续(原文 fallback,**不 raise**)
- 集成 Python `gettext` 域 "tgmonitor" 给 core 模块用(`AuthService` 等),
  `gettext.translation().install()` 后 `_()` 全局可用

调用时机:`app.py` 启动早期,QApplication 实例化后即装;`conftest.py` 加
`force_zh_cn_locale` fixture 强制 zh_CN 让既有 30+ 处 widget.text() == "中文"
断言保持兼容(默认 zh_CN 翻译 = 原文)。
"""

from __future__ import annotations

import gettext
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication

log = logging.getLogger(__name__)

# i18n 资源目录(本模块同目录:src/tgmonitor/i18n/)
_I18N_DIR = Path(__file__).parent
_DOMAIN = "tgmonitor"


def install_translator(qt_app: QApplication, locale: str = "zh_CN") -> None:
    """装 Qt `QTranslator` + Python `gettext` 域。

    行为:
    - `.qm` / `.mo` 找不到 → log + **不 raise**(原文 fallback)
    - 强制 `QLocale.setDefault(locale)`,防 CI 切 LANG 撞英文
    """
    # 1) Python gettext 域 — core 模块(`AuthService` 等)走 `_("...")`
    try:
        trans = gettext.translation(
            _DOMAIN,
            localedir=str(_I18N_DIR),
            languages=[locale],
            fallback=True,
        )
        trans.install()  # 装到 builtins,_() 全局可用
        log.debug("gettext 域 '%s' 装好(locale=%s)", _DOMAIN, locale)
    except Exception as e:  # noqa: BLE001
        log.warning("gettext.translation 失败,fallback 到内置 _(): %s", e)

    # 2) Qt `QTranslator` — UI 控件(QLabel / QPushButton 等)走 `self.tr(...)`
    from PySide6.QtCore import QLocale, QTranslator

    qt_translator = QTranslator(qt_app)
    qm_path = _I18N_DIR / f"{locale}.qm"
    if qm_path.exists():
        if qt_translator.load(str(qm_path)):
            qt_app.installTranslator(qt_translator)
            log.info("Qt 翻译器已装: %s", qm_path)
        else:
            log.warning("QTranslator.load 失败: %s", qm_path)
    else:
        # zh_CN 默认即原文(.qm 是 lrelease 编译产物,可有可无)
        log.info(
            "翻译文件 %s 不存在,UI 用原文(zh_CN 默认即原文)",
            qm_path,
        )

    # 3) 强制 locale(防测试/生产环境 LANG 撞英文撞坏 widget 文本断言)
    QLocale.setDefault(QLocale(locale))


def get_i18n_dir() -> Path:
    """测试用 — 返回 i18n 资源目录路径。"""
    return _I18N_DIR
