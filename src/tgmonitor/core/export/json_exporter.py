"""JSON Exporter — 完整 DTO 序列化,结构化、可程序消费。

2026-11-25 P2-2.1.4 #12:渲染前显式序列化 — Enum → `.value`(DB 内部值,
如 "photo" / "done")、datetime → isoformat;`default=str` 仅作未知类型
最终兜底。

向后兼容注:新导出文件中枚举字段以 **DB 内部字符串值** 出现
("photo" / "done",与 CSV / ZIP manifest 一致),不是 "MediaType.PHOTO"
这类 repr 形式;消费方按字符串值解析。datetime 一律 ISO 8601
(旧实现 ChannelDTO.created_at / last_synced_at 落进 `default=str`,
输出 "2026-01-01 12:00:00+00:00" 空格分隔,现为标准 isoformat)。

实现说明:旧代码的 `isinstance(media.get("type"), str) is False` 分支
永远不生效(MediaType 是 str 子类,isinstance 恒真)— 已删除,统一走
`_json_safe` 递归转换,不再依赖 str-Enum 在 json.dumps 里的原生 str
路径这种隐式行为。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tgmonitor.core.dto import ChannelDTO, ExportFormat, MessageDTO
from tgmonitor.core.export.base import Exporter, exporter

if TYPE_CHECKING:
    from tgmonitor.core.objectstore.base import ObjectStore
    from tgmonitor.core.storage.repository import StorageRepository


def _json_safe(obj: Any) -> Any:
    """递归把 Enum → `.value`、datetime → isoformat;其余原样透传。

    json.dumps 的 `default=str` 仍保留作未知类型的最终兜底(如 raw
    dict 里的异常对象),但 Enum / datetime 不再依赖它。
    """
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


@exporter(ExportFormat.JSON)
class JsonExporter(Exporter):
    """JSON Exporter — 完整 DTO 序列化(`schema: tgmonitor.export/v1`)。

    结构化、可程序消费;Enum 显式转 `.value`、datetime 转 isoformat
    (见模块 docstring 的向后兼容注)。
    """

    format = ExportFormat.JSON
    extension = ".json"

    async def render(
        self,
        out_path: Path,
        channels: dict[int, ChannelDTO],
        messages: list[MessageDTO],
        *,
        object_store: ObjectStore | None = None,
        include_thumbnails: bool = False,
        include_metadata: bool = True,
        storage: StorageRepository | None = None,  # 2026-09-29:缩略图独立表读用(JSON 不用,签名对齐)
    ) -> int:
        """写 JSON → 返回字节数。

        `object_store` / `include_thumbnails` JSON 不用,仅保形。
        `include_metadata`(v1.7.3)对 JSON 是 no-op — asdict 总是含全部字段
        (is_favorite / tags / notes),否则用户得做 `if False` 删字段,
        反而更脆。协议签名加这个 kwarg 是为了与 ABC 对齐。
        """
        del include_metadata  # JSON 永远含 — 用户拿到完整 DTO
        payload = {
            "schema": "tgmonitor.export/v1",
            "channels": [_json_safe(asdict(c)) for c in channels.values()],
            "messages": [_json_safe(asdict(m)) for m in messages],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        out_path.write_text(text, encoding="utf-8")  # noqa: ASYNC240 — 同 csv/html,写文件同步即可
        return out_path.stat().st_size  # noqa: ASYNC240 — 同上
