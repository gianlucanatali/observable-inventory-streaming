"""Environment configuration. Fails at startup naming the missing/invalid variable."""
import os
import re

MODES = ("none", "per_request", "startup")


_STORE_ID = re.compile(r"^S\d{2}$")


def parse_store_hosts(raw):
    """Parse STORE_HOSTS=S01=store-s01,S02=store-s02,... (order = display order); returns the store ids."""
    ids = []
    for item in raw.split(","):
        sid, sep, host = item.strip().partition("=")
        if not sep or not host.strip():
            raise RuntimeError(f"inventory-api config: STORE_HOSTS entry {item!r} must look like S01=store-s01")
        if not _STORE_ID.match(sid):
            raise RuntimeError(f"inventory-api config: STORE_HOSTS store id {sid!r} must look like S01")
        if sid in ids:
            raise RuntimeError(f"inventory-api config: STORE_HOSTS lists {sid} twice")
        ids.append(sid)
    return ids


class Config:
    def __init__(self, env=None):
        self._env = os.environ if env is None else env
        e = self._env
        self.redis_url = self._required("REDIS_URL")
        self.release = self._required("DD_VERSION")
        self.mode = self._required("CATALOGUE_MODE")
        if self.mode not in MODES:
            raise RuntimeError(f"inventory-api config: CATALOGUE_MODE={self.mode!r} invalid, expected one of {MODES}")
        self.stores = parse_store_hosts(self._required("STORE_HOSTS"))
        self.catalogue_path = e.get("CATALOGUE_PATH", "/app/catalogue/catalogue.json")
        self.feed_max_age_s = 10
        self.dogstatsd_host = e.get("DD_DOGSTATSD_HOST", e.get("DD_AGENT_HOST", "localhost"))
        self.dogstatsd_port = int(e.get("DD_DOGSTATSD_PORT", "8125"))

    def _required(self, name):
        v = self._env.get(name)
        if not v:
            raise RuntimeError(f"inventory-api config: required environment variable {name} is not set")
        return v
