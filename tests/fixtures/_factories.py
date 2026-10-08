"""`make_message` / `make_photo` 测试数据工厂 — 2026-08-31 v1.5.0 PR #A6。

原 tests/conftest.py::make_message / ::make_photo(行 445-480)。

2026-10-04 plan 阶段 1.3.D:`make_message` 加 `**overrides` — 之前 5
显式字段(channel_id / msg_id / text / media / date),2026-10-04 改收
任意 MessageDTO 字段,**overrides 优先**。这样多个 test 文件各自定义
`_make_msg(...)` 可以直接 `from tests.fixtures._factories import
make_message` 复用 + 用 `is_favorite=True` / `is_pinned=True` /
`reactions=...` 等任意字段填充,不再保留本地 thin 函数(净减 ~120 LOC)。

注意:这些是**工厂函数**,不是 pytest fixture — 不需要 `@pytest.fixture`
装饰,导入即可调。默认 naive datetime(2026-01-01 12:00),与 v1.4.0
起的 aware UTC 产线路径不同 — 见 `_in_memory_repository.list_messages`
的归一化逻辑。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from tgmonitor.core.dto import MediaDTO, MediaType, MessageDTO


def make_message(
    channel_id: int = 100,
    msg_id: int = 1,
    text: str = "hello",
    media: list[MediaDTO] | None = None,
    date: datetime | None = None,
    **overrides: Any,
) -> MessageDTO:
    """构造一条消息 DTO — 默认 naive datetime(测试 fixture 用)。

    `**overrides` 让任意 MessageDTO 字段(`is_favorite` / `is_pinned` /
    `reactions` / `tags` / ...)直接传进来 — 之前 7 文件各自定义 thin
    `_make_msg(...)` 是被这个局限逼出来的,现在工厂泛化,test 文件
    改 import 即可,不再保留本地包装函数。
    """
    base: dict[str, Any] = {
        "id": 0,
        "channel_id": channel_id,
        "telegram_msg_id": msg_id,
        "date": date or datetime(2026, 1, 1, 12, 0, 0),
        "text": text,
        "author": "alice",
        "media": media or [],
    }
    base.update(overrides)
    return MessageDTO(**base)


def make_photo(channel_id: int = 100, msg_id: int = 1) -> MessageDTO:
    """构造一条带 800x600 jpg 的消息 — 测试 Media Manager / Lightbox 用。"""
    return make_message(
        channel_id=channel_id,
        msg_id=msg_id,
        text="photo!",
        media=[
            MediaDTO(
                type=MediaType.PHOTO,
                mime_type="image/jpeg",
                file_name="pic.jpg",
                file_size=1234,
                width=800,
                height=600,
                # 2026-09-29:thumb_key / thumb_backend 已删(thumb 走独立
                # thumbnails 表 + thumb/ prefix);改 thumbnail_telegram_file_id
                # 关联 TG 端 Thumbnail.file.id。
                thumbnail_telegram_file_id="33",
            )
        ],
    )
