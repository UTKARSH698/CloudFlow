"""
Structured JSON Logging for CloudFlow
======================================
Outputs one JSON line per log record — compatible with CloudWatch Logs Insights.

Why JSON logging?
  Standard text logging (2024-01-01 INFO Order created order_id=abc) is
  human-readable but machine-unfriendly. CloudWatch Logs Insights can query
  JSON fields directly:

    fields @timestamp, order_id, level
    | filter service = "order_service" and level = "ERROR"
    | sort @timestamp desc

  This turns your logs into a searchable structured database.

Usage:
  from shared.logger import get_logger
  logger = get_logger(__name__)
  logger.info("Order created", extra={"order_id": "abc", "total_cents": 999})

Output:
  {"timestamp":"2024-01-01T00:00:00Z","level":"INFO","service":"order_service",
   "message":"Order created","order_id":"abc","total_cents":999}
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

# Standard logging.LogRecord fields we don't want in the output
_STDLIB_FIELDS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName",
}

_configured = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()

        log_obj: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "service": record.name,
            "message": record.message,
        }

        # Merge any extra fields passed via logger.info(..., extra={...})
        for key, value in record.__dict__.items():
            if key not in _STDLIB_FIELDS:
                log_obj[key] = value

        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_obj, default=str)


def get_logger(name: str) -> logging.Logger:
    """
    Return a logger configured to emit structured JSON to stdout.
    Idempotent — safe to call multiple times.
    """
    global _configured
    if not _configured:
        root = logging.getLogger()
        formatter = _JsonFormatter()
        if root.handlers:
            for h in root.handlers:
                h.setFormatter(formatter)
        else:
            handler = logging.StreamHandler()
            handler.setFormatter(formatter)
            root.addHandler(handler)
        log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
        root.setLevel(getattr(logging, log_level, logging.INFO))
        _configured = True
    return logging.getLogger(name)
