"""form_section 单元测试 — src/tgmonitor/ui/widgets/form_section.py。

plan-batch2 目标 4 第 2 步:12 case 覆盖 10 种 kind + 关键 invariant
(addRow 必触发 / set_visible 跨 group 隔离 / i18n context 守门 /
echo_password / visible_when / retranslate 双态)。

QT_QPA_PLATFORM=offscreen 无 GUI 跑(跟其他 UI 测试一致)。
"""

from __future__ import annotations

from enum import Enum

import pytest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from tgmonitor.ui.widgets.form_section import (
    ActionDef,
    FieldDef,
    FieldKind,
    FormSection,
    find_form_row,
)


@pytest.fixture
def holder(qapp: QApplication) -> QWidget:
    """返一个父 widget(持有 built groupbox 防止 GC)。"""
    return QWidget()


# ============================================================
# build 基础:每种 kind 1 个 round-trip case
# ============================================================


def test_int_kind_round_trip(holder: QWidget) -> None:
    """kind=INT: QSpinBox + addRow,get/set 走 .value()。"""
    s = FormSection(
        title="T",
        fields=(FieldDef(key="api_id", kind=FieldKind.INT, label="API ID:", max=2_000_000_000),),
    )
    gb = s.build(holder)
    assert isinstance(gb, QGroupBox)
    assert gb.findChild(QSpinBox) is not None
    s.set_value("api_id", 12345)
    assert s.get_value("api_id") == 12345
    assert gb.findChild(QSpinBox).value() == 12345


def test_text_kind_round_trip(holder: QWidget) -> None:
    """kind=TEXT: QLineEdit + addRow,get/set 走 .text()。"""
    s = FormSection(
        title="T",
        fields=(FieldDef(key="phone", kind=FieldKind.TEXT, label="Phone:", placeholder="+1"),),
    )
    gb = s.build(holder)
    s.set_value("phone", "+10000000000")
    assert s.get_value("phone") == "+10000000000"
    assert isinstance(gb.findChild(QLineEdit), QLineEdit)


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


def test_path_kind_adds_browse_button(holder: QWidget) -> None:
    """kind=PATH: QLineEdit + 「浏览…」按钮(子节点数 ≥ 2)。"""
    s = FormSection(
        title="T",
        fields=(FieldDef(key="db_root", kind=FieldKind.PATH, label="DB Root:"),),
    )
    gb = s.build(holder)
    # 浏览按钮 + edit + 默认按钮(若有)= 至少 1 个 QPushButton + 1 个 QLineEdit
    assert gb.findChild(QPushButton) is not None
    assert gb.findChild(QLineEdit) is not None


def test_choice_kind_with_enum(holder: QWidget) -> None:
    """kind=CHOICE: Iterable[Enum],data=enum 本身。"""

    class _C(Enum):
        A = "a"
        B = "b"

    s = FormSection(
        title="T",
        fields=(
            FieldDef(key="backend", kind=FieldKind.CHOICE, label="Backend:", options=(_C.A, _C.B)),
        ),
    )
    gb = s.build(holder)
    cmb = gb.findChild(QComboBox)
    assert cmb is not None
    assert cmb.itemData(0) == _C.A
    s.set_value("backend", _C.B)
    assert s.get_value("backend") == _C.B


def test_choice_pairs_kind_with_tuples(holder: QWidget) -> None:
    """kind=CHOICE_PAIRS: Iterable[(data, display)],data 自定。"""
    s = FormSection(
        title="T",
        fields=(
            FieldDef(
                key="lang",
                kind=FieldKind.CHOICE_PAIRS,
                label="Lang:",
                options=(("zh_CN", "简体中文"), ("en_US", "English")),
            ),
        ),
    )
    gb = s.build(holder)
    cmb = gb.findChild(QComboBox)
    assert cmb is not None
    assert cmb.itemData(0) == "zh_CN"
    s.set_value("lang", "en_US")
    assert s.get_value("lang") == "en_US"


def test_bool_kind_round_trip(holder: QWidget) -> None:
    """kind=BOOL: QCheckBox,get/set 走 .isChecked()。"""
    s = FormSection(
        title="T",
        fields=(FieldDef(key="resume", kind=FieldKind.BOOL, label="Resume"),),
    )
    gb = s.build(holder)
    cb = gb.findChild(QCheckBox)
    assert cb is not None
    s.set_value("resume", True)
    assert s.get_value("resume") is True
    assert cb.isChecked() is True


def test_static_label_kind(holder: QWidget) -> None:
    """kind=STATIC_LABEL: 只读 QLabel,get_value 返 None,不能 set 写。"""
    s = FormSection(
        title="T",
        fields=(FieldDef(key="hint", kind=FieldKind.STATIC_LABEL, label="提示:…"),),
    )
    gb = s.build(holder)
    lbl = gb.findChild(QLabel)
    assert lbl is not None
    assert lbl.text() == "提示:…"
    assert s.get_value("hint") is None


# ============================================================
# invariant:addRow 必触发(供 _find_form_row 走 row index 寻址)
# ============================================================


def test_build_creates_one_row_per_field(holder: QWidget) -> None:
    """build 后 QFormLayout.rowCount() == fields 数 — 守门 addRow 必触发。

    跟 test_form_row.py:109-112,174-177 同样守门:不 addRow 后续
    set_visible 走 row index 会乱。
    """
    s = FormSection(
        title="T",
        fields=(
            FieldDef(key="a", kind=FieldKind.TEXT, label="A:"),
            FieldDef(key="b", kind=FieldKind.INT, label="B:", max=100),
            FieldDef(key="c", kind=FieldKind.BOOL, label="C"),
        ),
    )
    gb = s.build(holder)
    form = gb.findChild(QFormLayout)
    assert form is not None
    assert form.rowCount() == 3


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


