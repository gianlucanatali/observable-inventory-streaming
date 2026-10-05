"""JSON logging with Datadog trace correlation."""
import json
import logging
import sys
import time

from ddtrace import tracer


class JsonFormatter(logging.Formatter):
    def format(self, record):
        span = tracer.current_span()
        out = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": "inventory-api",
            "dd.service": "inventory-api",
            "dd.env": _env("DD_ENV"),
            "dd.version": _env("DD_VERSION"),
        }
        if span is not None:
            out["dd.trace_id"] = str(span.trace_id)
            out["dd.span_id"] = str(span.span_id)
        if record.exc_info:
            out["error.stack"] = self.formatException(record.exc_info)
            out["error.kind"] = record.exc_info[0].__name__
        return json.dumps(out)


def _env(name):
    import os
    return os.environ.get(name, "")


def configure():
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.INFO)
