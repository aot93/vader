"""Source Manager — SMB share provisioning (simulated backend by default, see
conftest's SMB_BACKEND-less env, which falls back to "simulator" the same way
HARDWARE_BACKEND does)."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from app.main import app
from app.models import Source, SourceHealth
from app.mounts import get_mount_backend
from app.mounts.base import sanitize_hostname, sanitize_share, unit_name_for
from app.services import source_manager as sm


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# --- sanitisation -----------------------------------------------------------


def test_sanitize_hostname_accepts_names_and_ips():
    assert sanitize_hostname(" Workstation-07 ") == "Workstation-07"
    assert sanitize_hostname("192.168.10.18") == "192.168.10.18"


@pytest.mark.parametrize("bad", ["", "  ", "host name", "host/../etc", "a\nb", "[Service]"])
def test_sanitize_hostname_rejects_injection_attempts(bad):
    with pytest.raises(ValueError):
        sanitize_hostname(bad)


def test_sanitize_share_rejects_newlines_and_brackets():
    assert sanitize_share("Projects") == "Projects"
    with pytest.raises(ValueError):
        sanitize_share("Projects\n[Unit]\nExecStart=rm -rf /")


def test_unit_name_for_is_a_safe_slug():
    assert unit_name_for("Workstation-07.local") == "mnt-vader-workstation-07-local"


# --- service layer -----------------------------------------------------------


def test_create_source_provisions_and_records_audit(db):
    source = sm.create_source(
        db, hostname="WS07", share="Projects", username="alice", password="hunter2",
    )
    db.commit()

    assert source.mount_path.endswith("WS07")
    assert source.credentials_path.endswith("WS07.creds")
    assert source.last_health == SourceHealth.unknown

    # password never touches the row or the audit trail
    assert "password" not in vars(source)
    assert "hunter2" not in repr(vars(source))

    from app.models import AuditLog

    entry = db.scalar(select(AuditLog).where(AuditLog.action == "source.add"))
    assert entry is not None
    assert "hunter2" not in str(entry.detail)


def test_create_source_writes_credentials_file_mode_600(db):
    source = sm.create_source(
        db, hostname="WS08", share="Projects", username="bob", password="s3cret",
    )
    db.commit()
    from pathlib import Path

    cred = Path(source.credentials_path)
    assert cred.is_file()
    assert oct(cred.stat().st_mode)[-3:] == "600"
    text = cred.read_text()
    assert "username=bob" in text
    assert "password=s3cret" in text


def test_create_source_rejects_duplicate_hostname(db):
    sm.create_source(db, hostname="WS09", share="A", username="a", password="p")
    db.commit()
    with pytest.raises(ValueError, match="already exists"):
        sm.create_source(db, hostname="WS09", share="B", username="a", password="p")


def test_create_source_rejects_bad_input(db):
    with pytest.raises(ValueError):
        sm.create_source(db, hostname="bad host", share="A", username="a", password="p")
    with pytest.raises(ValueError):
        sm.create_source(db, hostname="WS10", share="A", username="a", password="")
    with pytest.raises(ValueError):
        sm.create_source(db, hostname="WS11", share="A", username="a", password="p",
                          smb_version="9.9")


def test_check_health_transitions_and_recheck(db):
    source = sm.create_source(db, hostname="WS12", share="A", username="a", password="p")
    db.commit()

    sm.check_health(db, source)
    db.commit()
    assert source.last_health == SourceHealth.healthy
    assert source.last_error is None

    # flip the simulated mount to "down" and recheck
    backend = get_mount_backend()
    from app.mounts.simulator import _sim_mount_dir

    spec = sm._spec_for(source)  # noqa: SLF001 - test reaching into the module under test
    (_sim_mount_dir(spec) / ".simulate-down").write_text("down")

    sm.check_health(db, source)
    db.commit()
    assert source.last_health == SourceHealth.unhealthy
    assert source.last_error

    from app.models import AuditLog

    healthy_to_unhealthy = db.scalars(
        select(AuditLog).where(AuditLog.action == "source.health")
    ).all()
    assert len(healthy_to_unhealthy) == 1
    assert backend.backend == "simulator"


def test_list_healthy_sources_only_returns_healthy(db):
    a = sm.create_source(db, hostname="WS13", share="A", username="a", password="p")
    b = sm.create_source(db, hostname="WS14", share="B", username="a", password="p")
    db.commit()
    sm.check_health(db, a)
    db.commit()
    healthy = sm.list_healthy_sources(db)
    assert [s.hostname for s in healthy] == ["WS13"]
    assert b.hostname not in [s.hostname for s in healthy]


def test_delete_source_deprovisions_and_removes_row(db):
    source = sm.create_source(db, hostname="WS15", share="A", username="a", password="p")
    db.commit()
    from pathlib import Path

    cred_path = Path(source.credentials_path)
    assert cred_path.is_file()

    sm.delete_source(db, source.id)
    db.commit()

    assert not cred_path.exists()
    assert db.get(Source, source.id) is None


# --- router -------------------------------------------------------------


def test_sources_list_and_new_form_render(client):
    assert client.get("/sources").status_code == 200
    assert client.get("/sources/new").status_code == 200


def test_create_source_via_form_then_detail(client, db):
    r = client.post("/sources", data={
        "hostname": "WS20", "share": "Projects", "username": "carol",
        "password": "s3cret", "smb_version": "3.0",
    }, follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    detail = client.get(location)
    assert detail.status_code == 200
    assert "WS20" in detail.text
    assert "s3cret" not in detail.text


def test_create_source_via_form_rejects_bad_hostname(client):
    r = client.post("/sources", data={
        "hostname": "bad host", "share": "A", "username": "a", "password": "p",
    })
    assert r.status_code == 400
    assert "invalid hostname" in r.text


def test_unknown_source_detail_is_404(client):
    assert client.get("/sources/999999").status_code == 404


def test_recheck_and_delete_round_trip(client):
    r = client.post("/sources", data={
        "hostname": "WS21", "share": "A", "username": "a", "password": "p",
    }, follow_redirects=False)
    source_url = r.headers["location"]
    source_id = source_url.rsplit("/", 1)[-1]

    recheck = client.post(f"/sources/{source_id}/recheck", follow_redirects=False)
    assert recheck.status_code == 303
    assert "healthy" in client.get(source_url).text

    delete = client.post(f"/sources/{source_id}/delete", follow_redirects=False)
    assert delete.status_code == 303
    assert client.get(source_url).status_code == 404


def test_healthy_source_appears_as_write_job_datalist_option(client):
    r = client.post("/sources", data={
        "hostname": "WS22", "share": "Projects", "username": "a", "password": "p",
    }, follow_redirects=False)
    source_url = r.headers["location"]
    source_id = source_url.rsplit("/", 1)[-1]
    client.post(f"/sources/{source_id}/recheck")

    form = client.get("/jobs/new/write")
    assert form.status_code == 200
    assert "WS22" in form.text
    assert 'list="known-sources"' in form.text
