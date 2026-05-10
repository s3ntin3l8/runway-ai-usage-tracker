import json
import logging
from datetime import datetime


class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        # Render in the process's local zone (honors TZ env var) with explicit
        # offset, so downstream readers don't have to guess.
        ts = datetime.fromtimestamp(record.created).astimezone()
        entry: dict = {
            "timestamp": ts.isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry)
