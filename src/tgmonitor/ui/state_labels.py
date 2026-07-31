"""state_labels.py — login state → (dot, label) 单源映射。

之前 3 处各自维护:
  1. `main_window.py:_STATE_DOT` (9 状态)
  2. `main_window.py:_STATE_LABEL` (9 状态)
  3. `dashboard_widget.py:L284` 内联 dict (3 状态,缺 6 状态)
  4. `dashboard_widget.py:L344-353` if/elif chain (3 状态,跟 #3 不一致)

抽 4 个表到 module,所有 caller 走同一 source of truth:
  - `state_dot(state)` — 圆点 emoji(🟢 / 🔴 / 🟡 / ⚪ / ⏳)
  - `state_label(state)` — 状态文本(已登录 / 错误 / 未登录 / 需验证码 / …)
  - `state_badge(state)` — 圆点 + 标签 拼接 ("🟢 已登录"),给 dashboard card
  - `state_hint(state)` — 行动建议文字(给新用户引导)

新增状态时,只需在 `STATE_DOT` / `STATE_LABEL` 各加一行,任意 caller 复用。
"""
from __future__ import annotations

# 9 个 TDLib 登录状态 — 上游 `TgLibTelegramClient.start()` 返回值 + 状态机过渡。
# 完整流程:uninit → phone_required → code_required → password_required → ready
#           ready → logging_out → closed
# 错误:任意状态 → error →  待手动 close
# 关闭:任意状态 → closing → closed
STATE_DOT: dict[str, str] = {
    "ready": "🟢",
    "error": "🔴",
    "phone_required": "🟡",
    "code_required": "🟡",
    "password_required": "🟡",
    "closed": "⚪",
    "logging_out": "⏳",
    "closing": "⏳",
    "uninit": "⚪",
}

STATE_LABEL: dict[str, str] = {
    "ready": "已登录",
    "error": "错误",
    "phone_required": "未登录",
    "code_required": "需验证码",
    "password_required": "需 2FA",
    "closed": "会话关闭",
    "logging_out": "登出中…",
    "closing": "关闭中…",
    "uninit": "启动中…",
}

# 行动建议 — 给 dashboard card 副标题 / 新用户空状态引导
STATE_HINT: dict[str, str] = {
    "ready": "实时接收订阅频道的新消息",
    "error": "点击设置 → 账户 检查凭据 / 重启",
    "phone_required": "点击设置 → 账户 填写 API ID / Hash / 手机号",
    "code_required": "弹窗已出 — 输入 Telegram 验证码",
    "password_required": "弹窗已出 — 输入 2FA 密码",
    "closed": "已登出;打开设置 → 账户 重新登录",
    "logging_out": "正在登出…",
    "closing": "正在关闭…",
    "uninit": "未启动 — 正常情况会在 1-2 秒内到 ready",
}


def state_dot(state: str) -> str:
    """圆点 emoji;未知状态返 ⚪。"""
    return STATE_DOT.get(state, "⚪")


def state_label(state: str) -> str:
    """状态文本;未知状态原样返回(caller 看到 raw 状态名比空字符串更易排错)。"""
    return STATE_LABEL.get(state, state)


def state_badge(state: str) -> str:
    """`圆点 + 标签` 拼接,给 dashboard card 主标题。

    未知状态返 `⚪ {state}`(跟 dashboard 旧行为一致)。
    """
    if state in STATE_DOT:
        return f"{STATE_DOT[state]} {STATE_LABEL[state]}"
    return f"⚪ {state}"


def state_hint(state: str) -> str:
    """行动建议;未知状态空字符串(避免误导)。"""
    return STATE_HINT.get(state, "")
