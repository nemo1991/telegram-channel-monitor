"""TelethonTelegramClient 离线单测 — v2.0.0(mock Telethon client,零网络/凭据)。

覆盖:
- 凭据守卫 / 状态机唯一写路径
- 鉴权 request/response:start / submit_phone / code / password / email /
  email_code / registration / logout(含 401 重置 session)
- lifecycle:stop / close / _check_alive
- 频道:list_joined_channels(best-effort)/ get_channel_metadata / join_channel
- iter_chat_history:ChatUnavailableError / FloodWaitError → TelegramRateLimitError
- download_file:非法 id / 无媒体 / 成功 / 缩略图 / 进度终值
- 实时 handler:new/edited → stream;deleted/views/reactions/pin/title/photo → bus
- 批量操作:mark_read / forward / pin / unpin / add/remove reaction
- watchdog:ConnectionStateChanged 合成
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon import errors, functions, types

from tgmonitor.core.config import Settings
from tgmonitor.core.events import (
    AuthErrorOccurred,
    ChannelMetadataChanged,
    ChannelPhotoChanged,
    ChannelTitleChanged,
    ConnectionStateChanged,
    EventBus,
    LoginStateChanged,
    MessageDeleted,
    MessageInteractionsChanged,
    MessagePinChanged,
)
from tgmonitor.core.telegram.errors import (
    ChatUnavailableError,
    ClientClosingError,
    TelegramNotConfiguredError,
    TelegramRateLimitError,
)
from tgmonitor.core.telegram.telethon_client import TelethonTelegramClient
from tgmonitor.core.telegram.telethon_messages import encode_file_id

_DATE = datetime(2026, 1, 1, tzinfo=UTC)
_CID = -1000000000123


def _async_iter(items: list):
    async def gen():
        for it in items:
            yield it

    return gen()


def _make_mock_client() -> MagicMock:
    """MagicMock 底座:异步方法显式 AsyncMock,is_connected 保持同步。

    `await client(request)` 走 `side_effect`(AsyncMock)— 测试改
    `client.side_effect.return_value` 即可控制裸 RPC 返回。
    """
    c = MagicMock()
    c.side_effect = AsyncMock(return_value=None)
    c.connect = AsyncMock()
    c.disconnect = AsyncMock()
    c.is_user_authorized = AsyncMock(return_value=False)
    c.get_me = AsyncMock(return_value=SimpleNamespace(id=1, username="me", first_name="Me"))
    c.send_code_request = AsyncMock()
    c.sign_in = AsyncMock()
    c.log_out = AsyncMock()
    c.is_connected = MagicMock(return_value=True)
    c.get_entity = AsyncMock()
    c.get_messages = AsyncMock(return_value=None)
    c.download_media = AsyncMock(return_value=b"data")
    c.iter_dialogs = MagicMock(return_value=_async_iter([]))
    c.iter_messages = MagicMock(return_value=_async_iter([]))
    c.send_read_acknowledge = AsyncMock()
    c.forward_messages = AsyncMock()
    c.pin_message = AsyncMock()
    c.unpin_message = AsyncMock()
    c.get_input_entity = AsyncMock(return_value=types.InputPeerChannel(_CID, 1))
    c.add_event_handler = MagicMock()
    return c


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def settings() -> Settings:
    return Settings.for_test()


@pytest.fixture
async def wrapper(settings, bus, monkeypatch):
    w = TelethonTelegramClient(settings, event_bus=bus)
    mock = _make_mock_client()
    monkeypatch.setattr(w, "_build_client", lambda: mock)
    w._client = mock
    yield w, mock
    await w._cancel_watchdog()


def _channel(cid: int = 123, **kw) -> types.Channel:
    base = dict(
        id=cid,
        title="频道",
        photo=None,
        broadcast=True,
        megagroup=False,
        date=_DATE,
        username="chan",
        participants_count=100,
    )
    base.update(kw)
    return types.Channel(**base)


def _sent_code(type_obj) -> types.auth.SentCode:
    return types.auth.SentCode(type=type_obj, phone_code_hash="hash123")


# ---- 凭据守卫 / 初始状态 ----


def test_missing_credentials_raises() -> None:
    with pytest.raises(TelegramNotConfiguredError):
        TelethonTelegramClient(Settings.for_test(api_id=0))


def test_initial_state_uninit(wrapper) -> None:
    w, _ = wrapper
    assert w.state == "uninit"
    assert w.me is None


async def test_set_state_publishes_login_event(wrapper, bus) -> None:
    w, _ = wrapper
    received: list[LoginStateChanged] = []
    bus.subscribe(LoginStateChanged, lambda e: _collect(received, e))
    w._set_state("ready")
    w._set_state("ready")  # 重复同状态 → 不再发
    await bus.flush()
    assert len(received) == 1
    assert received[0].state == "ready"


async def _collect(lst: list, e: Any) -> None:
    lst.append(e)


# ---- start ----


async def test_start_authorized_ready(wrapper, bus) -> None:
    w, mock = wrapper
    mock.is_user_authorized.return_value = True
    state, _ = await w.start()
    assert state == "ready"
    assert w.me == {"id": 1, "username": "me", "first_name": "Me"}
    mock.connect.assert_awaited_once()


async def test_start_unauthorized_phone_required(wrapper) -> None:
    w, mock = wrapper
    mock.is_user_authorized.return_value = False
    state, _ = await w.start()
    assert state == "phone_required"
    assert w.me is None


async def test_start_connect_timeout_error(wrapper, monkeypatch) -> None:
    w, mock = wrapper
    w._BOOT_TIMEOUT = 0.05

    async def _slow_connect():
        import asyncio

        await asyncio.sleep(1)

    mock.connect.side_effect = _slow_connect
    state, detail = await w.start()
    assert state == "error"
    assert "超时" in detail


async def test_start_401_resets_session(wrapper) -> None:
    w, mock = wrapper
    # 第一次 connect 抛 UnauthorizedError → reset → 第二次成功(未授权)
    mock.connect.side_effect = [
        errors.UnauthorizedError(request=None, message="AUTH_KEY_UNREGISTERED"),
        None,
    ]
    mock.is_user_authorized.return_value = False
    state, _ = await w.start()
    assert state == "phone_required"
    assert mock.connect.await_count == 2


async def test_start_proxy_unreachable_error(wrapper, monkeypatch) -> None:
    w, _ = wrapper
    w._settings.proxy = "socks5://127.0.0.1:1"

    async def _bad_probe(url, **_kw):
        return False, "代理 TCP 不通"

    monkeypatch.setattr("tgmonitor.core.telegram.telethon_client.probe_proxy", _bad_probe)
    state, detail = await w.start()
    assert state == "error"
    assert "代理" in detail


# ---- submit_phone ----


async def test_submit_phone_code_required(wrapper) -> None:
    w, mock = wrapper
    mock.send_code_request.return_value = _sent_code(types.auth.SentCodeTypeApp(length=5))
    state, _ = await w.submit_phone("+10000000000")
    assert state == "code_required"
    assert w._phone_code_hash == "hash123"


async def test_submit_phone_email_code_login(wrapper) -> None:
    w, mock = wrapper
    mock.send_code_request.return_value = _sent_code(
        types.auth.SentCodeTypeEmailCode(email_pattern="a***@b.com", length=5)
    )
    state, _ = await w.submit_phone("+10000000000")
    assert state == "email_code_required"
    assert w._email_flow == "login"


async def test_submit_phone_setup_email(wrapper) -> None:
    w, mock = wrapper
    mock.send_code_request.return_value = _sent_code(types.auth.SentCodeTypeSetUpEmailRequired())
    state, _ = await w.submit_phone("+10000000000")
    assert state == "email_required"
    assert w._email_flow == "setup"


async def test_submit_phone_error_emits_auth_error(wrapper, bus) -> None:
    w, mock = wrapper
    mock.send_code_request.side_effect = errors.PhoneNumberInvalidError(request=None)
    received: list[AuthErrorOccurred] = []
    bus.subscribe(AuthErrorOccurred, lambda e: _collect(received, e))
    state, _ = await w.submit_phone("bad")
    assert state == "uninit"  # 顶层状态不变
    await bus.flush()
    assert len(received) == 1
    assert received[0].source == "phone"


async def test_submit_phone_401_resets_and_retries(wrapper) -> None:
    w, mock = wrapper
    mock.send_code_request.side_effect = [
        errors.UnauthorizedError(request=None, message="AUTH_KEY_UNREGISTERED"),
        _sent_code(types.auth.SentCodeTypeApp(length=5)),
    ]
    state, _ = await w.submit_phone("+10000000000")
    assert state == "code_required"
    assert mock.send_code_request.await_count == 2


# ---- submit_code ----


async def test_submit_code_success_ready(wrapper) -> None:
    w, mock = wrapper
    w._phone = "+10000000000"
    w._phone_code_hash = "hash123"
    state, _ = await w.submit_code("12345")
    assert state == "ready"
    mock.sign_in.assert_awaited_once()


async def test_submit_code_2fa_password_required(wrapper) -> None:
    w, mock = wrapper
    mock.sign_in.side_effect = errors.SessionPasswordNeededError(request=None)
    state, _ = await w.submit_code("12345")
    assert state == "password_required"


async def test_submit_code_unoccupied_registration(wrapper) -> None:
    w, mock = wrapper
    mock.sign_in.side_effect = errors.PhoneNumberUnoccupiedError(request=None)
    state, _ = await w.submit_code("12345")
    assert state == "registration_required"


async def test_submit_code_invalid_stays(wrapper, bus) -> None:
    w, mock = wrapper
    w._state = "code_required"
    mock.sign_in.side_effect = errors.PhoneCodeInvalidError(request=None)
    received: list[AuthErrorOccurred] = []
    bus.subscribe(AuthErrorOccurred, lambda e: _collect(received, e))
    state, _ = await w.submit_code("wrong")
    assert state == "code_required"
    await bus.flush()
    assert received[0].source == "code"


# ---- submit_password ----


async def test_submit_password_success(wrapper) -> None:
    w, mock = wrapper
    state, _ = await w.submit_password("pw")
    assert state == "ready"
    mock.sign_in.assert_awaited_once_with(password="pw")


async def test_submit_password_wrong_stays(wrapper, bus) -> None:
    w, mock = wrapper
    w._state = "password_required"
    mock.sign_in.side_effect = errors.PasswordHashInvalidError(request=None)
    received: list[AuthErrorOccurred] = []
    bus.subscribe(AuthErrorOccurred, lambda e: _collect(received, e))
    state, _ = await w.submit_password("bad")
    assert state == "password_required"
    await bus.flush()
    assert received[0].source == "password"


# ---- submit_email / email_code ----


async def test_submit_email_setup_flow(wrapper) -> None:
    w, mock = wrapper
    w._phone = "+10000000000"
    w._phone_code_hash = "hash123"
    state, _ = await w.submit_email("a@b.com")
    assert state == "email_code_required"
    assert w._email_flow == "setup"
    req = mock.call_args[0][0]
    assert isinstance(req, functions.account.SendVerifyEmailCodeRequest)
    assert req.email == "a@b.com"


async def test_submit_email_code_login_flow_signs_in(wrapper) -> None:
    w, mock = wrapper
    w._email_flow = "login"
    w._phone = "+10000000000"
    w._phone_code_hash = "hash123"
    state, _ = await w.submit_email_code("99999")
    assert state == "ready"
    mock.sign_in.assert_awaited_once()


async def test_submit_email_code_setup_flow_verifies(wrapper) -> None:
    w, mock = wrapper
    w._email_flow = "setup"
    w._phone = "+10000000000"
    w._phone_code_hash = "hash123"
    mock.side_effect.return_value = types.account.EmailVerifiedLogin(
        email="a@b.com",
        sent_code=_sent_code(types.auth.SentCodeTypeApp(length=5)),
    )
    state, _ = await w.submit_email_code("99999")
    assert state == "code_required"
    assert w._phone_code_hash == "hash123"
    req = mock.call_args[0][0]
    assert isinstance(req, functions.account.VerifyEmailRequest)


# ---- submit_registration ----


async def test_submit_registration_ready(wrapper) -> None:
    w, mock = wrapper
    w._phone = "+10000000000"
    w._phone_code_hash = "hash123"
    mock.side_effect.return_value = SimpleNamespace(
        user=SimpleNamespace(id=2, username="new", first_name="New")
    )
    state, _ = await w.submit_registration("New", "User")
    assert state == "ready"
    assert w.me["id"] == 2
    req = mock.call_args[0][0]
    assert isinstance(req, functions.auth.SignUpRequest)
    assert req.first_name == "New"


# ---- logout ----


async def test_logout_resets_to_phone_required(wrapper) -> None:
    w, mock = wrapper
    w._state = "ready"
    w._me = {"id": 1}
    await w.logout()
    assert w.state == "phone_required"
    assert w.me is None
    mock.log_out.assert_awaited_once()
    mock.connect.assert_awaited()  # 重建后 reconnect


# ---- lifecycle ----


async def test_close_sets_closing_and_guard(wrapper) -> None:
    w, mock = wrapper
    await w.close()
    assert w._closing is True
    with pytest.raises(ClientClosingError):
        w._check_alive()
    with pytest.raises(ClientClosingError):
        await w.submit_phone("+1")


async def test_stop_keeps_client_resets_state(wrapper) -> None:
    w, mock = wrapper
    w._state = "ready"
    await w.stop()
    assert w.state == "uninit"
    assert w._closing is False
    assert w._client is mock  # 实例保留
    mock.disconnect.assert_awaited()


async def test_stop_closes_streams(wrapper) -> None:
    w, _ = wrapper
    s = w.subscribe_updates()
    assert len(w._streams) == 1
    await w.stop()
    assert w._streams == []
    with pytest.raises(StopAsyncIteration):
        await s.__anext__()


# ---- list_joined_channels ----


async def test_list_joined_channels_closing_returns_empty(wrapper) -> None:
    w, _ = wrapper
    w._closing = True
    assert await w.list_joined_channels() == []


async def test_list_joined_channels_maps_entities(wrapper) -> None:
    w, mock = wrapper
    w._state = "ready"
    chat = types.Chat(id=55, title="群", photo=None, participants_count=10, date=_DATE, version=1)
    user = types.User(id=7, access_hash=1, first_name="人")
    mock.iter_dialogs.return_value = _async_iter(
        [
            SimpleNamespace(entity=_channel()),
            SimpleNamespace(entity=chat),
            SimpleNamespace(entity=user),  # 私聊 → 跳过
        ]
    )
    result = await w.list_joined_channels()
    assert len(result) == 2
    assert result[0].kind == "channel"
    assert result[0].id == _CID
    assert result[0].username == "chan"
    assert result[1].kind == "basic_group"
    assert result[1].id == -55


async def test_list_joined_channels_supergroup_kind(wrapper) -> None:
    w, mock = wrapper
    w._state = "ready"
    sg = _channel(broadcast=False, megagroup=True)
    mock.iter_dialogs.return_value = _async_iter([SimpleNamespace(entity=sg)])
    result = await w.list_joined_channels()
    assert result[0].kind == "supergroup"


# ---- get_channel_metadata ----


async def test_get_channel_metadata_full_count(wrapper) -> None:
    w, mock = wrapper
    mock.get_entity.return_value = _channel(participants_count=None)
    mock.side_effect.return_value = SimpleNamespace(
        full_chat=SimpleNamespace(participants_count=5000)
    )
    dto = await w.get_channel_metadata(_CID)
    assert dto.member_count == 5000
    assert dto.title == "频道"


async def test_get_channel_metadata_not_found_stub(wrapper) -> None:
    w, mock = wrapper
    mock.get_entity.side_effect = ValueError("no entity")
    dto = await w.get_channel_metadata(_CID)
    assert dto.title == f"#{_CID}"


# ---- join_channel ----


async def test_join_channel_username(wrapper) -> None:
    w, mock = wrapper
    mock.get_entity.return_value = _channel()
    dto = await w.join_channel("@chan")
    assert dto.id == _CID
    mock.get_entity.assert_awaited_with("chan")
    req = mock.call_args[0][0]
    assert isinstance(req, functions.channels.JoinChannelRequest)


async def test_join_channel_invite_link(wrapper) -> None:
    w, mock = wrapper
    mock.side_effect.return_value = SimpleNamespace(chats=[_channel()])
    dto = await w.join_channel("https://t.me/+abc123")
    assert dto.id == _CID
    req = mock.call_args[0][0]
    assert isinstance(req, functions.messages.ImportChatInviteRequest)
    assert req.hash == "abc123"


# ---- iter_chat_history ----


async def test_iter_chat_history_yields_mapped(wrapper) -> None:
    w, mock = wrapper
    mock.get_entity.return_value = _channel()
    msg = types.Message(id=10, peer_id=types.PeerChannel(123), date=_DATE, message="hi")
    mock.iter_messages.return_value = _async_iter([msg])
    out = [m async for m in w.iter_chat_history(_CID)]
    assert len(out) == 1
    assert out[0].telegram_msg_id == 10
    assert out[0].text == "hi"


async def test_iter_chat_history_unavailable(wrapper) -> None:
    w, mock = wrapper
    mock.get_entity.side_effect = errors.ChannelPrivateError(request=None)
    with pytest.raises(ChatUnavailableError):
        _ = [m async for m in w.iter_chat_history(_CID)]


async def test_iter_chat_history_value_error_unavailable(wrapper) -> None:
    w, mock = wrapper
    mock.get_entity.side_effect = ValueError("not found")
    with pytest.raises(ChatUnavailableError):
        _ = [m async for m in w.iter_chat_history(_CID)]


async def test_iter_chat_history_flood_wait_translated(wrapper) -> None:
    w, mock = wrapper
    mock.get_entity.return_value = _channel()

    async def _flood():
        raise errors.FloodWaitError(request=None, capture=30)
        yield  # pragma: no cover

    mock.iter_messages.return_value = _flood()
    with pytest.raises(TelegramRateLimitError) as ei:
        _ = [m async for m in w.iter_chat_history(_CID)]
    assert ei.value.retry_after_seconds == 30


# ---- download_file ----


async def test_download_file_invalid_id(wrapper) -> None:
    w, _ = wrapper
    assert await w.download_file("12345") is None  # 旧数字 id


async def test_download_file_no_message(wrapper) -> None:
    w, mock = wrapper
    mock.get_messages.return_value = None
    assert await w.download_file(encode_file_id(_CID, 10)) is None


async def test_download_file_success(wrapper) -> None:
    w, mock = wrapper
    doc = types.Document(
        id=1,
        access_hash=1,
        file_reference=b"\x00",
        date=_DATE,
        mime_type="video/mp4",
        size=4,
        dc_id=2,
        attributes=[],
    )
    mock.get_messages.return_value = SimpleNamespace(media=types.MessageMediaDocument(document=doc))
    mock.download_media.return_value = b"data"
    out = await w.download_file(encode_file_id(_CID, 10))
    assert out == b"data"
    mock.get_messages.assert_awaited_with(_CID, ids=10)


async def test_download_file_thumb_document(wrapper) -> None:
    w, mock = wrapper
    doc = types.Document(
        id=1,
        access_hash=1,
        file_reference=b"\x00",
        date=_DATE,
        mime_type="video/mp4",
        size=4,
        dc_id=2,
        attributes=[],
    )
    mock.get_messages.return_value = SimpleNamespace(media=types.MessageMediaDocument(document=doc))
    await w.download_file(encode_file_id(_CID, 10, thumb=True))
    assert mock.download_media.call_args.kwargs["thumb"] == -1


async def test_download_file_progress_final_emit(wrapper) -> None:
    w, mock = wrapper
    doc = types.Document(
        id=1,
        access_hash=1,
        file_reference=b"\x00",
        date=_DATE,
        mime_type="video/mp4",
        size=4,
        dc_id=2,
        attributes=[],
    )
    mock.get_messages.return_value = SimpleNamespace(media=types.MessageMediaDocument(document=doc))
    calls: list[tuple[int, int | None]] = []

    async def cb(downloaded: int, total: int | None) -> None:
        calls.append((downloaded, total))

    out = await w.download_file(encode_file_id(_CID, 10), progress_callback=cb)
    assert out == b"data"
    assert (4, 4) in calls  # 终值


async def test_download_file_failure_returns_none(wrapper) -> None:
    w, mock = wrapper
    doc = types.Document(
        id=1,
        access_hash=1,
        file_reference=b"\x00",
        date=_DATE,
        mime_type="video/mp4",
        size=4,
        dc_id=2,
        attributes=[],
    )
    mock.get_messages.return_value = SimpleNamespace(media=types.MessageMediaDocument(document=doc))
    mock.download_media.side_effect = RuntimeError("network")
    assert await w.download_file(encode_file_id(_CID, 10)) is None


# ---- 实时 handler ----


async def test_on_new_message_pushes_stream(wrapper) -> None:
    w, _ = wrapper
    stream = w.subscribe_updates()
    msg = types.Message(id=10, peer_id=types.PeerChannel(123), date=_DATE, message="hi")
    await w._on_new_message(SimpleNamespace(message=msg))
    dto = await stream.__anext__()
    assert dto.telegram_msg_id == 10
    await stream.aclose()


async def test_on_message_deleted_publishes(wrapper, bus) -> None:
    w, _ = wrapper
    received: list[MessageDeleted] = []
    bus.subscribe(MessageDeleted, lambda e: _collect(received, e))
    event = SimpleNamespace(chat_id=_CID, deleted_ids=[1, 2])
    await w._on_message_deleted(event)
    await bus.flush()
    assert [e.telegram_msg_id for e in received] == [1, 2]


async def test_on_message_deleted_no_peer_skips(wrapper, bus) -> None:
    w, _ = wrapper
    received: list[MessageDeleted] = []
    bus.subscribe(MessageDeleted, lambda e: _collect(received, e))
    await w._on_message_deleted(SimpleNamespace(chat_id=None, deleted_ids=[1]))
    await bus.flush()
    assert received == []


async def test_on_views_publishes(wrapper, bus) -> None:
    w, _ = wrapper
    received: list[MessageInteractionsChanged] = []
    bus.subscribe(MessageInteractionsChanged, lambda e: _collect(received, e))
    await w._on_views(types.UpdateChannelMessageViews(channel_id=123, id=10, views=999))
    await bus.flush()
    assert received[0].channel_id == _CID
    assert received[0].views == 999
    assert received[0].reactions is None


async def test_on_reactions_publishes(wrapper, bus) -> None:
    w, _ = wrapper
    received: list[MessageInteractionsChanged] = []
    bus.subscribe(MessageInteractionsChanged, lambda e: _collect(received, e))
    update = types.UpdateMessageReactions(
        peer=types.PeerChannel(123),
        msg_id=10,
        reactions=types.MessageReactions(
            results=[types.ReactionCount(reaction=types.ReactionEmoji(emoticon="🔥"), count=2)]
        ),
    )
    await w._on_reactions(update)
    await bus.flush()
    assert received[0].views is None
    assert received[0].reactions[0].emoji == "🔥"


async def test_on_pinned_channel_publishes(wrapper, bus) -> None:
    w, _ = wrapper
    received: list[MessagePinChanged] = []
    bus.subscribe(MessagePinChanged, lambda e: _collect(received, e))
    await w._on_pinned_channel(
        types.UpdatePinnedChannelMessages(
            channel_id=123, messages=[10, 11], pts=1, pts_count=1, pinned=True
        )
    )
    await bus.flush()
    assert len(received) == 2
    assert all(e.is_pinned for e in received)
    assert received[0].channel_id == _CID


async def test_on_chat_action_title(wrapper, bus) -> None:
    w, _ = wrapper
    received: list[ChannelTitleChanged] = []
    bus.subscribe(ChannelTitleChanged, lambda e: _collect(received, e))
    await w._on_chat_action(
        SimpleNamespace(chat_id=_CID, new_title="新名", new_photo=False, photo=None)
    )
    await bus.flush()
    assert received[0].new_title == "新名"


async def test_on_chat_action_photo_removed(wrapper, bus) -> None:
    w, _ = wrapper
    received: list[ChannelPhotoChanged] = []
    bus.subscribe(ChannelPhotoChanged, lambda e: _collect(received, e))
    await w._on_chat_action(
        SimpleNamespace(chat_id=_CID, new_title=None, new_photo=True, photo=None)
    )
    await bus.flush()
    assert received[0].local_path is None


async def test_on_channel_updated_refreshes(wrapper, bus) -> None:
    w, mock = wrapper
    mock.get_entity.return_value = _channel(title="改后")
    received: list[ChannelMetadataChanged] = []
    bus.subscribe(ChannelMetadataChanged, lambda e: _collect(received, e))
    await w._on_channel_updated(types.UpdateChannel(channel_id=123))
    await bus.flush()
    assert received[0].channel_id == _CID
    assert received[0].title == "改后"


# ---- 批量操作 ----


async def test_mark_messages_read(wrapper) -> None:
    w, mock = wrapper
    await w.mark_messages_read(_CID, [3, 1, 2])
    mock.send_read_acknowledge.assert_awaited_with(_CID, max_id=3)


async def test_mark_messages_read_empty_noop(wrapper) -> None:
    w, mock = wrapper
    await w.mark_messages_read(_CID, [])
    mock.send_read_acknowledge.assert_not_awaited()


async def test_forward_messages_sorted(wrapper) -> None:
    w, mock = wrapper
    await w.forward_messages(_CID, -100999, [3, 1, 2])
    mock.forward_messages.assert_awaited_with(-100999, [1, 2, 3], from_peer=_CID)


async def test_pin_messages_per_id(wrapper) -> None:
    w, mock = wrapper
    await w.pin_messages(_CID, [1, 2], only_for_self=True)
    assert mock.pin_message.await_count == 2
    mock.pin_message.assert_any_await(_CID, 1, notify=False, pm_oneside=True)


async def test_unpin_messages_per_id(wrapper) -> None:
    w, mock = wrapper
    await w.unpin_messages(_CID, [1, 2])
    assert mock.unpin_message.await_count == 2


async def test_add_reaction_emoji(wrapper) -> None:
    w, mock = wrapper
    await w.add_reaction(_CID, 10, "🔥", is_big=True)
    req = mock.call_args[0][0]
    assert isinstance(req, functions.messages.SendReactionRequest)
    assert req.msg_id == 10
    assert req.big is True
    assert isinstance(req.reaction[0], types.ReactionEmoji)
    assert req.reaction[0].emoticon == "🔥"


async def test_add_reaction_custom_emoji(wrapper) -> None:
    w, mock = wrapper
    await w.add_reaction(_CID, 10, "custom_emoji_id:777")
    req = mock.call_args[0][0]
    assert isinstance(req.reaction[0], types.ReactionCustomEmoji)
    assert req.reaction[0].document_id == 777


async def test_add_reaction_empty_noop(wrapper) -> None:
    w, mock = wrapper
    await w.add_reaction(_CID, 10, "")
    mock.get_input_entity.assert_not_awaited()


async def test_remove_reaction_filters_chosen(wrapper) -> None:
    w, mock = wrapper
    reactions = types.MessageReactions(
        results=[
            types.ReactionCount(
                reaction=types.ReactionEmoji(emoticon="🔥"), count=2, chosen_order=0
            ),
            types.ReactionCount(
                reaction=types.ReactionEmoji(emoticon="👍"), count=1, chosen_order=1
            ),
        ]
    )
    mock.get_messages.return_value = SimpleNamespace(reactions=reactions)
    await w.remove_reaction(_CID, 10, "🔥")
    req = mock.call_args[0][0]
    # 只剩 👍(🔥 被过滤)
    assert len(req.reaction) == 1
    assert req.reaction[0].emoticon == "👍"


# ---- watchdog ----


async def test_watchdog_publishes_connection_state(wrapper, bus, monkeypatch) -> None:
    w, mock = wrapper
    w._WATCHDOG_INTERVAL = 0.01
    w._state = "ready"
    received: list[ConnectionStateChanged] = []
    bus.subscribe(ConnectionStateChanged, lambda e: _collect(received, e))
    mock.is_connected.return_value = True
    w._start_watchdog()
    import asyncio

    await asyncio.sleep(0.05)
    assert any(e.state == "ready" for e in received)
    # 断线 → waiting_for_network + 主动 reconnect
    mock.is_connected.return_value = False
    mock.connect.reset_mock()
    await asyncio.sleep(0.05)
    assert any(e.state == "waiting_for_network" for e in received)
    mock.connect.assert_awaited()
    await w._cancel_watchdog()


# ---- UpdateStream 契约 ----


async def test_stream_aclose_idempotent(wrapper) -> None:
    w, _ = wrapper
    s = w.subscribe_updates()
    await s.aclose()
    await s.aclose()  # 幂等
    assert w._streams == []
    with pytest.raises(StopAsyncIteration):
        await s.__anext__()
