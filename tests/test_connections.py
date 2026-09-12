"""Connection Manager — SMB share provisioning (simulated backend by default,
see conftest's SMB_BACKEND-less env, which falls back to "simulator" the same
way HARDWARE_BACKEND does)."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from app.main import app
from app.models import Connection, ConnectionHealth, ConnectionPurpose
from app.mounts import get_mount_backend
from app.mounts.base import mount_dir_name, sanitize_hostname, sanitize_share, unit_name_for
from app.services import connection_manager as cm


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


def test_unit_name_and_mount_dir_differ_by_purpose_not_by_ingest_default():
    # ingest keeps the original, suffix-less form — an already-provisioned
    # real ingest mount must never be renamed out from under itself.
    assert unit_name_for("WS07", "ingest") == "mnt-vader-ws07"
    assert mount_dir_name("WS07", "ingest") == "WS07"
    # restore_destination is always a distinct mount for the same hostname.
    assert unit_name_for("WS07", "restore_destination") == "mnt-vader-ws07-restore"
    assert mount_dir_name("WS07", "restore_destination") == "WS07-restore"


# --- service layer -----------------------------------------------------------


def test_create_connection_provisions_and_records_audit(db):
    connection = cm.create_connection(
        db, hostname="WS07", share="Projects", username="alice", password="hunter2",
    )
    db.commit()

    assert connection.mount_path.endswith("WS07")
    assert connection.credentials_path.endswith("WS07.creds")
    assert connection.last_health == ConnectionHealth.unknown
    assert connection.purpose == ConnectionPurpose.ingest

    # password never touches the row or the audit trail
    assert "password" not in vars(connection)
    assert "hunter2" not in repr(vars(connection))

    from app.models import AuditLog

    entry = db.scalar(select(AuditLog).where(AuditLog.action == "connection.add"))
    assert entry is not None
    assert "hunter2" not in str(entry.detail)


def test_create_connection_writes_credentials_file_mode_600(db):
    connection = cm.create_connection(
        db, hostname="WS08", share="Projects", username="bob", password="s3cret",
    )
    db.commit()
    from pathlib import Path

    cred = Path(connection.credentials_path)
    assert cred.is_file()
    assert oct(cred.stat().st_mode)[-3:] == "600"
    text = cred.read_text()
    assert "username=bob" in text
    assert "password=s3cret" in text


def test_create_connection_rejects_duplicate_hostname_and_purpose(db):
    cm.create_connection(db, hostname="WS09", share="A", username="a", password="p")
    db.commit()
    with pytest.raises(ValueError, match="already exists"):
        cm.create_connection(db, hostname="WS09", share="B", username="a", password="p")


def test_same_hostname_can_have_one_ingest_and_one_restore_destination(db):
    """Separate-entries design: an ingest connection and a restore-destination
    connection for the same physical machine must not collide (different
    mount dir, unit name, credentials file) and both must be creatable."""
    ingest = cm.create_connection(
        db, hostname="WS09B", share="Projects", username="a", password="p",
        purpose="ingest",
    )
    restore_dest = cm.create_connection(
        db, hostname="WS09B", share="Restores", username="a", password="p",
        purpose="restore_destination",
    )
    db.commit()

    assert ingest.mount_path != restore_dest.mount_path
    assert ingest.unit_name != restore_dest.unit_name
    assert ingest.credentials_path != restore_dest.credentials_path
    assert ingest.purpose == ConnectionPurpose.ingest
    assert restore_dest.purpose == ConnectionPurpose.restore_destination


def test_create_connection_rejects_bad_input(db):
    with pytest.raises(ValueError):
        cm.create_connection(db, hostname="bad host", share="A", username="a", password="p")
    with pytest.raises(ValueError):
        cm.create_connection(db, hostname="WS10", share="A", username="a", password="")
    with pytest.raises(ValueError):
        cm.create_connection(db, hostname="WS11", share="A", username="a", password="p",
                              smb_version="9.9")
    with pytest.raises(ValueError):
        cm.create_connection(db, hostname="WS11B", share="A", username="a", password="p",
                              purpose="nonsense")


def test_check_health_transitions_and_recheck(db):
    connection = cm.create_connection(db, hostname="WS12", share="A", username="a", password="p")
    db.commit()

    cm.check_health(db, connection)
    db.commit()
    assert connection.last_health == ConnectionHealth.healthy
    assert connection.last_error is None

    # flip the simulated mount to "down" and recheck
    backend = get_mount_backend()
    from pathlib import Path

    (Path(connection.mount_path) / ".simulate-down").write_text("down")

    cm.check_health(db, connection)
    db.commit()
    assert connection.last_health == ConnectionHealth.unhealthy
    assert connection.last_error

    from app.models import AuditLog

    healthy_to_unhealthy = db.scalars(
        select(AuditLog).where(AuditLog.action == "connection.health")
    ).all()
    assert len(healthy_to_unhealthy) == 1
    assert backend.backend == "simulator"


def test_mount_path_is_the_real_backend_mount_root_not_a_hardcoded_setting(db):
    """Regression: Connection.mount_path must come from the active backend's
    mount_root(), not always be built from smb_mount_base — otherwise a
    'healthy' connection under the simulator points a write job at a path the
    simulator never actually created (settings.smb_mount_base is /mnt/vader,
    which the simulator backend never touches)."""
    from pathlib import Path

    connection = cm.create_connection(db, hostname="WS12B", share="A", username="a", password="p")
    db.commit()

    backend = get_mount_backend()
    assert connection.mount_path == str(backend.mount_root() / "WS12B")
    assert not connection.mount_path.startswith("/mnt/vader")  # simulator backend in tests

    mount_dir = Path(connection.mount_path)
    assert mount_dir.is_dir()

    cm.check_health(db, connection)
    db.commit()
    assert connection.last_health == ConnectionHealth.healthy
    # exactly what the write-job form's suggested path must satisfy
    assert mount_dir.is_dir()


def test_list_healthy_connections_filters_by_purpose_and_health(db):
    a = cm.create_connection(db, hostname="WS13", share="A", username="a", password="p")
    b = cm.create_connection(db, hostname="WS14", share="B", username="a", password="p")
    restore_dest = cm.create_connection(
        db, hostname="WS13", share="R", username="a", password="p",
        purpose="restore_destination",
    )
    db.commit()
    cm.check_health(db, a)
    cm.check_health(db, restore_dest)
    db.commit()

    healthy_ingest = cm.list_healthy_connections(db, purpose=ConnectionPurpose.ingest)
    assert [c.hostname for c in healthy_ingest] == ["WS13"]
    assert b.hostname not in [c.hostname for c in healthy_ingest]

    healthy_restore = cm.list_healthy_connections(db, purpose=ConnectionPurpose.restore_destination)
    assert [c.id for c in healthy_restore] == [restore_dest.id]


def test_delete_connection_deprovisions_and_removes_row(db):
    connection = cm.create_connection(db, hostname="WS15", share="A", username="a", password="p")
    db.commit()
    from pathlib import Path

    cred_path = Path(connection.credentials_path)
    assert cred_path.is_file()

    cm.delete_connection(db, connection.id)
    db.commit()

    assert not cred_path.exists()
    assert db.get(Connection, connection.id) is None


# --- router -------------------------------------------------------------


def test_connections_list_and_new_form_render(client):
    assert client.get("/connections").status_code == 200
    assert client.get("/connections/new").status_code == 200
    assert client.get("/connections/new?purpose=restore_destination").status_code == 200


def test_create_connection_via_form_then_detail(client, db):
    r = client.post("/connections", data={
        "hostname": "WS20", "share": "Projects", "username": "carol",
        "password": "s3cret", "smb_version": "3.0", "purpose": "ingest",
    }, follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    detail = client.get(location)
    assert detail.status_code == 200
    assert "WS20" in detail.text
    assert "s3cret" not in detail.text


def test_create_restore_destination_via_form_is_read_write(client, db):
    r = client.post("/connections", data={
        "hostname": "WS20B", "share": "Restores", "username": "carol",
        "password": "s3cret", "purpose": "restore_destination",
    }, follow_redirects=False)
    assert r.status_code == 303
    connection = db.scalar(select(Connection).where(Connection.hostname == "WS20B"))
    assert connection.purpose == ConnectionPurpose.restore_destination
    assert cm._spec_for(connection).read_only is False

    marker = Path(connection.mount_path).parent.parent / "systemd" / f"{connection.unit_name}.marker"
    assert "rw" in marker.read_text()


def test_create_connection_via_form_rejects_bad_hostname(client):
    r = client.post("/connections", data={
        "hostname": "bad host", "share": "A", "username": "a", "password": "p",
    })
    assert r.status_code == 400
    assert "invalid hostname" in r.text


def test_unknown_connection_detail_is_404(client):
    assert client.get("/connections/999999").status_code == 404


def test_recheck_and_delete_round_trip(client):
    r = client.post("/connections", data={
        "hostname": "WS21", "share": "A", "username": "a", "password": "p",
    }, follow_redirects=False)
    connection_url = r.headers["location"]
    connection_id = connection_url.rsplit("/", 1)[-1]

    recheck = client.post(f"/connections/{connection_id}/recheck", follow_redirects=False)
    assert recheck.status_code == 303
    assert "healthy" in client.get(connection_url).text

    delete = client.post(f"/connections/{connection_id}/delete", follow_redirects=False)
    assert delete.status_code == 303
    assert client.get(connection_url).status_code == 404


def test_healthy_ingest_connection_appears_as_write_job_datalist_option(client):
    r = client.post("/connections", data={
        "hostname": "WS22", "share": "Projects", "username": "a", "password": "p",
    }, follow_redirects=False)
    connection_url = r.headers["location"]
    connection_id = connection_url.rsplit("/", 1)[-1]
    client.post(f"/connections/{connection_id}/recheck")

    form = client.get("/jobs/new/write")
    assert form.status_code == 200
    assert "WS22" in form.text
    assert 'list="known-sources"' in form.text
