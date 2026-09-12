from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_db
from app.models import BackupCategory, ConnectionPurpose
from app.services import connection_manager as cm
from app.services.catalog import SearchFilters, distinct_source_machines, search_content
from app.services.restore import prepare_restore
from app.web import templates

router = APIRouter(dependencies=[Depends(require_auth)])


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@router.get("/search")
def search_page(
    request: Request,
    db: Session = Depends(get_db),
    q: str | None = None,
    project: str | None = None,
    source_machine: str | None = None,
    backup_category: str | None = None,
    content_type: str | None = None,
    tape_barcode: str | None = None,
    written_from: str | None = None,
    written_to: str | None = None,
):
    filters = SearchFilters(
        q=q or None, project=project or None, source_machine=source_machine or None,
        backup_category=backup_category or None, content_type=content_type or None,
        tape_barcode=tape_barcode or None,
        written_from=_parse_dt(written_from), written_to=_parse_dt(written_to),
    )
    has_query = any([q, project, source_machine, backup_category, content_type, tape_barcode,
                     written_from, written_to])
    hits = search_content(db, filters) if has_query else []
    return templates.TemplateResponse(request, "search.html", {
        "hits": hits, "filters": filters, "has_query": has_query,
        "machines": distinct_source_machines(db),
        "categories": [c.value for c in BackupCategory],
        "content_types": [
            ("exr_sequence", "EXR image sequence (Type A)"),
            ("video_chunk", "Video chunk (Type B)"),
            ("config", "Config / log file (Type C)"),
            ("audio_media", "Audio / media file (Type D)"),
        ],
        "restore_destinations": cm.list_healthy_connections(
            db, purpose=ConnectionPurpose.restore_destination
        ),
    })


@router.post("/search/prepare-restore")
def prepare_restore_action(
    request: Request,
    db: Session = Depends(get_db),
    sequence_ids: list[int] = Form(default=[]),
    item_ids: list[int] = Form(default=[]),
    destination_path: str = Form(""),
    destination_connection_id: str = Form(""),
    destination_subpath: str = Form(""),
    include_manifests: bool = Form(False),
):
    try:
        req = prepare_restore(
            db,
            sequence_container_ids=sequence_ids,
            content_item_ids=item_ids,
            destination_path=destination_path.strip() or None,
            destination_connection_id=int(destination_connection_id)
            if destination_connection_id.strip() else None,
            destination_subpath=destination_subpath.strip() or None,
            include_manifests=include_manifests,
            requested_by="operator",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/restores/{req.id}", status_code=303)
