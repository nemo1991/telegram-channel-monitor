"""v2.0.6 频道域命名去歧义守卫（FR-07 G7）。

锁定「频道域 subscribe 词根 + 语义=监听」已全量正名为 monitored 词根，
同时确保 EventBus 事件订阅域与 TG 订阅 API 零误伤。

历史背景：v2.0.3 已确立术语「订阅=TG 侧已加入(tg_subscribed)、监听=
monitor 白名单」；2.0.6 把频道域里用 subscribe 词根表达监听的标识符
一次清干净，本测试防止未来回潮。

2026-11-19 补：UI 层 `MonitorViewModel.known_channels`（语义=仅已监听，
名实不符且诱发左栏覆盖 bug）同批正名为 `monitored_channels`，本文件
一并守卫其不回潮。
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"

# 频道域里已被正名的旧标识符（全部应归零）
_BANNED = (
    "subscribe_channel",
    "unsubscribe_channel",
    "list_subscribed_channels",
    "set_channel_subscribed",
    "refresh_subscribed_channels",
    "include_unsubscribed",
    "subscribed_ids",  # 覆盖 _subscribed_ids
    "_on_channel_subscribed",
    "_on_channel_unsubscribed",
    "no_subscribed",
    "lst_subscribed",
    "card_subscribed",
    "subscribed_count",
    "set_subscribed",
    "_subscribed_ids",
    "_on_subscribed_double_click",
    "_add_to_subscribed_list",
    "_remove_from_subscribed_list",
)

# 必须保留的「事件订阅域 / TG 订阅」标识符（误伤即失败）
_REQUIRED = (
    "subscribe_updates",  # telegram client 更新流
    "subscribe_all",  # EventBus
    "set_channel_tg_subscribed",  # TG 订阅
    "count_tg_subscribed",
    "is_tg_subscribed",
)


def _src_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _SRC.rglob("*.py"))


def test_channel_domain_subscribe_names_are_gone():
    """G7：频道域旧 subscribe 标识符全部归零。"""
    text = _src_text()
    hits = [name for name in _BANNED if re.search(rf"\b{re.escape(name)}\b", text)]
    assert hits == [], f"频道域旧标识符未清干净: {hits}"


def test_event_domain_and_tg_subscribe_not_touched():
    """G7 反向：事件订阅域与 TG 订阅 API 零误伤。"""
    text = _src_text()
    missing = [name for name in _REQUIRED if name not in text]
    assert missing == [], f"事件域/TG 订阅标识符被误改: {missing}"


def test_ui_known_channels_alias_is_gone():
    """2026-11-19：UI 层 `known_channels`（语义=仅已监听）已正名为
    `monitored_channels`，防止名实不符的旧名回潮再次诱发左栏覆盖 bug。"""
    offenders = []
    for p in _SRC.rglob("*.py"):
        for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "known_channels" in line:
                offenders.append(f"{p.relative_to(_SRC).as_posix()}:{lineno}")
    assert offenders == [], f"UI 层旧名 known_channels 回潮: {offenders}"

    # 反向：正名后的字段确实存在（误改/漏改即失败）
    text = _src_text()
    assert "monitored_channels" in text, "monitored_channels 正名未落地"
