"""gunicorn entrypoint: `gunicorn app.wsgi:app`. Built once per worker (no --preload) so the
Kafka client and consumer thread are created after the fork."""
import os

from . import build_deps, create_app
from .config import Config
from .logging_json import configure_logging

configure_logging()
_cfg = Config.from_env(os.environ)
app = create_app(_cfg, build_deps(_cfg))
