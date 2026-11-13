"""Telethon boundary. Only the explicit login command may authenticate."""

import asyncio
import logging
import os
import re
import sqlite3
import warnings
from collections.abc import Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from getpass import GetPassWarning, getpass
from pathlib import Path
from stat import S_ISREG

from telethon import TelegramClient, errors, events, types, utils

from job_market.config import TelegramCredentials
from job_market.domain.contracts import MessageObservation
from job_market.ingestion.mapping import map_message
from job_market.storage.workspace import lock_file

_LOG = logging.getLogger(__name__)
_USERNAME = re.compile(r"(?:@|https://t\.me/)?([A-Za-z][A-Za-z0-9_]{2,31})/?\Z")
_CHAT_ID = re.compile(r"-[0-9]{1,19}\Z")
_FLOOD_LIMIT = 300


class TelegramError(ValueError):
    """A Telegram failure that can be printed without exposing account data."""


@dataclass(frozen=True)
class ChannelInfo:
    chat_id: int
    title: str = field(repr=False)
    username: str | None


@dataclass(frozen=True)
class Channel:
    info: ChannelInfo
    peer: types.InputPeerChannel = field(repr=False)


@dataclass(frozen=True)
class Page:
    observations: tuple[MessageObservation, ...] = field(repr=False)

    @property
    def ids(self) -> tuple[int, ...]:
        return tuple(item.telegram_message_id for item in self.observations)


def _now() -> datetime:
    return datetime.now(UTC)


