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
