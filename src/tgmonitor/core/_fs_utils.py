"""Filesystem 小工具 — 给 settings cache UI + clear_channel_preview_dialog 共享。

抽出来是 2026-09-24 v1.8.3:之前 `_format_bytes` 只在 clear_channel_preview_dialog
有,settings 加 cache UI 后第二处要,直接 import 此处避免重复实现。
"""

from __future__ import annotations

import os
from pathlib import Path


def dir_size(path: Path) -> int:
    """递归累加 path 下所有 regular file 的 st_size。Path 不存在返 0。

    同步阻塞 — 调用方需包 `asyncio.to_thread`(避免阻塞 qasync 主 loop)。
    OSError 单文件 stat 失败不抛,跳过继续累加其他文件。
    """
    if not path.exists():
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            try:
                total += (Path(dirpath) / fn).stat().st_size
            except OSError:
                # 文件被删 / 权限抖 — 跳过继续
                pass
    return total


def format_bytes(n: int) -> str:
    """人类可读字节数。`n==0` 走「0 B」。"""
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(n)
    i = 0
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{size:.1f}{units[i]}" if i > 0 else f"{int(size)}B"
