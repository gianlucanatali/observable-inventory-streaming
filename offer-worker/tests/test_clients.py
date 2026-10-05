import json

import httpx
import pytest

from offer_worker import jev as jev_module
from offer_worker.bedrock import BedrockWriter, TextError, TextTimeout
from offer_worker.jev import HttpJevClient, JevError, JevRateLimited, JevTimeout


def jev(handler):
    return HttpJevClient("http://jev/v1/systemone", "key", "jev-latest", 800, httpx.Client(transport=httpx.MockTransport(handler)))


def test_jev_parses_choice_and_sends_bearer():
    seen = {}

    def h(req):
        seen["auth"], seen["body"] = req.headers["authorization"], req.read()
        return httpx.Response(200, json={"answers": {"offer": {"type": "choice", "choice": "notify_me", "confidence": 0.9}}})

    got = jev(h).choose("s", "i", {"notify_me": "d"})
    assert (got.choice, got.confidence) == ("notify_me", 0.9)
    assert seen["auth"] == "Bearer key"
    assert json.loads(seen["body"]) == {
        "state": "s",
        "model": "jev-latest",
        "questions": {
            "offer": {
                "type": "choice",
                "instructions": "i",
                "criteria": {"notify_me": "d"},
            }
        },
    }


@pytest.mark.parametrize("resp,exc", [(httpx.Response(429), JevRateLimited), (httpx.Response(500), JevError),
                                      (httpx.Response(200, json={"nope": 1}), JevError)])
def test_jev_errors(resp, exc):
    with pytest.raises(exc):
        jev(lambda r: resp).choose("s", "i", {"a": "b"})


def test_jev_timeout():
    def h(req):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(JevTimeout):
        jev(h).choose("s", "i", {"a": "b"})


def test_jev_manual_llm_span_annotates_only_request_and_decision_data(monkeypatch):
    class Recorder:
        enabled = True
        annotations = []

        @classmethod
        def annotate(cls, **kwargs):
            cls.annotations.append(kwargs)

    monkeypatch.setattr(jev_module, "LLMObs", Recorder, raising=False)

    got = jev(lambda request: httpx.Response(200, json={"answers": {"offer": {
        "type": "choice", "choice": "notify_me", "confidence": 0.9,
        "probabilities": {"notify_me": 0.9, "alt:P0160": 0.1},
    }}})).choose("exact state", "exact instructions", {"notify_me": "wait", "alt:P0160": "switch"})

    assert got.choice == "notify_me"
    assert Recorder.annotations == [{
        "input_data": [{"role": "user", "content": json.dumps({
            "state": "exact state", "instructions": "exact instructions",
            "criteria": {"notify_me": "wait", "alt:P0160": "switch"},
        }, sort_keys=True)}],
        "output_data": [{"role": "assistant", "content": json.dumps({
            "choice": "notify_me", "confidence": 0.9,
            "probabilities": {"notify_me": 0.9, "alt:P0160": 0.1},
        }, sort_keys=True)}],
    }]


def test_jev_preserves_decision_when_llm_observability_is_disabled(monkeypatch):
    monkeypatch.setattr(jev_module.LLMObs, "enabled", False)

    got = jev(lambda request: httpx.Response(200, json={"answers": {"offer": {
        "type": "choice", "choice": "notify_me", "confidence": 0.9,
    }}})).choose("state", "instructions", {"notify_me": "wait"})

    assert (got.choice, got.confidence) == ("notify_me", 0.9)


class FakeBedrock:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.kw = result, exc, None

    def converse(self, **kw):
        self.kw = kw
        if self.exc:
            raise self.exc
        return self.result


def test_bedrock_converse_shape():
    c = FakeBedrock({"output": {"message": {"content": [{"text": "hi"}]}}})
    assert BedrockWriter("eu-west-1", "m", 1000, c).write("facts") == "hi" and c.kw["modelId"] == "m"


def test_bedrock_errors():
    class ReadTimeoutError(Exception):
        pass

    with pytest.raises(TextTimeout):
        BedrockWriter("r", "m", 1000, FakeBedrock(exc=ReadTimeoutError("x"))).write("f")
    with pytest.raises(TextError):
        BedrockWriter("r", "m", 1000, FakeBedrock({"output": {}})).write("f")
    with pytest.raises(TextError):
        BedrockWriter("r", "m", 1000, FakeBedrock(exc=RuntimeError("denied"))).write("f")
