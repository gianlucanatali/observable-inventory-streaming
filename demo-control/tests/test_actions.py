import time

from conftest import auth
from demo_control.actions import Actions


def test_actions_are_authenticated_and_rendered(client):
    assert client.get("/control/api/actions").status_code == 401
    assert client.post("/control/api/actions/sell-out", json={}).status_code == 401

    page = client.get("/control/", headers=auth())
    assert page.status_code == 200
    assert "Actions" in page.get_data(as_text=True)
    assert "Sell out product" in page.get_data(as_text=True)
    assert "Reset demo data" in page.get_data(as_text=True)


def test_sell_out_defaults_to_p0042_and_rejects_bad_product(client):
    started = client.post("/control/api/actions/sell-out", json={}, headers=auth())
    assert started.status_code == 202
    body = started.get_json()
    assert body["product_id"] == "P0042"
    assert body["status"] in {"queued", "running", "succeeded"}

    bad = client.post("/control/api/actions/sell-out", json={"product_id": "nope"}, headers=auth())
    assert bad.status_code == 400
    assert "Pdddd" in bad.get_json()["error"]


def test_sell_out_accepts_a_custom_valid_product_id(client, parts):
    started = client.post("/control/api/actions/sell-out", json={"product_id": "P0007"}, headers=auth())
    assert started.status_code == 202
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        status = client.get("/control/api/actions", headers=auth()).get_json()
        if status["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.01)
    assert status["status"] == "succeeded"
    assert parts["actions"].calls == [("sell-out", "P0007")]


def test_action_status_reports_failure_and_duplicate_submit_is_idempotent(client, parts):
    parts["actions"].fail_sell = RuntimeError("source S03 unavailable")
    first = client.post("/control/api/actions/sell-out", json={"product_id": "P0042"}, headers=auth())
    second = client.post("/control/api/actions/sell-out", json={"product_id": "P0042"}, headers=auth())
    assert first.status_code == 202 and second.status_code == 202
    assert second.get_json()["id"] == first.get_json()["id"]

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        status = client.get("/control/api/actions", headers=auth()).get_json()
        if status["status"] == "failed":
            break
        time.sleep(0.01)
    assert status["status"] == "failed"
    assert "source S03 unavailable" in status["error"]


def test_reset_action_does_not_expose_shell_or_cloud_paths(client, parts):
    result = client.post("/control/api/actions/reset", json={}, headers=auth())
    assert result.status_code == 202
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        status = client.get("/control/api/actions", headers=auth()).get_json()
        if status["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.01)
    assert status["status"] == "succeeded"
    assert parts["actions"].calls == [("reset", None)]
    assert not hasattr(parts["actions"], "subprocess")


def test_actions_emit_truthful_datadog_events_and_ignore_event_send_failure():
    class Events:
        def __init__(self, fail=False):
            self.events, self.fail = [], fail

        def event(self, title, message, alert_type=None, tags=None):
            self.events.append((title, message, alert_type, tags))
            if self.fail:
                raise RuntimeError("DogStatsd unavailable")

    def wait_for(actions):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            status = actions.status()
            if status["status"] in {"succeeded", "failed"}:
                return status
            time.sleep(0.01)
        return actions.status()

    events = Events()
    succeeded = Actions(lambda product, progress: progress(f"sold {product}"), lambda progress: None,
                        event_sink=events, stack="dev")
    assert wait_for(succeeded.start("sell-out", "P0007") and succeeded)["status"] == "succeeded"
    assert [(title, alert_type) for title, _, alert_type, _ in events.events] == [
        ("demo action: sell-out started", "info"), ("demo action: sell-out succeeded", "success")]
    assert all({"demo_event:action", "action:sell-out", "product:P0007", "stack:dev"} <= set(tags) for _, _, _, tags in events.events)

    telemetry_failure = Actions(lambda product, progress: progress("source completed"), lambda progress: None,
                                event_sink=Events(fail=True), stack="dev")
    assert wait_for(telemetry_failure.start("sell-out", "P0009") and telemetry_failure)["status"] == "succeeded"

    failing_events = Events(fail=True)
    source_failure = Actions(lambda product, progress: (_ for _ in ()).throw(RuntimeError("source unavailable")),
                             lambda progress: None, event_sink=failing_events, stack="dev")
    assert wait_for(source_failure.start("sell-out", "P0008") and source_failure)["status"] == "failed"
    assert "source unavailable" in source_failure.status()["error"]
    assert [title for title, _, _, _ in failing_events.events] == [
        "demo action: sell-out started", "demo action: sell-out failed"]
