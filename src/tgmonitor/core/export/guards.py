"""导出内容安全 guard — 2026-08-27 v1.4.0 PR #17。

Telegram 用户/频道内容是「半可信」输入:消息文本、文件名、channel
title 都来自外部,可能被攻击者利用导出器渲染层做以下副作用:

- **Markdown 注入 (CWE-79)**:``## `` / ``> `` / ``[...](javascript:...)`` /
  ``![...](tracker)`` 等被 Markdown 渲染器解析 — 受害者打开 .md 触发追踪
  像素或脚本协议
- **CSV 公式注入 (CWE-1236)**:Excel / LibreOffice / Numbers 把以
  ``=`` / ``+`` / ``-`` / ``@`` 开头的单元格当公式执行 — ``file_name =
  "=cmd|'/c calc'!A1"`` 是真实利用链
- **HTML 巨缩略图**:data URI 没大小上限 — 一张 50MB 缩略图内嵌到 HTML
  打开会卡死浏览器

各 exporter 在写入时调 ``_scrub_markdown`` / ``_guard_csv_cell`` /
``_check_thumb_size``;函数本身 stateless 且纯文本,便于单测。
"""

from __future__ import annotations

import re

# CSV 公式注入:Excel 把 `=` / `+` / `-` / `@` 开头的值当公式。Tab /
# CR 是早期 Excel 4.0 宏触发链,一并覆盖。
# 见 https://owasp.org/www-community/attacks/CSV_Injection
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# Markdown scrub 规则集:在行首出现,且被解析成 Markdown 结构时转义。
# - 行首 1-6 个 `#`(`## 标题` → `\#\# 标题`)
# - 行首 `>`(`> quote` → `\>`)
# - 行首 `*`(`* item` → `\*`)
# - 行首 ` ``` ` 围栏代码
# - `[…](javascript:…)` 链接协议
# - `![…](…)` 完整图片语法
_MD_HEADING_RE = re.compile(r"(^|\n)(\s*)(#{1,6})", flags=re.M)
_MD_BQ_RE = re.compile(r"(^|\n)(\s*)(>+)", flags=re.M)
_MD_UL_RE = re.compile(r"(^|\n)(\s*)(\*+)", flags=re.M)
_MD_FENCE_RE = re.compile(r"(^|\n)(\s*)```", flags=re.M)
_MD_JS_LINK_RE = re.compile(r"(\[.*?\]\()\s*javascript:", flags=re.I)
_MD_IMG_RE = re.compile(r"!\[", flags=re.M)


def _scrub_markdown(text: str) -> str:
    """防 Markdown 解析/渲染副作用(CWE-79)。

    转义原则:**保留原字符,但加 ``\\`` 让 Markdown 渲染器当字面文本**。
    用户可在编辑器手动去 ``\\`` 还原。
    """
    if not text:
        return text
    out = _MD_HEADING_RE.sub(r"\1\2\\\3", text)
    out = _MD_BQ_RE.sub(r"\1\2\\\3", out)
    out = _MD_UL_RE.sub(r"\1\2\\\3", out)
    out = _MD_FENCE_RE.sub(r"\1\2\\```", out)
    out = _MD_JS_LINK_RE.sub(r"\1", out)
    out = _MD_IMG_RE.sub(r"\\!\[", out)
    return out


def _guard_csv_cell(value: str | None) -> str:
    """防 Excel/LibreOffice formula injection(CWE-1236)。

    以 ``=+-@`` / Tab / CR 开头的 cell 加 ``'`` 前缀;``None`` / 空串原样。
    """
    if not value:
        return value or ""
    if value[0] in _CSV_FORMULA_PREFIXES:
        return "'" + value
    return value


# HTML 缩略图 data URI 大小上限 — 256 KB 是浏览器内联渲染「不卡」的
# 经验阈值。超过则不内嵌,改用占位文,避免 50MB 缩略图冻死浏览器。
MAX_THUMB_DATA_URI_BYTES = 256 * 1024
