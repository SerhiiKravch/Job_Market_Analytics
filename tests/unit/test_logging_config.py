"""Only approved events and metadata may appear in application logs."""

import io
import json
import logging

import pytest

from job_market.logging_config import SafeEventFormatter, configure_logging


@pytest.mark.parametrize(
    "private_text",
    [
        "TELEGRAM_API_HASH=synthetic-secret",
        "phone=+12345678900 login_code=12345 password=synthetic-password",
        "@private_contact recruiting@private.invalid",
        "Повний текст приватної вакансії",
        "session_contents=synthetic-session",
    ],
)
def test_unstructured_messages_are_replaced_entirely(private_text):
    record = logging.LogRecord("telethon", logging.WARNING, __file__, 1, private_text, (), None)
    payload = json.loads(SafeEventFormatter().format(record))
    assert payload["event"] == "unstructured_log_redacted"
    assert private_text not in json.dumps(payload)


def test_only_numeric_metadata_and_known_error_categories_are_allowed():
    record = logging.LogRecord(
        "job_market", logging.INFO, __file__, 1, "operation_failed", (), None
    )
    record.message_id = 123
    record.source_id = -100123456
    record.count = 4
    record.duration_ms = 3.5
    record.error_category = "database"
    record.full_text = "private text"
    record.api_hash = "synthetic-secret"
    payload = json.loads(SafeEventFormatter().format(record))
    assert payload["message_id"] == 123
    assert payload["source_id"] == -100123456
    assert payload["count"] == 4
    assert payload["duration_ms"] == 3.5
    assert payload["error_category"] == "database"
    assert "full_text" not in payload
    assert "api_hash" not in payload


@pytest.mark.parametrize(
    "value", ["private text", True, None, float("nan"), float("inf"), 10**1000]
)
def test_invalid_metadata_is_dropped(value):
    record = logging.LogRecord(
        "job_market", logging.INFO, __file__, 1, "operation_failed", (), None
    )
    record.count = value
    record.error_category = "synthetic-secret"
    payload = json.loads(SafeEventFormatter().format(record))
    assert "count" not in payload
    assert "error_category" not in payload


def test_configured_handler_drops_arguments_tracebacks_and_stack_info():
    stream = io.StringIO()
    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers[:], root.level
    try:
        configure_logging("DEBUG", stream=stream)
        try:
            raise ValueError("synthetic-private-exception")
        except ValueError:
            logging.getLogger("third_party").exception(
                "%s", "synthetic-private-argument", stack_info=True
            )
        logging.getLogger("job_market").info("configuration_validated")
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        assert [row["event"] for row in rows] == [
            "unstructured_log_redacted",
            "configuration_validated",
        ]
        assert "synthetic-private" not in stream.getvalue()
        assert "Traceback" not in stream.getvalue()
    finally:
        root.handlers[:] = previous_handlers
        root.setLevel(previous_level)
