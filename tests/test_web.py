from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_pages_render():
    with TestClient(app) as client:
        for path in ["/", "/library", "/tapes", "/tapes/new", "/search",
                     "/jobs", "/jobs/new/write", "/restores", "/audit", "/health"]:
            resp = client.get(path)
            assert resp.status_code == 200, (path, resp.status_code)


def test_export_catalog_csv_is_csv():
    with TestClient(app) as client:
        resp = client.get("/export/catalog.csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert resp.text.splitlines()[0].startswith("tape_barcode,")


def test_register_tape_via_form():
    with TestClient(app) as client:
        resp = client.post("/tapes/new", data={"barcode": "WEB001L8", "status": "scratch"},
                           follow_redirects=False)
        assert resp.status_code == 303
        detail = client.get("/tapes/WEB001L8")
        assert detail.status_code == 200
        assert "WEB001L8" in detail.text
