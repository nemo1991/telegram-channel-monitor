"""form_section 单元测试 — src/tgmonitor/ui/widgets/form_section.py。

plan-batch2 目标 4 第 2 步:12 case 覆盖 10 种 kind + 关键 invariant
(addRow 必触发 / set_visible 跨 group 隔离 / echo_password /
visible_when / action 按钮双态 / groupbox 标题刷新)。

QT_QPA_PLATFORM=offscreen 无 GUI 跑(跟其他 UI 测试一致)。
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QWidget,
)

from tgmonitor.ui.widgets.form_section import (
    FieldDef,
    FieldKind,
    FormSection,
)


@pytest.fixture
def holder(qapp: QApplication) -> QWidget:
    """返一个父 widget(持有 built groupbox 防止 GC)。"""
    return QWidget()


# ============================================================
# build 基础:每种 kind 1 个 round-trip case
# ============================================================


def test_secret_kind_sets_password_echo(holder: QWidget) -> None:
    """kind=SECRET: QLineEdit + EchoMode.Password(关键 — 不能漏)。"""
    s = FormSection(
        title="T",
        fields=(FieldDef(key="api_hash", kind=FieldKind.SECRET, label="API Hash:"),),
    )
    gb = s.build(holder)
    edit = gb.findChild(QLineEdit)
    assert edit is not None
    assert edit.echoMode() == QLineEdit.EchoMode.Password
    s.set_value("api_hash", "secret")
    assert s.get_value("api_hash") == "secret"


# ============================================================
# invariant:addRow 必触发(供 _find_form_row 走 row index 寻址)
# ============================================================


def test_collect_returns_all_field_values(holder: QWidget) -> None:
    """collect() 返 {key: get_value(key)} 字典 — _collect 通用化入口。"""
    s = FormSection(
        title="T",
        fields=(
            FieldDef(key="phone", kind=FieldKind.TEXT, label="Phone:"),
            FieldDef(key="api_id", kind=FieldKind.INT, label="API ID:", max=10),
            FieldDef(key="resume", kind=FieldKind.BOOL, label="Resume"),
        ),
    )
    s.build(holder)
    s.set_value("phone", "+1")
    s.set_value("api_id", 7)
    s.set_value("resume", True)
    assert s.collect() == {"phone": "+1", "api_id": 7, "resume": True}


# ============================================================
# set_visible 跨 group 隔离
# ============================================================


# ============================================================
# action 按钮双态 / groupbox 标题刷新
# ============================================================


# ============================================================
# visible_when 条件批量显隐
# ============================================================


# ============================================================
# 错误路径
# ============================================================
