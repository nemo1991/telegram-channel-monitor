"""CLI `login` / `logout` 子命令 — 2.1.0 headless 二维码登录。

设计见 `docs/history/PRD-2.1.0-CLI.md` §5.2 / §6.1。

关键:二维码 URL 含一次性 token,只走 `start_qr_login()` 的 detail 返回值,
渲染成终端二维码写到 stderr,**不进日志、不进事件、不写 stdout**。
`--show-url` 才显式把 URL 打到 stderr。
"""

from __future__ import annotations

import argparse

from tgmonitor.core.app_service import AppService
from tgmonitor.core.monitor.service import MonitorService


async def run_login(
    args: argparse.Namespace,
    *,
    app: AppService | None = None,
    monitor: MonitorService | None = None,
) -> int:
    """headless 二维码登录。返回 0 成功 / 1 失败。"""
    from tgmonitor.cli._common import CliError, cli_context, info
    from tgmonitor.core.config import _user_data_dir
    from tgmonitor.core.settings_store import update_env_credentials

    api_id = getattr(args, "api_id", None)
    api_hash = getattr(args, "api_hash", None)

    # 生产路径(app 未注入):--api-id/--api-hash 必须在 bootstrap **之前**写 .env,
    # 否则组合根会用缺凭据构造 UnconfiguredTelegramClient,start_qr_login 必失败。
    # 只写这 2 个 key,与 GUI 登录按钮语义一致(不覆盖其它未保存字段)。
    if app is None and api_id is not None:
        if api_id <= 0:
            raise CliError("--api-id 必须为正整数")
        if not api_hash or len(api_hash) < 16:
            raise CliError("--api-hash 长度不足(至少 16 字符)")
        update_env_credentials(_user_data_dir() / ".env", api_id=api_id, api_hash=api_hash)

    async with cli_context(args, app=app, monitor=monitor, need_backend=True, skip_ready=True) as (
        ctx,
        _mon,
        settings,
    ):
        del settings
        # 测试注入路径(app 已注入):credential 变更只改内存 settings(client 是 fake)
        if app is not None and api_id is not None:
            ctx.settings.api_id = api_id
            ctx.settings.api_hash = api_hash or ""

        # 预检凭据(AuthService.start_qr_login 内部也会检,这里给更友好的提示)
        if ctx.settings.api_id <= 0 or len(ctx.settings.api_hash) < 16:
            raise CliError(
                "未配置 API 凭据:请设置 TG_API_ID / TG_API_HASH 或用 login --api-id --api-hash"
            )

        state, qr_url = await ctx.auth.start_qr_login()
        if state != "qr_required" or not qr_url:
            raise CliError(f"启动二维码登录失败: {qr_url or state}")

        _render_qr(qr_url, show_url=getattr(args, "show_url", False))
        info("请用 Telegram App 扫码授权(二维码有效期约 30 分钟)…")

        wait_state, detail = await ctx.auth.wait_qr_login()
        if wait_state == "ready":
            info("登录成功")
            return 0
        if detail and "过期" in detail:
            raise CliError("二维码已过期,请重新执行 tgmon-cli login")
        raise CliError(f"登录失败: {detail or wait_state}")


def _render_qr(url: str, *, show_url: bool) -> None:
    """二维码渲染到 stderr(终端 Unicode block)。segno 是直依赖,不碰 Qt。"""
    import sys

    import segno

    qr = segno.make(url, error="m")
    qr.terminal(out=sys.stderr)
    if show_url:
        print(f"[warning] 二维码 URL 含一次性 token,请勿外泄: {url}", file=sys.stderr)


async def run_logout(
    args: argparse.Namespace,
    *,
    app: AppService | None = None,
    monitor: MonitorService | None = None,
) -> int:
    """登出(清本地 session),要求 --yes。"""
    from tgmonitor.cli._common import CliError, cli_context, info

    if not getattr(args, "yes", False):
        raise CliError("logout 会清除本地 session,请加 --yes 确认")

    async with cli_context(args, app=app, monitor=monitor) as (ctx, _mon, _settings):
        await ctx.auth.logout()
    info("已登出")
    return 0
