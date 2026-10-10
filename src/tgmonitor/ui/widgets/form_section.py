"""form_section.py — settings_page 用的 form 分组抽象(2026-10-10 plan-batch2 目标 4)。

集中 QGroupBox + QFormLayout 的「一整组分形字段」样板,让 settings_page
变 orchestrator:

    form_section.py(form_row.py)        ←───  单 row 工厂(已存在)
            ▲
            │ 复用 row 工厂
            │
    form_section.py(FormSection)        ←───  本模块:整组(10+ 字段)
            ▲
            │
    settings_page.py                    ←───  编排 8 个 FormSection

## 关键设计

### 三种 dataclass

- **`FieldDef`**(frozen, 10 种 kind)— 描述「一个字段」:key / kind / label
  / placeholder / 默认值 / 范围 / 回调。`build()` 真正创 widget。
- **`FormSection`** — 持有 `title` + 一组 `FieldDef`,build() 返 QGroupBox,
  提供 get/set/retranslate/set_visible 通用 API。
- **`ActionDef`**(frozen, 非字段按钮)— 「测试连接 / 清理缓存」类
  副作用按钮,自带 enabled/disable 双态重译。

### kind 枚举(10 种)

`int` / `text` / `secret` / `path` / `choice` / `choice_pairs` / `bool` /
`static_label` / `keyseq`(走老路径,FormSection 不抽)/ `action`(走老路径)。

### i18n 约束(必读)

所有 `label` / `placeholder` / `tooltip` 由 **caller 提供已 tr() 过的字串**。
`FormSection` **不二次 tr**(避免 lupdate 静态扫描漏抽 / context 错配
成 "form_section" 而非 "SettingsPage")。

### `set_visible` 跨 group 隔离

走 settings_page 原 `_find_form_row` + `_set_form_row_visible` 逻辑
(findChildren(QGroupBox) 隔离)— 抽到本模块,settings_page 直接调
`self._storage.set_visible("db_dsn", not is_jsonl)` 即可。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from tgmonitor.ui.widgets import form_row

# ============================================================
# 枚举 + dataclass
# ============================================================


class FieldKind(str, Enum):
    """10 种字段 kind — 与 FieldDef 一一对应。"""

    INT = "int"
    TEXT = "text"
    SECRET = "secret"
    PATH = "path"
    CHOICE = "choice"  # Iterable[Enum],data=enum 本身
    CHOICE_PAIRS = "choice_pairs"  # Iterable[(data, display)],data 自定
    BOOL = "bool"  # QCheckBox
    STATIC_LABEL = "static_label"  # 只读 QLabel hint
    KEYSEQ = "keyseq"  # QKeySequenceEdit,走老路径,FormSection 不抽
    ACTION = "action"  # 非字段按钮,走 ActionDef


@dataclass(frozen=True)
class FieldDef:
    """一个字段的描述 — 10 种 kind 之一。

    `label` / `placeholder` / `tooltip` 由 caller 传**已 tr() 过的字串**
    (保持 i18n context "SettingsPage",不让 lupdate 抽到 form_section)。

    `visible_when` 是 key→bool 条件(例如 `{"is_jsonl": True}` →
    只在 JSONL 时显示);`set_visible` 走本模块隔离逻辑,不会跨 group 误隐藏。
    """

    key: str
    kind: FieldKind
    label: str
    placeholder: str = ""
    default: Any = None  # int/text/choice 默认值;bool 默认 checked 状态
    min: int = 0
    max: int = 100
    suffix: str = ""
    single_step: int = 1
    tooltip: str = ""
    echo_password: bool = False  # kind=secret 时
    options: tuple[Any, ...] = ()  # kind=choice / choice_pairs 时
    visible_when: tuple[tuple[str, bool], ...] = ()  # (cond_key, cond_value) 对
    file_mode: bool = False  # kind=path 时,True=保存文件而非目录
    on_default: Callable[[], None] | None = None  # kind=path 时,默认按钮回调
    default_tooltip: str | None = None  # kind=path 时


@dataclass(frozen=True)
class ActionDef:
    """非字段按钮 — 副作用(测试连接 / 清理缓存 / 等)。

    `label_idle` / `label_busy` 是**已 tr() 过的字串**;`retranslate()`
    根据 widget enabled 状态决定显示哪个。
    """

    key: str
    label_idle: str
    label_busy: str
    tooltip: str = ""


@dataclass
class FormSection:
    """一个 QGroupBox 分组 — 持有 title + fields + actions。

    `build()` 返回一个完整的 QGroupBox,内部 QFormLayout 已经按 fields 顺序
    排好;caller 直接 `vbox.addWidget(section.build(parent))` 即可。

    `get_value(key)` / `set_value(key, val)` 通用访问 widget 当前值;
    `retranslate()` 重译所有 label + action 按钮双态;
    `set_visible(key, bool)` 走 `findChildren(QGroupBox)` 隔离单字段。
    """

    title: str
    fields: tuple[FieldDef, ...] = ()
    actions: tuple[ActionDef, ...] = ()
    # ---- 运行时状态(build() 后填) ----
    _groupbox: QGroupBox | None = field(default=None, init=False, repr=False)
    _widgets: dict[str, QWidget] = field(default_factory=dict, init=False, repr=False)
    _actions: dict[str, QPushButton] = field(default_factory=dict, init=False, repr=False)
    _form: QFormLayout | None = field(default=None, init=False, repr=False)

    # ---- 构造 / build ----

    def build(self, parent: QWidget | None = None) -> QGroupBox:
        """构造 QGroupBox + QFormLayout + 各字段 widget;返回 groupbox。

        每个字段走对应的 form_row.py helper,保证 `addRow` 触发
        (test_form_row.py:109-112,174-177 守门)。
        """
        gb = QGroupBox(self.title, parent)
        v = QVBoxLayout(gb)
        v.setContentsMargins(8, 8, 8, 8)
        form = QFormLayout()
        form.setLabelAlignment(form.labelAlignment())
        v.addLayout(form)

        self._groupbox = gb
        self._form = form
        self._widgets = {}
        self._actions = {}

        for fdef in self.fields:
            widget = _build_field_widget(fdef, form)
            self._widgets[fdef.key] = widget
            if fdef.kind == FieldKind.STATIC_LABEL:
                # setObjectName 方便 QSS / 测试访问
                widget.setObjectName(f"lbl_{fdef.key}")
            else:
                widget.setObjectName(f"in_{fdef.key}")

        for adef in self.actions:
            btn = QPushButton(adef.label_idle)
            if adef.tooltip:
                btn.setToolTip(adef.tooltip)
            btn.setObjectName(f"btn_{adef.key}")
            form.addRow(adef.label_idle, btn)  # 用 idle label 当 row label
            self._actions[adef.key] = btn

        return gb

    # ---- 通用值访问 ----

    def get_value(self, key: str) -> Any:
        """取 key 当前值 — kind 决定怎么读。"""
        fdef = _field_by_key(self.fields, key)
        widget = self._widgets[key]
        return _read_widget(fdef, widget)

    def set_value(self, key: str, val: Any) -> None:
        """写 key 值 — kind 决定怎么写(默默忽略 None,空值落到空字符串/0)。"""
        fdef = _field_by_key(self.fields, key)
        widget = self._widgets[key]
        _write_widget(fdef, widget, val)

    def collect(self) -> dict[str, Any]:
        """遍历所有 fields,返 {key: get_value(key)}。"""
        return {fdef.key: self.get_value(fdef.key) for fdef in self.fields}

    def load(self, values: dict[str, Any]) -> None:
        """从 dict 写回 — 缺失的 key 保持当前值(不抛错)。"""
        for fdef in self.fields:
            if fdef.key in values:
                self.set_value(fdef.key, values[fdef.key])

    # ---- 重译 ----

    def retranslate(self) -> None:
        """重译 groupbox title + action 按钮双态 + static label 文字。

        字段 label / placeholder / tooltip **不重译** — caller 已在构造时
        传已 tr() 过的字串;此处不动它们(context 保持 "SettingsPage")。
        """
        if self._groupbox is not None:
            self._groupbox.setTitle(self.title)
        # action 按钮:enabled → idle,disabled → busy
        for adef in self.actions:
            btn = self._actions.get(adef.key)
            if btn is None:
                continue
            if btn.isEnabled():
                btn.setText(adef.label_idle)
            else:
                btn.setText(adef.label_busy)
        # static_label 文字(若 title 是动态的,例如「快捷键帮助:」,需重译)
        for fdef in self.fields:
            if fdef.kind == FieldKind.STATIC_LABEL and fdef.label:
                widget = self._widgets.get(fdef.key)
                if isinstance(widget, QLabel):
                    widget.setText(fdef.label)

    # ---- 显隐 ----

    def set_visible(self, key: str, visible: bool) -> None:
        """显示/隐藏单字段对应的 row — 走 groupbox 内 row index,跨 group 隔离。

        实现逻辑 = 原 settings_page._find_form_row + _set_form_row_visible
        (707-743),但限定在本 groupbox 内部找,避免误藏其他 group。
        """
        if self._form is None:
            return
        fdef = _field_by_key(self.fields, key)
        idx = self.fields.index(fdef)
        _set_row_visible(self._form, idx, visible)

    def apply_visibility(self, conditions: dict[str, bool]) -> None:
        """根据 `visible_when` 批量显隐 — 在外部条件变化时统一刷一次。

        `conditions` 是 `{cond_key: bool}` 字典;每个字段若有自己的
        `visible_when`,所有条件都满足才显示。
        """
        for fdef in self.fields:
            if not fdef.visible_when:
                continue
            show = all(conditions.get(ck, False) == cv for ck, cv in fdef.visible_when)
            self.set_visible(fdef.key, show)


# ============================================================
# 模块函数 — 抽出 settings_page 原 _find_form_row / _set_form_row_visible
# ============================================================


def _set_row_visible(form: QFormLayout, row_index: int, visible: bool) -> None:
    """显示/隐藏 form 第 row_index 行(label + field widget 一起)。

    row_index = fields 在 QFormLayout 里的行号(0 起;若有 actions 排在
    字段之后,本函数不处理 actions,因为 actions 不参与动态显隐)。
    """
    label_item = form.itemAt(row_index, QFormLayout.ItemRole.LabelRole)
    field_item = form.itemAt(row_index, QFormLayout.ItemRole.FieldRole)
    for item in (label_item, field_item):
        if item is None:
            continue
        w = item.widget()
        if w is not None:
            w.setVisible(visible)


def find_form_row(page: QWidget, key: str) -> tuple[QGroupBox, int] | None:
    """跨整个 SettingsPage 找 key 对应的 (groupbox, row_index)。

    复刻原 settings_page._find_form_row 行为:遍历 page.findChildren(QGroupBox),
    检查每个 groupbox 的 `_widgets` 字典(FormSection 内部用),返回第一个
    匹配项的 (group, row_index)。

    **不在 FormSection 上用 dict 持有 key 的话,fallback 走 row label text
    匹配** — 但 FormSection 已统一持有 `_widgets`,所以不写 fallback。
    """
    for gb in page.findChildren(QGroupBox):
        section = getattr(gb, "_section", None)
        if section is None or not isinstance(section, FormSection):
            continue
        for idx, fdef in enumerate(section.fields):
            if fdef.key == key:
                return gb, idx
    return None


# ============================================================
# 内部 helpers — build_field / read / write
# ============================================================


def _field_by_key(fields: tuple[FieldDef, ...], key: str) -> FieldDef:
    """按 key 查 FieldDef;找不到抛 KeyError(开发期 bug 必现)。"""
    for f in fields:
        if f.key == key:
            return f
    raise KeyError(f"FormSection: 未找到 key={key!r} 的 FieldDef")


def _build_field_widget(fdef: FieldDef, form: QFormLayout) -> QWidget:
    """按 kind 调对应 form_row helper(保证 addRow 触发)或直接构造。"""
    kind = fdef.kind
    if kind == FieldKind.INT:
        return form_row.spin_field(
            form,
            fdef.label,
            min=fdef.min,
            max=fdef.max,
            value=int(fdef.default or 0),
            suffix=fdef.suffix,
            single_step=fdef.single_step,
            tooltip=fdef.tooltip,
        )
    if kind == FieldKind.TEXT:
        return form_row.text_field(form, fdef.label, fdef.placeholder)
    if kind == FieldKind.SECRET:
        return form_row.text_field(form, fdef.label, fdef.placeholder, echo_password=True)
    if kind == FieldKind.PATH:
        return form_row.path_field(
            form,
            fdef.label,
            fdef.placeholder,
            on_default=fdef.on_default,
            default_tooltip=fdef.default_tooltip,
            file_mode=fdef.file_mode,
        )
    if kind == FieldKind.CHOICE:
        return form_row.combo_field(form, fdef.label, fdef.options)
    if kind == FieldKind.CHOICE_PAIRS:
        return form_row.combo_field(form, fdef.label, fdef.options)
    if kind == FieldKind.BOOL:
        # QCheckBox 不走 form_row(它是 inline,不是 row);手动 addRow
        cb = QCheckBox(fdef.label)
        if fdef.default:
            cb.setChecked(True)
        if fdef.tooltip:
            cb.setToolTip(fdef.tooltip)
        form.addRow("", cb)  # 行 label 留空(cb 自己带 label)
        return cb
    if kind == FieldKind.STATIC_LABEL:
        lbl = QLabel(fdef.label)
        lbl.setWordWrap(True)
        lbl.setProperty("role", "hint")
        form.addRow("", lbl)
        return lbl
    raise NotImplementedError(f"FormSection._build_field_widget 不支持 kind={kind!r}")


def _read_widget(fdef: FieldDef, widget: QWidget) -> Any:
    """按 kind 读 widget 当前值。"""
    kind = fdef.kind
    if kind == FieldKind.INT:
        assert isinstance(widget, QSpinBox)
        return widget.value()
    if kind in (FieldKind.TEXT, FieldKind.SECRET, FieldKind.PATH):
        assert isinstance(widget, QLineEdit)
        return widget.text()
    if kind in (FieldKind.CHOICE, FieldKind.CHOICE_PAIRS):
        assert isinstance(widget, QComboBox)
        return widget.currentData()
    if kind == FieldKind.BOOL:
        assert isinstance(widget, QCheckBox)
        return widget.isChecked()
    if kind == FieldKind.STATIC_LABEL:
        return None
    raise NotImplementedError(f"FormSection._read_widget 不支持 kind={kind!r}")


def _write_widget(fdef: FieldDef, widget: QWidget, val: Any) -> None:
    """按 kind 写 widget 值;val=None 时不动 widget(空值容忍)。"""
    if val is None:
        return
    kind = fdef.kind
    if kind == FieldKind.INT:
        assert isinstance(widget, QSpinBox)
        widget.setValue(int(val))
        return
    if kind in (FieldKind.TEXT, FieldKind.SECRET, FieldKind.PATH):
        assert isinstance(widget, QLineEdit)
        widget.setText(str(val))
        return
    if kind in (FieldKind.CHOICE, FieldKind.CHOICE_PAIRS):
        assert isinstance(widget, QComboBox)
        idx = widget.findData(val)
        if idx >= 0:
            widget.setCurrentIndex(idx)
        return
    if kind == FieldKind.BOOL:
        assert isinstance(widget, QCheckBox)
        widget.setChecked(bool(val))
        return
    if kind == FieldKind.STATIC_LABEL:
        return
    raise NotImplementedError(f"FormSection._write_widget 不支持 kind={kind!r}")


def _form_layout_iter(form: QFormLayout) -> Iterable[tuple[int, QWidget, QWidget]]:
    """(row_index, label_widget, field_widget) 迭代器 — 给 find_form_row 用。"""
    for i in range(form.rowCount()):
        label_item = form.itemAt(i, QFormLayout.ItemRole.LabelRole)
        field_item = form.itemAt(i, QFormLayout.ItemRole.FieldRole)
        lw = label_item.widget() if label_item else None
        fw = field_item.widget() if field_item else None
        if lw is None and fw is None:
            continue
        yield i, lw or QWidget(), fw or QWidget()


__all__ = [
    "ActionDef",
    "FieldDef",
    "FieldKind",
    "FormSection",
    "find_form_row",
]
