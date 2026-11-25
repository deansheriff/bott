"""Structured JSON logging.

Rule: never pass customer/agent message bodies to the logger at INFO. Log IDs,
event types, counts and durations. Use `extra={...}` for fields.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_STD_ATTRS = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}
_SECRET_HINTS = ("token", "secret", "password", "api_key", "authorization", "cookie")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STD_ATTRS or key.startswith("_"):
                continue
            if any(h in key.lower() for h in _SECRET_HINTS):
                value = "***"
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # httpx logs full request URLs at INFO; keep it quiet.
    for noisy in ("httpx", "httpcore", "openai", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
