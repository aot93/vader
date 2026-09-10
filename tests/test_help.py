"""The in-app user manual (/help) renders the checked-in user-manual/*.md files."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.services.manual import list_pages


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_help_home_renders_index(client):
    r = client.get("/help")
    assert r.status_code == 200
    assert "manual-nav" in r.text
    # a nav entry for every page
    for page in list_pages():
        assert f'href="/help/{page.slug}"' in r.text


def test_every_manual_page_renders(client):
    slugs = [p.slug for p in list_pages()]
    assert {"index", "archiving", "restore", "troubleshooting"} <= set(slugs)
    for slug in slugs:
        assert client.get(f"/help/{slug}").status_code == 200, slug


def test_cross_page_md_links_are_rewritten(client):
    body = client.get("/help/index").text
    assert 'href="/help/archiving"' in body
    assert '.md"' not in body  # no stale foo.md links leak through


def test_image_assets_are_served(client):
    body = client.get("/help/dashboard").text
    assert 'src="/help/assets/dashboard.png"' in body
    img = client.get("/help/assets/dashboard.png")
    assert img.status_code == 200
    assert img.headers["content-type"].startswith("image/")


def test_unknown_page_and_traversal_are_404(client):
    assert client.get("/help/does-not-exist").status_code == 404
    assert client.get("/help/assets/../../config.py").status_code == 404
