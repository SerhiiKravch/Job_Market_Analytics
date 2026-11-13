"""Telegram is mocked at the client boundary; SQLite uses real migrations."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from telethon import errors, events, types

from job_market.cli import main
from job_market.ingestion import telegram
from job_market.ingestion.mapping import map_message
from job_market.ingestion.service import Collector
from job_market.ingestion.telegram import TelegramError, TelegramGateway
from job_market.services.processing import installed_versions
from job_market.storage.database import Database
from job_market.storage.messages import MessageStore
from job_market.storage.migration import upgrade_database
from job_market.storage.models import ExtractionRun, Message, MessageRevision
from job_market.storage.workspace import lock_file, lock_workspace

NOW = datetime(2026, 11, 13, tzinfo=UTC)
PEER = types.PeerChannel(12345)
CHAT = -1000000012345


@pytest.mark.parametrize(
    ("text", "media", "kind"),
    [
        ("📌 Original\r\ncaption", types.MessageMediaPhoto(), "caption"),
        ("", types.MessageMediaPhoto(), "unsupported"),
        ("", None, "empty"),
        ("Raw **text**", None, "text"),
    ],
)
def test_message_mapping_preserves_original_content(text, media, kind):
    message = types.Message(id=42, peer_id=PEER, date=NOW, edit_date=NOW, message=text, media=media)
    observation = map_message(message, chat_id=CHAT, username="example_channel", observed_at=NOW)
    assert observation.full_text == text
    assert observation.content_kind == kind
    assert observation.published_at == observation.telegram_edited_at == NOW
    assert observation.message_url == "https://t.me/example_channel/42"


def post(number, *, text="Vacancy: QA Engineer", date=NOW, edit=None):
    return types.Message(id=number, peer_id=PEER, date=date, edit_date=edit, message=text)


def channel(*, username=None, broadcast=True):
    return types.Channel(
        id=PEER.channel_id,
        title="Synthetic channel",
        photo=types.ChatPhotoEmpty(),
        date=NOW,
        broadcast=broadcast,
        username=username,
        access_hash=123,
    )


class FakeClient:
    """Model exclusive Telethon message bounds and an update during startup."""

    def __init__(self, posts=(), *, authorized=True):
        self.posts = {item.id: item for item in posts}
        self.authorized = authorized
        self.disconnected = asyncio.get_running_loop().create_future()
        self.handlers = []
        self.calls = []
        self.on_updates = None
        self.before_page = None
        self.dialogs = [channel()]

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return SimpleNamespace(bot=False) if self.authorized else None

    async def iter_dialogs(self):
        for entity in self.dialogs:
            yield SimpleNamespace(entity=entity)

    async def get_entity(self, reference):
        self.calls.append(("entity", reference))
        return channel(username="example_channel")

    async def get_messages(self, peer, *, limit, min_id=0, max_id=0, reverse=False):
        assert isinstance(peer, types.InputPeerChannel) and peer.channel_id == PEER.channel_id
        self.calls.append(("page", limit, min_id, max_id, reverse))
        if self.before_page:
            self.before_page(limit, min_id, max_id, reverse)
        return [
            self.posts[key]
            for key in sorted(self.posts, reverse=not reverse)
            if key > min_id and (not max_id or key < max_id)
        ][:limit]

    def add_event_handler(self, callback, builder):
        self.handlers.append((callback, builder))

    def remove_event_handler(self, callback):
        self.handlers = [(cb, builder) for cb, builder in self.handlers if cb != callback]

    async def set_receive_updates(self, enabled):
        assert enabled and len(self.handlers) == 2
        assert {type(builder) for _, builder in self.handlers} == {
            events.NewMessage,
            events.MessageEdited,
        }
        if self.on_updates:
            await self.on_updates()

    async def emit(self, message, *, edited=False, chat_id=CHAT):
        kind = events.MessageEdited if edited else events.NewMessage
        for callback, builder in self.handlers:
            if type(builder) is kind:
                await callback(SimpleNamespace(chat_id=chat_id, message=message))


@pytest.fixture
def workspace(tmp_path):
    url = f"sqlite:///{tmp_path / 'jobs.db'}"
    upgrade_database(url)
    db = Database(url)
    try:
        yield db, url
    finally:
        db.close()


def setup(client, db, *, now=NOW):
    gateway = TelegramGateway(client, clock=lambda: now)
    store = MessageStore(db, installed_versions())
    source = store.register_source(
        telegram_chat_id=CHAT, name="Synthetic", now=now, backfill_days=2
    )
    collector = Collector(
        gateway,
        db,
        store,
        clock=lambda: now,
        page_size=2,
        page_pause=0,
        sync_seconds=60,
    )
    return gateway, store, source, collector


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def test_bounded_history_and_live_callbacks_do_not_skip_gaps(workspace):
    db, _ = workspace

    async def scenario():
        client = FakeClient([post(1, date=NOW - timedelta(days=3)), post(3), post(5)])
        gateway, store, source, collector = setup(client, db)
        resolved = await gateway.resolve(str(CHAT))
        stop = asyncio.Event()

        async def on_updates():
            await client.emit(post(100), chat_id=CHAT - 1)
            await client.emit(post(12))
            assert store.get_source(source.id).live_checkpoint_message_id is None

        client.on_updates = on_updates
        task = asyncio.create_task(collector.run(resolved, source, stop))
        try:
            await until(lambda: store.get_source(source.id).live_checkpoint_message_id == 5)
            client.posts[5] = post(5, text="Vacancy: Data Analyst", edit=NOW + timedelta(seconds=1))
            await client.emit(client.posts[5], edited=True)
            assert store.get_source(source.id).live_checkpoint_message_id == 5

            def edit_processed():
                with db.transaction() as session:
                    return (
                        session.scalar(
                            select(ExtractionRun.status)
                            .join(
                                MessageRevision,
                                ExtractionRun.message_revision_id == MessageRevision.id,
                            )
                            .where(MessageRevision.full_text == "Vacancy: Data Analyst")
                        )
                        == "completed"
                    )

            await until(edit_processed)
        finally:
            stop.set()
            await task
        assert not client.handlers
        with db.transaction() as session:
            assert set(session.scalars(select(Message.telegram_message_id))) == {3, 5, 12}
            rows = session.scalars(select(MessageRevision).order_by(MessageRevision.id)).all()
            assert len(rows) == 4 and rows[-1].full_text == "Vacancy: Data Analyst"

    asyncio.run(scenario())


def test_restart_preserves_initial_head_and_recovers_gap_and_missed_edit(workspace):
    db, url = workspace

    async def scenario():
        client = FakeClient([post(3), post(5), post(6)])
        gateway, store, source, collector = setup(client, db)

        def interrupt(limit, min_id, max_id, reverse):
            if max_id == 5:
                raise OSError("synthetic interruption")

        client.before_page = interrupt
        with pytest.raises(TelegramError, match="Telegram request failed"):
            await collector.run(await gateway.resolve(str(CHAT)), source, asyncio.Event())
        prior = store.get_source(source.id)
        assert prior.history_upper_message_id == 6 and prior.history_cursor_message_id == 5
        assert prior.live_checkpoint_message_id is None and not client.handlers
        db.close()
        restarted = Database(url)
        try:
            new_client = FakeClient(
                [
                    post(3),
                    post(5, text="Vacancy: Data Analyst", edit=NOW + timedelta(minutes=1)),
                    post(6),
                    post(8),
                    post(10),
                ]
            )
            gateway, store, source, collector = setup(
                new_client, restarted, now=NOW + timedelta(days=1)
            )
            assert source.history_start_at == prior.history_start_at
            stop = asyncio.Event()
            task = asyncio.create_task(
                collector.run(await gateway.resolve(str(CHAT)), source, stop)
            )
            try:
                await until(lambda: store.get_source(source.id).live_checkpoint_message_id == 10)
            finally:
                stop.set()
                await task
            state = store.get_source(source.id)
            assert state.history_upper_message_id == 6 and state.history_completed_at is not None
            assert ("page", 2, 6, 11, True) in new_client.calls
            with restarted.transaction() as session:
                assert set(session.scalars(select(Message.telegram_message_id))) == {3, 5, 6, 8, 10}
                current = session.scalar(
                    select(MessageRevision.full_text)
                    .join(
                        Message,
                        Message.current_revision_id == MessageRevision.id,
                    )
                    .where(Message.telegram_message_id == 5)
                )
                assert current == "Vacancy: Data Analyst"
        finally:
            restarted.close()

    asyncio.run(scenario())


def test_flood_wait_delays_retry_and_bounds_attempts():
    async def scenario():
        client = FakeClient()
        delays = []

        async def sleep(seconds):
            delays.append(seconds)

        def flood(*args):
            raise errors.FloodWaitError(request=None, capture=17)

        client.before_page = flood
        gateway = TelegramGateway(client, sleep=sleep)
        with pytest.raises(TelegramError, match="after 17 seconds"):
            await gateway.head(await gateway.resolve(str(CHAT)))
        assert delays == [17, 17]
        assert len(client.calls) == 3

    asyncio.run(scenario())


def test_private_id_must_resolve_from_dialog_and_sources_exclude_groups():
    async def scenario():
        client = FakeClient()
        client.dialogs.append(channel(broadcast=False))
        gateway = TelegramGateway(client)
        assert [item.chat_id for item in await gateway.sources()] == [CHAT]
        assert (await gateway.resolve(str(CHAT))).peer.access_hash == 123
        with pytest.raises(TelegramError, match="not available in your dialogs"):
            await gateway.resolve(str(CHAT - 1))
        with pytest.raises(TelegramError, match="public channel username"):
            await gateway.resolve("https://t.me/+synthetic")
        assert (
            await gateway.resolve("https://t.me/example_channel")
        ).info.username == "example_channel"

    asyncio.run(scenario())


def test_only_explicit_login_prompts_for_code_and_two_step_password():
    async def scenario():
        class LoginClient(FakeClient):
            async def send_code_request(self, phone):
                assert phone == "synthetic phone"
                return SimpleNamespace(phone_code_hash="synthetic hash")

            async def sign_in(self, phone=None, code=None, *, phone_code_hash=None, password=None):
                if password is None:
                    assert (phone, code, phone_code_hash) == (
                        "synthetic phone",
                        "synthetic code",
                        "synthetic hash",
                    )
                    raise errors.SessionPasswordNeededError(request=None)
                assert password == "synthetic password"
                self.authorized = True

        gateway = TelegramGateway(LoginClient(authorized=False))
        with pytest.raises(TelegramError, match="telegram login"):
            await gateway.require_user()
        inputs = iter(("synthetic phone", "synthetic code", "synthetic password"))
        assert await gateway.login(lambda _: next(inputs))
        assert not await gateway.login(lambda _: pytest.fail("Already signed in"))

    asyncio.run(scenario())


def test_cli_requires_a_session_and_never_constructs_client_without_it(
    monkeypatch, tmp_path, capsys
):
    configure_cli(monkeypatch)

    def unexpected(*args, **kwargs):
        pytest.fail("The missing session must be rejected before constructing a client.")

    monkeypatch.setattr(telegram, "TelegramClient", unexpected)
    assert main(["--project-root", str(tmp_path), "telegram", "sources"]) == 1
    assert "Run 'telegram login'" in capsys.readouterr().err
    assert not (tmp_path / "var").exists()


def configure_cli(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "a" * 32)
    monkeypatch.setenv("TELEGRAM_CHANNEL", str(CHAT))


def test_mocked_cli_expired_session_never_prompts_and_disconnects(
    monkeypatch,
    tmp_path,
    capsys,
):
    configure_cli(monkeypatch)
    path = tmp_path / "var" / "telegram.session"
    path.parent.mkdir()
    path.touch()
    disconnected = []

    class Expired(FakeClient):
        async def connect(self):
            pass

        async def disconnect(self):
            disconnected.append(True)

    monkeypatch.setattr(telegram, "TelegramClient", lambda *a, **kw: Expired(authorized=False))
    monkeypatch.setattr(telegram, "hidden_prompt", lambda _: pytest.fail("Unexpected prompt"))
    assert main(["--project-root", str(tmp_path), "telegram", "sources"]) == 1
    assert "Run 'telegram login'" in capsys.readouterr().err
    assert disconnected == [True]
    assert path.stat().st_mode & 0o777 == 0o600


def test_mocked_cli_collect_releases_locks_after_disconnect(monkeypatch, tmp_path, capsys):
    configure_cli(monkeypatch)
    path = tmp_path / "var" / "telegram.session"
    path.parent.mkdir()
    path.touch()
    url = f"sqlite:///{tmp_path / 'var' / 'jobs.db'}"
    upgrade_database(url)
    clients = []

    class Disconnected(FakeClient):
        async def connect(self):
            pass

        async def set_receive_updates(self, enabled):
            await super().set_receive_updates(enabled)
            self.disconnected.set_result(None)

        async def disconnect(self):
            self.closed = True

    def factory(*args, **kwargs):
        assert kwargs["receive_updates"] is False
        client = Disconnected([post(1)])
        clients.append(client)
        return client

    monkeypatch.setattr(telegram, "TelegramClient", factory)
    assert main(["--project-root", str(tmp_path), "collect"]) == 1
    assert "restart collection to reconcile" in capsys.readouterr().err
    assert len(clients) == 1 and clients[0].closed and not clients[0].handlers
    with lock_workspace(url), lock_file(path.with_name(path.name + ".worker.lock"), "session"):
        pass


def test_live_write_error_is_not_swallowed_by_event_dispatch(workspace):
    db, _ = workspace

    async def scenario():
        client = FakeClient()
        gateway, _, source, collector = setup(client, db)
        with db.engine.begin() as connection:
            connection.exec_driver_sql("""
                CREATE TRIGGER fail_live BEFORE INSERT ON messages
                BEGIN SELECT RAISE(ABORT, 'synthetic private failure'); END
            """)
        stop = asyncio.Event()

        async def inject():
            await client.emit(post(4))

        client.on_updates = inject
        with pytest.raises(TelegramError, match="live update could not be stored"):
            await collector.run(await gateway.resolve(str(CHAT)), source, stop)
        assert stop.is_set() and not client.handlers

    asyncio.run(scenario())
