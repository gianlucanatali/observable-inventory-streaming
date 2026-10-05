"""gunicorn settings. Sync workers; bounded defaults, overridable by env."""
import os


def _int(name, default, lo, hi):
    raw = os.environ.get(name, str(default))
    try:
        v = int(raw)
    except ValueError:
        raise RuntimeError(f"gunicorn.conf.py: {name}={raw!r} is not an integer")
    if not lo <= v <= hi:
        raise RuntimeError(f"gunicorn.conf.py: {name}={v} outside allowed range {lo}..{hi}")
    return v


bind = f"0.0.0.0:{_int('PORT', 8080, 1, 65535)}"
workers = _int("WEB_CONCURRENCY", 2, 1, 8)
worker_class = "sync"
threads = _int("GUNICORN_THREADS", 1, 1, 8)
timeout = _int("GUNICORN_TIMEOUT", 30, 5, 120)
accesslog = None  # request logs come from the app's JSON logger and traces
errorlog = "-"


def post_fork(server, worker):
    """Mode startup: prepare the catalogue in each worker before it serves (readyz is 503 until then)."""
    from app.wsgi import app
    app.extensions["prepare"]()
# gunicorn 26 control socket (runtime management CLI): unused here, and its default path is under a HOME that
# the non-root app user does not have, which logs an ERROR at every start.
control_socket_disable = True
