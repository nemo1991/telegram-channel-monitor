"""core.media_service package — 2026-10-15 plan-batch6 拆 facade。

`MediaService` 是 Media Manager 域的门面,内部按职责拆 5 个子 module:
- `_qt_bridge` (本目录) — module-level Qt callback 注入(state + 4 function)
- `_media_crud` — list_media / delete_media / preview_delete_by_channel /
  delete_by_channel
- `_media_bytes` — load_thumbnail_bytes / _read_objectstore_bytes /
  load_media_bytes
- `_media_os_actions` — open_media / open_media_with_result / reveal_in_folder
  / _spawn_reveal / copy_media_path / _stage_s3_to_tmp / retry_media
- `_media_reconcile` — reconcile_orphans

`MediaService` 退化为主类 facade,公共 method 1 行 thin delegate,签名
1:1 保留(向后兼容,UI / `_media_actions.py` 等调用面零变化)。

注意:外部 import 路径 `tgmonitor.core.media_service.MediaService` 保持
不变(本 `__init__.py` re-export)。
"""

from __future__ import annotations

from tgmonitor.core.media_service.media_service import MediaService

__all__ = ["MediaService"]
