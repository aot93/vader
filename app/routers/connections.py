from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_db
from app.models import Connection, ConnectionPurpose
from app.mounts import MountError
from app.services import connection_manager as cm
from app.web import templates

router = APIRouter(prefix="/connections", dependencies=[Depends(require_auth)])


@router.get("")
def list_connections(request: Request, db: Session = Depends(get_db)):
    connections = db.scalars(select(Connection).order_by(Connection.hostname)).all()
    ingest = [c for c in connections if c.purpose == ConnectionPurpose.ingest]
    restore_destinations = [c for c in connections if c.purpose == ConnectionPurpose.restore_destination]
    return templates.TemplateResponse(request, "connections_list.html", {
        "ingest": ingest, "restore_destinations": restore_destinations,
    })


@router.get("/new")
def new_connection_form(request: Request, purpose: str = "ingest"):
    try:
        purpose_enum = ConnectionPurpose(purpose)
    except ValueError:
        purpose_enum = ConnectionPurpose.ingest
    return templates.TemplateResponse(request, "connection_form.html", {
        "error": None, "purpose": purpose_enum.value,
    })


@router.post("")
def create_connection(
    request: Request,
    db: Session = Depends(get_db),
    hostname: str = Form(...),
    share: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    domain: str = Form(""),
    smb_version: str = Form("3.0"),
    purpose: str = Form("ingest"),
):
    try:
        connection = cm.create_connection(
            db, hostname=hostname, share=share, username=username, password=password,
            domain=domain, smb_version=smb_version, purpose=purpose,
        )
        db.commit()
    except (ValueError, MountError) as exc:
        db.rollback()
        return templates.TemplateResponse(
            request, "connection_form.html",
            {"error": str(exc), "purpose": purpose}, status_code=400,
        )
    return RedirectResponse(f"/connections/{connection.id}", status_code=303)


@router.get("/{connection_id}")
def connection_detail(connection_id: int, request: Request, db: Session = Depends(get_db)):
    connection = db.get(Connection, connection_id)
    if not connection:
        raise HTTPException(status_code=404, detail="unknown connection")
    return templates.TemplateResponse(request, "connection_detail.html", {"connection": connection})


@router.post("/{connection_id}/recheck")
def recheck_connection(connection_id: int, db: Session = Depends(get_db)):
    connection = db.get(Connection, connection_id)
    if not connection:
        raise HTTPException(status_code=404, detail="unknown connection")
    cm.check_health(db, connection)
    db.commit()
    return RedirectResponse(f"/connections/{connection_id}", status_code=303)


@router.post("/{connection_id}/delete")
def delete_connection(connection_id: int, db: Session = Depends(get_db)):
    try:
        cm.delete_connection(db, connection_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MountError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return RedirectResponse("/connections", status_code=303)
