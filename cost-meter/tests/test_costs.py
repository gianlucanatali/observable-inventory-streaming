import datetime as dt

from cost_meter.adapters import COSTS_URL, ConfluentCosts
from cost_meter.core import BilledLine


class FakeResp:
    def __init__(self, body):
        self.status_code, self._body, self.text = 200, body, ""

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params=None, auth=None, timeout=None):
        self.calls.append((url, params))
        return FakeResp(self.pages[url])


def line(line_type, env, amount, original, day="2026-10-03", qty=1.0):
    res = {"id": "lkc-1", "environment": {"id": env}} if env else None
    return {"line_type": line_type, "resource": res, "amount": amount, "original_amount": original,
            "start_date": day, "quantity": qty}


def test_billed_sums_this_environment_only_and_follows_pages():
    pages = {
        COSTS_URL: {"data": [line("KAFKA_NUM_CKUS", "env-a", 1.0, 1.0), line("KAFKA_NUM_CKUS", "env-b", 9.0, 9.0),
                             line("PROMO_CREDIT", None, -5.0, -5.0)],
                    "metadata": {"next": "https://next"}},
        "https://next": {"data": [line("KAFKA_NUM_CKUS", "env-a", 0.5, 0.5, day="2026-10-04", qty=3.0), line("FLINK_NUM_CFUS", "env-a", 0.2, 0.3)],
                         "metadata": {}},
    }
    s = FakeSession(pages)
    out = ConfluentCosts("k", "s", "env-a", session=s).billed(dt.date(2026, 10, 3), dt.date(2026, 10, 4))
    assert out == {"KAFKA_NUM_CKUS": BilledLine(1.5, 1.5, 3.0), "FLINK_NUM_CFUS": BilledLine(0.2, 0.3, 0.0)}
    assert s.calls[0][1]["start_date"] == "2026-10-03" and s.calls[0][1]["end_date"] == "2026-10-05"
    assert s.calls[1] == ("https://next", None)


def test_org_view_keeps_every_environment_and_the_promo_credit():
    pages = {COSTS_URL: {"data": [line("KAFKA_NUM_CKUS", "env-a", 1.0, 1.0), line("KAFKA_NUM_CKUS", "env-b", 9.0, 9.0),
                                  line("PROMO_CREDIT", None, -5.0, -5.0)], "metadata": {}}}
    env, org = ConfluentCosts("k", "s", "env-a", session=FakeSession(pages)).billed_env_and_org(
        dt.date(2026, 10, 3), dt.date(2026, 10, 4))
    assert env == {"KAFKA_NUM_CKUS": BilledLine(1.0, 1.0, 0.0)}
    assert org == {"KAFKA_NUM_CKUS": BilledLine(10.0, 10.0, 0.0), "PROMO_CREDIT": BilledLine(-5.0, -5.0, 0.0)}


def test_org_billed_gross_ignores_promo_credit_netting():
    pages = {COSTS_URL: {"data": [
        line("KAFKA_NUM_CKUS", "env-a", 7e-16, 4.19),
        line("PROMO_CREDIT", None, -4.19, -4.19),
    ], "metadata": {}}}
    gross = ConfluentCosts("k", "s", "env-a", session=FakeSession(pages)).org_gross_billed(
        dt.date(2026, 10, 3), dt.date(2026, 10, 4))

    assert gross == 4.19
