"""v2.0.7 FR-01/FR-02 契约 — 顶部单行卡 + 状态语义色。

- 顶部 4 张卡(「消息总数」已删),每张只有一行 QHBox;
- 登录态 12 值 + 连接态 5 值全部落入四档语义色(ready/error/pending/unset);
- 大盘状态卡与底部唯一状态位都按当前状态挂 property("state");
- 色号真值源在 style.qss,旧状态点死规则已清理。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QHBoxLayout

from tests.fixtures._real_window import build_real_main_window
from tgmonitor.ui.state_labels import state_color_bucket
from tgmonitor.ui.widgets.dashboard_widget import _StatCard
from tgmonitor.ui.widgets.status_bar import _UnifiedStateLabel

_QSS_PATH = Path(__file__).resolve().parents[1] / "src/tgmonitor/ui/resources/style.qss"


def test_four_single_line_cards(qapp, qloop, tmp_path):
    """FR-01:顶部剩 4 卡,「消息总数」属性已移除,每卡无嵌套行。"""
    win = build_real_main_window(qloop, tmp_path)
    dash = win.dashboard
    assert not hasattr(dash, "card_total_messages")
    for card in (
        dash.card_channels,
        dash.card_monitored,
        dash.card_state,
        dash.card_storage,
    ):
        row = card.layout()
        assert isinstance(row, QHBoxLayout)
        assert all(row.itemAt(i).layout() is None for i in range(row.count()))
    win.close()


@pytest.mark.parametrize(
    ("state", "bucket"),
    [
        # 登录态 12 值
        ("ready", "ready"),
        ("error", "error"),
        ("phone_required", "pending"),
        ("code_required", "pending"),
        ("email_required", "pending"),
        ("email_code_required", "pending"),
        ("registration_required", "pending"),
        ("password_required", "pending"),
        ("closed", "unset"),
        ("logging_out", "unset"),
        ("closing", "unset"),
        ("uninit", "unset"),
        # 连接态 5 值
        ("waiting_for_network", "pending"),
        ("connecting", "pending"),
        ("updating", "pending"),
        ("unknown", "unset"),
    ],
)
def test_state_color_bucket_covers_all_states(state: str, bucket: str) -> None:
    """FR-02:12 + 5 态全集都有明确档位,无 KeyError。"""
    assert state_color_bucket(state) == bucket


def test_unknown_state_falls_back_to_unset() -> None:
    assert state_color_bucket("some_future_state") == "unset"


@pytest.mark.parametrize(
    ("state", "bucket"),
    [
        ("ready", "ready"),
        ("error", "error"),
        ("code_required", "pending"),
        ("uninit", "unset"),
    ],
)
def test_dashboard_state_card_sets_bucket(qapp, state: str, bucket: str) -> None:
    """FR-02:大盘连接状态卡按状态挂 property。"""
    card = _StatCard("连接状态", "nav_live")
    card.set_state(state)
    assert card.value_lbl.property("state") == bucket
    card.deleteLater()


@pytest.mark.parametrize(
    ("state", "bucket"),
    [
        # ready 时颜色随连接态,见 test_status_label_uses_conn_bucket_when_ready
        ("error", "error"),
        ("code_required", "pending"),
        ("uninit", "unset"),
    ],
)
def test_status_label_sets_bucket_on_login_state(qapp, state: str, bucket: str) -> None:
    """FR-02:底部唯一状态位按登录态挂 property。"""
    label = _UnifiedStateLabel()
    label.set_login_state(state)
    assert label.property("state") == bucket
    label.deleteLater()


def test_status_label_uses_conn_bucket_when_ready(qapp) -> None:
    """FR-02:ready 时颜色随连接态而非登录态。"""
    label = _UnifiedStateLabel()
    label.set_login_state("ready")
    label.set_connection_state("connecting")
    assert label.property("state") == "pending"
    label.deleteLater()


def test_qss_has_four_buckets_and_no_dead_rules() -> None:
    """FR-02/清理:两个承载位都有四档色,旧 statusDot/headerState 死规则已删。"""
    qss = _QSS_PATH.read_text(encoding="utf-8")
    for selector in ("#statValue", "#statusStateLabel"):
        for bucket in ("ready", "error", "pending", "unset"):
            assert f'{selector}[state="{bucket}"]' in qss
    assert "#headerState" not in qss
    assert "QFrame#statusDot" not in qss
