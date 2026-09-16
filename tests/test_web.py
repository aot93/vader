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


def test_job_error_and_actions_show_on_both_full_page_and_the_polled_partial():
    """Regression test: job_detail.html's Result/Error/action-buttons used to
    live outside the HTMX-polled fragment, so they were frozen at whatever
    they looked like on the very first page load — a job that failed *after*
    that load would show a blank page with no error text and no Retry button
    until a manual reload. Both the full page and its own ?partial=1 render
    must reflect the job's current state."""
    from app.db import SessionLocal
    from app.models import Job, JobStatus, JobType

    with TestClient(app) as client:
        with SessionLocal() as db:
            job = Job(job_type=JobType.tape_import, params={"barcodes": ["X"]},
                      status=JobStatus.failed, error="boom: no free drives available")
            db.add(job)
            db.commit()
            job_id = job.id

        full = client.get(f"/jobs/{job_id}")
        assert full.status_code == 200
        assert "boom: no free drives available" in full.text
        assert "Re-run with same parameters" in full.text

        partial = client.get(f"/jobs/{job_id}?partial=1")
        assert partial.status_code == 200
        assert "boom: no free drives available" in partial.text
        assert "Re-run with same parameters" in partial.text


def test_pause_button_shown_only_for_a_running_write_job():
    from app.db import SessionLocal
    from app.models import Job, JobStatus, JobType

    with TestClient(app) as client:
        with SessionLocal() as db:
            write_job = Job(job_type=JobType.write, params={}, status=JobStatus.running)
            verify_job = Job(job_type=JobType.verify, params={}, status=JobStatus.running)
            db.add_all([write_job, verify_job])
            db.commit()
            write_id, verify_id = write_job.id, verify_job.id

        write_page = client.get(f"/jobs/{write_id}")
        assert f'action="/jobs/{write_id}/pause"' in write_page.text

        # pause is write-only for now (see PLAN_tape_import_optimization.md)
        verify_page = client.get(f"/jobs/{verify_id}")
        assert f'action="/jobs/{verify_id}/pause"' not in verify_page.text


def test_pause_endpoint_rejects_non_write_job_types():
    from app.db import SessionLocal
    from app.models import Job, JobStatus, JobType

    with TestClient(app) as client:
        with SessionLocal() as db:
            job = Job(job_type=JobType.verify, params={}, status=JobStatus.running)
            db.add(job)
            db.commit()
            job_id = job.id

        resp = client.post(f"/jobs/{job_id}/pause")
        assert resp.status_code == 400


def test_queued_write_job_pause_shortcuts_straight_to_paused():
    from app.db import SessionLocal
    from app.models import Job, JobStatus, JobType

    with TestClient(app) as client:
        with SessionLocal() as db:
            job = Job(job_type=JobType.write, params={}, status=JobStatus.queued)
            db.add(job)
            db.commit()
            job_id = job.id

        client.post(f"/jobs/{job_id}/pause", follow_redirects=False)

        with SessionLocal() as db:
            assert db.get(Job, job_id).status == JobStatus.paused

        page = client.get(f"/jobs/{job_id}")
        assert "Resume" in page.text


def test_resume_endpoint_rejects_a_non_paused_job():
    from app.db import SessionLocal
    from app.models import Job, JobStatus, JobType

    with TestClient(app) as client:
        with SessionLocal() as db:
            job = Job(job_type=JobType.write, params={}, status=JobStatus.completed)
            db.add(job)
            db.commit()
            job_id = job.id

        resp = client.post(f"/jobs/{job_id}/resume")
        assert resp.status_code == 400


def test_resume_endpoint_clones_a_paused_jobs_params():
    from app.db import SessionLocal
    from app.models import Job, JobStatus, JobType

    with TestClient(app) as client:
        with SessionLocal() as db:
            job = Job(job_type=JobType.write, params={"source_path": "/tmp/x"},
                     status=JobStatus.paused)
            db.add(job)
            db.commit()
            job_id = job.id

        resp = client.post(f"/jobs/{job_id}/resume", follow_redirects=False)
        assert resp.status_code == 303
        clone_id = int(resp.headers["location"].rsplit("/", 1)[-1])
        assert clone_id != job_id

        with SessionLocal() as db:
            clone = db.get(Job, clone_id)
            assert clone.status == JobStatus.queued
            assert clone.params == {"source_path": "/tmp/x"}
