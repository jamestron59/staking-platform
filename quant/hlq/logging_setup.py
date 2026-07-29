"""Structured logging. Every line is one JSON object.

Human-readable logs are useless for post-mortem on a trading system: you need
to reconstruct exactly what the bot knew at the moment it decided. So every
decision emits a structured event with its inputs, and those events are the
same records the model layer later trains on.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%03dZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


class EventLogger:
    """Thin wrapper that forces structured fields.

    `log.event("order_submitted", cloid=..., px=..., sz=...)` rather than
    f-strings, so the log is queryable.
    """

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    def event(self, name: str, level: int = logging.INFO, **fields: Any) -> None:
        self._log.log(level, name, extra={"extra_fields": fields})

    def warn(self, name: str, **fields: Any) -> None:
        self.event(name, logging.WARNING, **fields)

    def error(self, name: str, **fields: Any) -> None:
        self.event(name, logging.ERROR, **fields)

    def debug(self, name: str, **fields: Any) -> None:
        self.event(name, logging.DEBUG, **fields)

    def exception(self, name: str, **fields: Any) -> None:
        self._log.exception(name, extra={"extra_fields": fields})


def setup_logging(log_dir: str | os.PathLike[str] | None = None, level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(JsonFormatter())
    root.addHandler(stream)

    if log_dir is not None:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            d / "hlq.jsonl", maxBytes=256 * 1024 * 1024, backupCount=20
        )
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)

    # The SDK logs full signed payloads at DEBUG. Never let that reach disk.
    logging.getLogger("hyperliquid").setLevel(logging.INFO)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str) -> EventLogger:
    return EventLogger(name)
