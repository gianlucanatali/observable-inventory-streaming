"""gunicorn entrypoint: `app.wsgi:app`. Startup preparation runs in gunicorn.conf.py post_fork."""
import logging

from app import logs
from app.main import create_app

logs.configure()
app = create_app()
logging.getLogger("inventory-api").info(
    "inventory-api started release=%s mode=%s", app.config["RELEASE"], app.extensions["catalogue"].mode)
