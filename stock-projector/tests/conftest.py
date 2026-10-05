import json
from pathlib import Path

import fakeredis
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT_AVRO = Path(__file__).parents[2] / "contracts" / "avro"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis()
