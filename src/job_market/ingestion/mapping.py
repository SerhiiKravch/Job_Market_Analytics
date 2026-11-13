"""Translate Telegram's raw text and timestamps without formatting or guessing."""

from datetime import datetime

from telethon import types

from job_market.domain.contracts import MessageObservation
from job_market.domain.enums import ContentKind


def map_message(
    message: types.Message | types.MessageService,
    *,
    chat_id: int,
    username: str | None,
    observed_at: datetime,
) -> MessageObservation:
    text = getattr(message, "message", None) or ""
    media = getattr(message, "media", None)
    has_media = media is not None and not isinstance(
        media, (types.MessageMediaEmpty, types.MessageMediaWebPage)
    )
    if isinstance(message, types.MessageService) or (not text and has_media):
        kind = ContentKind.UNSUPPORTED
    elif not text.strip():
        kind = ContentKind.EMPTY
    else:
        kind = ContentKind.CAPTION if has_media else ContentKind.TEXT
    return MessageObservation(
        telegram_chat_id=chat_id,
        telegram_message_id=message.id,
        published_at=message.date,
        observed_at=observed_at,
        telegram_edited_at=getattr(message, "edit_date", None),
        full_text=text,
        content_kind=kind,
        message_url=f"https://t.me/{username}/{message.id}" if username else None,
    )