def test_set_visible_toggles_single_field_in_group(holder: QWidget) -> None:
    """set_visible 单字段 → 对应 row 隐藏/显示,**不**影响同 group 其他 row。"""
    s = FormSection(
        title="T",
        fields=(
            FieldDef(key="a", kind=FieldKind.TEXT, label="A:"),
            FieldDef(key="b", kind=FieldKind.TEXT, label="B:"),
            FieldDef(key="c", kind=FieldKind.TEXT, label="C:"),
        ),
    )
    s.build(holder)
    s.set_visible("b", False)
    # 找 a/b/c widget(从 _widgets 拿引用)
    w_a = s._widgets["a"]
    w_b = s._widgets["b"]
    w_c = s._widgets["c"]
    # 用 isHidden() 反向断言:widget 没 show 时 isVisible() 永远 False,
    # 但 setVisible(False) 会把 isHidden() 翻成 True。setVisible(True) 翻回。
    assert w_a.isHidden() is False
    assert w_b.isHidden() is True
    assert w_c.isHidden() is False
    # 再显示回来
    s.set_visible("b", True)
    assert w_b.isHidden() is False


def test_find_form_row_finds_across_nested_groups(holder: QWidget) -> None:
    """find_form_row 跨嵌套 QGroupBox 找 key — 不串到错误 group。"""
    s1 = FormSection(
        title="G1",
        fields=(FieldDef(key="g1_field", kind=FieldKind.TEXT, label="g1:"),),
    )
    s2 = FormSection(
        title="G2",
        fields=(FieldDef(key="g2_field", kind=FieldKind.TEXT, label="g2:"),),
    )
    gb1 = s1.build(holder)
    gb2 = s2.build(holder)
    # 模拟外层 QFormLayout 装两个 group
    outer = QFormLayout(holder)
    outer.addRow(gb1)
    outer.addRow(gb2)

    # find_form_row 走 page.findChildren(QGroupBox),需要 page 持有 group
    # 直接传 holder 测试
    found1 = find_form_row(holder, "g1_field")
    found2 = find_form_row(holder, "g2_field")
    assert found1 is not None and found1[0] is gb1
    assert found2 is not None and found2[0] is gb2


# ============================================================
# retranslate 双态(action 按钮)
# ============================================================


def test_retranslate_action_button_idle_vs_busy(holder: QWidget) -> None:
    """action 按钮 enabled → idle 文字,disabled → busy 文字。"""
    s = FormSection(
        title="T",
        actions=(ActionDef(key="test", label_idle="测试连接", label_busy="测试中…"),),
    )
    gb = s.build(holder)
    btn = gb.findChild(QPushButton)
    assert btn is not None
    assert btn.text() == "测试连接"
    # 模拟「正在跑」disable
    btn.setEnabled(False)
    s.retranslate()
    assert btn.text() == "测试中…"
    # 恢复
    btn.setEnabled(True)
    s.retranslate()
    assert btn.text() == "测试连接"


def test_retranslate_resets_groupbox_title(holder: QWidget) -> None:
    """retranslate() 把 groupbox title 刷成 self.title(支持 caller 改 title 后刷回)。"""
    s = FormSection(title="原标题", fields=(FieldDef(key="x", kind=FieldKind.TEXT, label="X:"),))
    gb = s.build(holder)
    s.title = "新标题"
    s.retranslate()
    assert gb.title() == "新标题"


# ============================================================
# visible_when 条件批量显隐
# ============================================================


def test_apply_visibility_with_visible_when(holder: QWidget) -> None:
    """visible_when 条件 + apply_visibility 批量 — DB 后端切 JSONL 场景。"""
    s = FormSection(
        title="DB",
        fields=(
            FieldDef(key="db_dsn", kind=FieldKind.TEXT, label="DSN:"),
            FieldDef(
                key="db_root",
                kind=FieldKind.PATH,
                label="Root:",
                visible_when=(("is_jsonl", True),),
            ),
        ),
    )
    s.build(holder)
    # PG 模式:db_root 藏
    s.apply_visibility({"is_jsonl": False})
    # path_field 内部 wrapper + QLineEdit 套在 QHBoxLayout,断言走 QFormLayout
    # itemAt(row, FieldRole) 拿到的 wrapper widget(整 row 显隐单位)
    form = s._form
    assert form is not None
    dsn_field_item = form.itemAt(0, QFormLayout.ItemRole.FieldRole)
    root_field_item = form.itemAt(1, QFormLayout.ItemRole.FieldRole)
    assert dsn_field_item is not None and root_field_item is not None
    assert dsn_field_item.widget() is not None
    assert dsn_field_item.widget().isHidden() is False
    assert root_field_item.widget().isHidden() is True
    # 切 JSONL:db_root 显示
    s.apply_visibility({"is_jsonl": True})
    assert root_field_item.widget().isHidden() is False


# ============================================================
# 错误路径
# ============================================================


def test_get_value_unknown_key_raises() -> None:
    """未知 key → KeyError(开发期 bug 必现,不静默 None)。"""
    s = FormSection(title="T")
    with pytest.raises(KeyError, match="unknown"):
        s.get_value("unknown")


def test_set_value_none_is_silent() -> None:
    """set_value(val=None) 静默跳过 — load() 容忍缺失字段。"""
    s = FormSection(
        title="T",
        fields=(FieldDef(key="x", kind=FieldKind.TEXT, label="X:"),),
    )
    s.build()
    s.set_value("x", None)  # 不抛
    # 仍是初始空字符串
    assert s.get_value("x") == ""
