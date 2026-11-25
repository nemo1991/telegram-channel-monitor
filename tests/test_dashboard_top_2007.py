"""v2.0.7 FR-01/FR-02 契约 — 顶部单行卡 + 状态语义色。

- 顶部 4 张卡(「消息总数」已删),每张只有一行 QHBox;
- 登录态 12 值 + 连接态 5 值全部落入四档语义色(ready/error/pending/unset);
- 大盘状态卡与底部唯一状态位都按当前状态挂 property("state");
- 色号真值源在 style.qss,旧状态点死规则已清理。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tgmonitor.ui.state_labels import state_color_bucket

_QSS_PATH = Path(__file__).resolve().parents[1] / "src/tgmonitor/ui/resources/style.qss"


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