def hidden_prompt(label: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        try:
            return getpass(label)
        except (GetPassWarning, EOFError, OSError):
            raise TelegramError(
                "Interactive login requires a terminal with hidden input."
            ) from None


class TelegramGateway:
    def __init__(
        self,
        client: TelegramClient,
        *,
        clock: Callable[[], datetime] = _now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._client = client
        self._clock = clock
        self._sleep = sleep

    async def _request[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(3):
            try:
                return await operation()
            except errors.FloodWaitError as exc:
                seconds = max(1, exc.seconds)
                if seconds > _FLOOD_LIMIT or attempt == 2:
                    raise TelegramError(
                        f"Telegram rate limited this request. Retry after {seconds} seconds."
                    ) from None
                _LOG.warning("telegram_flood_wait", extra={"duration_ms": seconds * 1000})
                await self._sleep(seconds)
            except (errors.UnauthorizedError, errors.AuthKeyError, errors.AuthKeyNotFound):
                raise TelegramError("Telegram session unavailable. Run 'telegram login'.") from None
            except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError):
                raise TelegramError(
                    "Login code invalid or expired. Run 'telegram login' again."
                ) from None
            except errors.PasswordHashInvalidError:
                raise TelegramError(
                    "Two-step password rejected. Run 'telegram login' again."
                ) from None
            except (errors.RPCError, errors.SecurityError, sqlite3.Error, OSError, ValueError):
                raise TelegramError(
                    "Telegram request failed. Check network, session, and channel access."
                ) from None
        raise AssertionError("Unreachable retry limit.")

    async def login(self, prompt: Callable[[str], str]) -> bool:
        """Return whether a fresh interactive login was performed."""
        if await self._request(self._client.is_user_authorized):
            await self.require_user()
            return False
        phone = prompt("Telegram phone number (including country code): ").strip()
        if not phone:
            raise TelegramError("A phone number is required.")
        sent = await self._request(lambda: self._client.send_code_request(phone))
        code = prompt("Telegram login code: ").strip()
        if not code:
            raise TelegramError("A login code is required.")

        async def sign_in():
            try:
                return await self._client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
            except errors.SessionPasswordNeededError:
                return None

        if await self._request(sign_in) is None:
            password = prompt("Telegram two-step password: ")
            if not password:
                raise TelegramError("A two-step password is required.")
            await self._request(lambda: self._client.sign_in(password=password))
        await self.require_user()
        return True

    async def require_user(self) -> None:
        if not await self._request(self._client.is_user_authorized):
            raise TelegramError("Telegram session unavailable. Run 'telegram login'.")
        account = await self._request(self._client.get_me)
        if account is None or account.bot:
            raise TelegramError("An authorized Telegram user account is required.")

    @staticmethod
    def _channel(entity) -> Channel | None:
        if not isinstance(entity, types.Channel) or not entity.broadcast:
            return None
        try:
            peer = utils.get_input_peer(entity)
        except (TypeError, ValueError):
            return None
        return Channel(ChannelInfo(utils.get_peer_id(entity), entity.title, entity.username), peer)

    async def _dialogs(self) -> list[Channel]:
        async def read():
            channels = []
            async for dialog in self._client.iter_dialogs():
                if channel := self._channel(dialog.entity):
                    channels.append(channel)
            return channels

        return await self._request(read)

    async def sources(self) -> tuple[ChannelInfo, ...]:
        return tuple(channel.info for channel in await self._dialogs())

    async def resolve(self, reference: str) -> Channel:
        if _CHAT_ID.fullmatch(reference):
            wanted = int(reference)
            for channel in await self._dialogs():
                if channel.info.chat_id == wanted:
                    return channel
            raise TelegramError(
                "Configured channel is not available in your dialogs. Run 'telegram sources'."
            )
        match = _USERNAME.fullmatch(reference)
        if not match:
            raise TelegramError(
                "Use a public channel username or a channel ID from 'telegram sources'."
            )
        entity = await self._request(lambda: self._client.get_entity("@" + match[1]))
        if not (channel := self._channel(entity)):
            raise TelegramError("The configured source is not a resolvable broadcast channel.")
        return channel

    async def head(self, channel: Channel) -> int | None:
        messages = await self._request(lambda: self._client.get_messages(channel.peer, limit=1))
        return messages[0].id if messages else None

    async def page(
        self,
        channel: Channel,
        *,
        limit: int,
        after: int = 0,
        before: int = 0,
        ascending: bool = False,
    ) -> Page:
        messages = await self._request(
            lambda: self._client.get_messages(
                channel.peer,
                limit=limit,
                min_id=after,
                max_id=before,
                reverse=ascending,
            )
        )
        if any(utils.get_peer_id(message.peer_id) != channel.info.chat_id for message in messages):
            raise TelegramError("Telegram returned a message outside the configured channel.")
        observed_at = self._clock()
        rows = tuple(
            map_message(
                message,
                chat_id=channel.info.chat_id,
                username=channel.info.username,
                observed_at=observed_at,
            )
            for message in messages
        )
        ids = [item.telegram_message_id for item in rows]
        if (
            len(ids) > limit
            or ids != sorted(set(ids), reverse=not ascending)
            or any(i <= after or (before and i >= before) for i in ids)
        ):
            raise TelegramError("Telegram returned a page outside the requested bounds.")
        return Page(rows)

    @contextmanager
    def listen(
        self,
        channel: Channel,
        on_observation: Callable[[MessageObservation], None],
        on_error: Callable[[], None],
    ) -> Iterator[None]:
        async def callback(event):
            if event.chat_id != channel.info.chat_id:
                return
            try:
                on_observation(
                    map_message(
                        event.message,
                        chat_id=channel.info.chat_id,
                        username=channel.info.username,
                        observed_at=self._clock(),
                    )
                )
            except Exception:
                # Telethon normally logs and swallows callback errors. Tell the
                # coordinator to stop instead of continuing with an unrecorded update.
                on_error()

        self._client.add_event_handler(callback, events.NewMessage(chats=channel.info.chat_id))
        self._client.add_event_handler(callback, events.MessageEdited(chats=channel.info.chat_id))
        try:
            yield
        finally:
            self._client.remove_event_handler(callback)

    async def enable_updates(self) -> None:
        await self._request(lambda: self._client.set_receive_updates(True))

    async def wait_disconnected(self) -> None:
        try:
            await asyncio.shield(self._client.disconnected)
        except (errors.RPCError, errors.AuthKeyNotFound, errors.SecurityError, OSError):
            raise TelegramError("Telegram disconnected; restart collection to reconcile.") from None


def _prepare_session(path: Path, create: bool) -> None:
    if not create and not path.is_file():
        raise TelegramError("Telegram session unavailable. Run 'telegram login'.")
    descriptor = os.open(
        path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0), 0o600
    )
    try:
        state = os.fstat(descriptor)
        if not S_ISREG(state.st_mode) or state.st_nlink != 1:
            raise TelegramError("Use a private, regular Telegram session file.")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


@asynccontextmanager
async def open_telegram(path: Path, credentials: TelegramCredentials, *, login: bool = False):
    # Telethon appends the suffix if missing. Lock and secure that exact path.
    path = Path(str(path) if str(path).endswith(".session") else str(path) + ".session")
    try:
        path = path.resolve()
    except (OSError, RuntimeError):
        raise TelegramError("Could not resolve the Telegram session path.") from None
    if login:
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            raise TelegramError("Could not create the Telegram session directory.") from None
    elif not path.is_file():
        raise TelegramError("Telegram session unavailable. Run 'telegram login'.")
    with lock_file(path.with_name(path.name + ".worker.lock"), "Telegram session"):
        try:
            _prepare_session(path, login)
            client = TelegramClient(
                path,
                credentials.api_id,
                credentials.api_hash.get_secret_value(),
                receive_updates=False,
                flood_sleep_threshold=0,
                request_retries=2,
                connection_retries=3,
                raise_last_call_error=True,
            )
        except (OSError, sqlite3.Error, RuntimeError):
            raise TelegramError(
                "Could not open Telegram session. Check its path and access."
            ) from None
        gateway = TelegramGateway(client)
        try:
            await gateway._request(client.connect)
            yield gateway
        finally:
            try:
                await client.disconnect()
            except (errors.RPCError, sqlite3.Error, OSError, RuntimeError):
                _LOG.warning("operation_failed", extra={"error_category": "telegram"})
