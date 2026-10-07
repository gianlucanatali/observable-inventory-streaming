"""Links card: reads Redis demo:links (written by make links-publish), never fails the page."""
import json

from conftest import auth

LINKS = [
    {"id": "shop", "group": "Shop", "name": "Online shop", "desc": "Product page P0042", "url": "http://alb.example/#/product/P0042"},
    {"id": "stock-dashboard", "group": "Datadog", "name": "Stock dashboard", "url": "https://app.datadoghq.eu/dashboard/abc"},
    {"id": "no-such-thumb", "group": "Other", "name": "Extra", "url": "https://example.org/x"},
]


def test_page_shows_the_published_links_grouped_with_thumbnails(client, parts):
    parts["redis"].set("demo:links", json.dumps(LINKS))
    html = client.get("/control/", headers=auth()).get_data(as_text=True)
    assert html.index('id="links-card"') < html.index('id="actions"')
    assert 'href="http://alb.example/#/product/P0042" target="_blank" rel="noopener noreferrer"' in html
    assert '<img src="/control/thumbs/shop.jpg"' in html and "no-thumb" in html
    assert html.index(">Shop<") < html.index(">Datadog<") < html.index(">Other<")  # group tags, in group order
    api = client.get("/control/api/links", headers=auth()).get_json()
    assert [l["id"] for l in api["links"]] == ["shop", "stock-dashboard", "no-such-thumb"]
    assert client.get("/control/thumbs/shop.jpg", headers=auth()).status_code == 200


def test_missing_or_bad_links_show_why_and_the_page_still_loads(client, parts):
    r = client.get("/control/", headers=auth())
    assert r.status_code == 200 and "run make links-publish" in r.get_data(as_text=True)
    assert client.get("/control/api/links", headers=auth()).status_code == 503
    parts["redis"].set("demo:links", json.dumps([{"id": "x", "group": "Shop", "name": "X", "url": "javascript:alert(1)"}]))
    html = client.get("/control/", headers=auth()).get_data(as_text=True)
    assert "url must be http or https" in html and 'href="javascript' not in html


def test_link_text_is_escaped_and_thumbs_need_auth(client, parts):
    parts["redis"].set("demo:links", json.dumps([{"id": "shop", "group": "Shop", "name": "<b>x</b>", "url": "https://a.example/"}]))
    assert "&lt;b&gt;x&lt;/b&gt;" in client.get("/control/", headers=auth()).get_data(as_text=True)
    assert client.get("/control/thumbs/shop.jpg").status_code == 401
    assert client.get("/control/thumbs/..%2Fapp.py", headers=auth()).status_code == 404


STACK = {"version": 1, "stack": "hybrid", "env": "dd-demo-hybrid", "alb": "http://alb.example", "vm_public_ip": "203.0.113.7",
         "confluent_env": "env-abc", "kafka_cluster": "lkc-xyz"}


def test_guide_json_joins_stack_and_links_and_the_button_carries_it(client, parts):
    parts["redis"].set("demo:links", json.dumps(LINKS))
    parts["redis"].set("demo:stack", json.dumps({**STACK, "alb": "http://alb.example/"}))  # trailing slash is dropped
    got = client.get("/control/api/guide-json", headers=auth()).get_json()
    assert got == {**STACK, "links": {"control": "http://alb.example/control/", "shop": "http://alb.example/#/product/P0042",
                                      "stock-dashboard": "https://app.datadoghq.eu/dashboard/abc", "no-such-thumb": "https://example.org/x"}}
    html = client.get("/control/", headers=auth()).get_data(as_text=True)
    assert 'id="guide-copy">Copy for the workshop guide</button>' in html and "0.2.4" in html
    assert "&#34;kafka_cluster&#34;: &#34;lkc-xyz&#34;" in html and 'id="guide-copy" disabled' not in html
    assert "execCommand" in html and "navigator.clipboard" in html


def test_guide_json_needs_both_keys_and_the_page_still_loads(client, parts):
    r = client.get("/control/api/guide-json", headers=auth())
    assert r.status_code == 503 and "demo:stack" in r.get_json()["error"]
    html = client.get("/control/", headers=auth()).get_data(as_text=True)
    assert 'id="guide-copy" disabled' in html and "Copy unavailable: No demo:stack published" in html
    parts["redis"].set("demo:stack", json.dumps(STACK))  # stack alone: links missing
    r = client.get("/control/api/guide-json", headers=auth())
    assert r.status_code == 503 and "No links published" in r.get_json()["error"]
    assert client.get("/control/api/guide-json").status_code == 401


def test_malformed_stack_is_a_clear_503(client, parts):
    parts["redis"].set("demo:links", json.dumps(LINKS))
    for bad, why in [("not json", "not JSON"), ("[]", "JSON object"), (json.dumps({**STACK, "alb": ""}), "['alb']"),
                     (json.dumps({**STACK, "version": 2}), "version must be 1"),
                     (json.dumps({**STACK, "alb": "javascript:x"}), "http(s)")]:
        parts["redis"].set("demo:stack", bad)
        r = client.get("/control/api/guide-json", headers=auth())
        assert r.status_code == 503 and why in r.get_json()["error"], (bad, r.get_json())
        assert client.get("/control/", headers=auth()).status_code == 200


def test_guide_json_is_escaped_in_the_page(client, parts):
    parts["redis"].set("demo:links", json.dumps(LINKS))
    parts["redis"].set("demo:stack", json.dumps({**STACK, "stack": "</textarea><script>alert(1)</script>"}))
    html = client.get("/control/", headers=auth()).get_data(as_text=True)
    assert "</textarea><script>alert(1)" not in html and "&lt;/textarea&gt;" in html


def test_first_published_link_is_the_first_tile(client, parts):
    first = {"id": "overview-dashboard", "group": "Datadog", "name": "Overview dashboard", "url": "https://app.datadoghq.eu/dashboard/ovw"}
    parts["redis"].set("demo:links", json.dumps([first, *LINKS]))
    html = client.get("/control/", headers=auth()).get_data(as_text=True)
    assert html.index('data-link="overview-dashboard"') < html.index('data-link="shop"')
    assert '<img src="/control/thumbs/overview-dashboard.jpg"' in html.split('data-link="overview-dashboard"')[1].split("</a>")[0]


def _jpeg_size(path) -> tuple[int, int]:
    """(width, height) from the JPEG start-of-frame marker, stdlib only."""
    import struct
    data = path.read_bytes()
    pos = 2
    while pos < len(data):
        marker, length = data[pos + 1], struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[pos + 5:pos + 9])
            return width, height
        pos += 2 + length
    raise AssertionError(f"{path}: no JPEG frame header")


def test_every_published_tile_id_has_a_480x270_thumbnail():
    from demo_control.links_card import THUMBS
    for tid in ("overview-dashboard", "stream-lineage", "topic-inventory-cdc", "topic-stock-sellable"):
        assert _jpeg_size(THUMBS / f"{tid}.jpg") == (480, 270), tid
