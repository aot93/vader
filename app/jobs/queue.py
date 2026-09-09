"""Enqueue helpers for background jobs."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Job, JobStatus, JobType


def enqueue(db: Session, job_type: JobType, params: dict, *, created_by: str = "operator") -> Job:
    job = Job(job_type=job_type, params=params, created_by=created_by, status=JobStatus.queued)
    db.add(job)
    db.flush()
    return job
