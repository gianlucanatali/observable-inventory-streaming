from smoke import checks
import pytest

from smoke.cli import build_parser


def conn(state="RUNNING", tasks=("RUNNING",)):
    return {"status": {"connector": {"state": state}, "tasks": [{"id": i, "state": s} for i, s in enumerate(tasks)]}}


def all_conns(**over):
    d = {n: conn() for n in checks.REQUIRED_CONNECTORS}
    d.update(over)
    return d


def test_connectors_ok():
    assert checks.evaluate_connectors(all_conns()).ok


def test_connector_missing_failed_task_and_no_tasks():
    d = all_conns()
    del d["sellable-redis"]
    d["inventory-s02"] = conn(tasks=("RUNNING", "FAILED"))
    d["inventory-s03"] = conn(tasks=())
    r = checks.evaluate_connectors(d)
    assert not r.ok
    assert "sellable-redis missing" in r.detail and "FAILED" in r.detail and "no tasks" in r.detail


def test_connectors_empty():
    assert not checks.evaluate_connectors({}).ok


def avail(**over):
    d = {"product_id": "P0042", "status": "available", "sellable": 3, "feed": "ok", "release": "1.0.0",
         "stores": [{"store_id": f"S0{i}", "feed": "ok"} for i in range(1, 6)]}
    d.update(over)
    return d


def test_availability_ok_and_out_of_stock_ok():
    assert checks.evaluate_availability("P0042", 200, avail()).ok
    assert checks.evaluate_availability("P0042", 200, avail(status="out_of_stock", sellable=0)).ok


def test_availability_failures():
    assert not checks.evaluate_availability("P0042", 502, None).ok
    assert not checks.evaluate_availability("P0042", 200, avail(status="unknown", unknown_reason="not_ready")).ok
    assert not checks.evaluate_availability("P0042", 200, avail(feed="stale")).ok
    s = avail()["stores"]
    s[2] = {"store_id": "S03", "feed": "stale"}
    r = checks.evaluate_availability("P0042", 200, avail(stores=s))
    assert not r.ok and "S03" in r.detail
    assert not checks.evaluate_availability("P0042", 200, avail(stores=[])).ok
    assert not checks.evaluate_availability("P0042", 200, avail(product_id="P0001")).ok


def test_product_page():
    foot = "Serving release 1.0.0"
    assert checks.evaluate_product_page("3 available online\nBy store: Milano 2", foot).ok
    assert checks.evaluate_product_page("Out of stock online", foot).ok
    assert not checks.evaluate_product_page("Availability can't be confirmed right now", foot).ok
    assert not checks.evaluate_product_page("Checking availability…", foot).ok
    assert not checks.evaluate_product_page("At least 3 available online By store: Roma 2 (last seen, not live)", foot).ok
    assert not checks.evaluate_product_page("3 available online", "Serving release –").ok
    assert not checks.evaluate_product_page("3 available online", "").ok


def test_noise_and_catalogue():
    assert checks.evaluate_browser_noise("home", [], []).ok
    assert not checks.evaluate_browser_noise("home", ["boom"], []).ok
    assert not checks.evaluate_browser_noise("home", [], ["GET /x HTTP 500"]).ok
    assert checks.evaluate_catalogue(24).ok and not checks.evaluate_catalogue(0).ok


def test_control():
    params = {"stack": "dev", "params": [{"key": "a", "status": "ok"}]}
    assert checks.evaluate_control(200, "<h1>Demo control", 200, params).ok
    assert not checks.evaluate_control(401, "", 401, None).ok
    assert not checks.evaluate_control(200, "<html>other", 200, params).ok
    assert not checks.evaluate_control(200, "Demo control", 200, {"params": []}).ok
    bad = {"params": [{"key": "a", "status": "error", "detail": "redis down"}]}
    assert "redis down" in checks.evaluate_control(200, "Demo control", 200, bad).detail


def test_cart_and_line_format():
    assert checks.evaluate_cart("ADD", 201, {"cart_id": "c1"}).ok
    assert not checks.evaluate_cart("ADD", 400, {"error": "x"}).ok
    assert checks.Result("n", True, "d").line().startswith("PASS")
    assert checks.Result("n", False, "d").line().startswith("FAIL")


def test_parser_rejects_removed_record_command():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["record"])
