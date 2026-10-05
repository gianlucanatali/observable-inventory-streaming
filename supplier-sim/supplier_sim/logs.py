"""Structured JSON logs with Datadog trace correlation read from the active span."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        out.update(getattr(record, "ctx", {}))
        from ddtrace import tracer  # imported lazily so formatting works before tracer start-up

        span = tracer.current_span()
        if span is not None:
            out["dd.trace_id"] = str(span.trace_id)
            out["dd.span_id"] = str(span.span_id)
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging() -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.INFO)
