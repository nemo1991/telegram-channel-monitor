"""入口:`python -m tgmonitor` 启动桌面应用。

仅作为薄壳:实际工作在 `app.py` 的 `run()` 里,这里只处理顶层异常与退出码。
"""
from __future__ import annotations

import sys


def main() -> int:
    from tgmonitor.app import run

    try:
        run()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"[tgmonitor] fatal: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
