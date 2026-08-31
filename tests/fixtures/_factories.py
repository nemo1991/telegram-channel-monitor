"""`make_message` / `make_photo` 测试数据工厂 — 2026-08-31 v1.5.0 PR #A6。

原 tests/conftest.py::make_message / ::make_photo(行 445-480)。

注意:这些是**工厂函数**,不是 pytest fixture — 不需要 `@pytest.fixture`
装饰,导入即可调。默认 naive datetime(2026-01-01 12:00),与 v1.4.0
起的 aware UTC 产线路径不同 — 见 `_in_memory_repository.list_messages`
的归一化逻辑。
"""

from __future__ import annotations

from datetime import datetime

from tgmonitor.core.dto import MediaDTO, MediaType, MessageDTO


def make_message(
    channel_id: int = 100,
    msg_id: int = 1,
    text: str = "hello",
    media: list[MediaDTO] | None = None,
    date: datetime | None = None,
) -> MessageDTO:
    """构造一条消息 DTO — 默认 naive datetime(测试 fixture 用)。"""
    return MessageDTO(
        id=0,
        channel_id=channel_id,
        telegram_msg_id=msg_id,
        date=date or datetime(2026, 1, 1, 12, 0, 0),
        text=text,
        author="alice",
        media=media or [],
    )


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
                thumb_key="media/abc.jpg.thumb",
                thumb_backend="local",
            )
        ],
    )
