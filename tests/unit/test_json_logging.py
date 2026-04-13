import json
import logging
import sys


def test_json_formatter_basic_fields():
    """JsonFormatter emits JSON with timestamp, level, logger, message."""
    from app.core.logging import JsonFormatter
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test.logger", level=logging.INFO,
        pathname="", lineno=0, msg="Hello world",
        args=(), exc_info=None,
    )
    output = formatter.format(record)
    data = json.loads(output)  # must be valid JSON
    assert data["level"] == "INFO"
    assert data["message"] == "Hello world"
    assert data["logger"] == "test.logger"
    assert "timestamp" in data
    assert "exc_info" not in data  # no exception present


def test_json_formatter_with_exception():
    """exc_info key is present and contains traceback when exception attached."""
    from app.core.logging import JsonFormatter
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        ei = sys.exc_info()
    record = logging.LogRecord(
        name="test.logger", level=logging.ERROR,
        pathname="", lineno=0, msg="Error occurred",
        args=(), exc_info=ei,
    )
    output = formatter.format(record)
    data = json.loads(output)
    assert "exc_info" in data
    assert "ValueError" in data["exc_info"]
    assert "boom" in data["exc_info"]


def test_json_formatter_message_args_interpolated():
    """printf-style message args are interpolated into the message."""
    from app.core.logging import JsonFormatter
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="x", level=logging.DEBUG,
        pathname="", lineno=0, msg="value is %d",
        args=(42,), exc_info=None,
    )
    data = json.loads(formatter.format(record))
    assert data["message"] == "value is 42"


def test_json_formatter_timestamp_is_iso8601():
    """Timestamp is a valid ISO 8601 string ending in +00:00 or Z."""
    from datetime import datetime

    from app.core.logging import JsonFormatter
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="x", level=logging.INFO,
        pathname="", lineno=0, msg="ts test",
        args=(), exc_info=None,
    )
    data = json.loads(formatter.format(record))
    # Should parse without error
    datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
