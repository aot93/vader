from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_db
from app.models import Source
from app.mounts import MountError
from app.services import source_manager as sm
from app.web import templates

router = APIRouter(prefix="/sources", dependencies=[Depends(require_auth)])


@router.get("")
def list_sources(request: Request, db: Session = Depends(get_db)):
    sources = db.scalars(select(Source).order_by(Source.hostname)).all()
    return templates.TemplateResponse(request, "sources_list.html", {"sources": sources})


@router.get("/new")
def new_source_form(request: Request):
    return templates.TemplateResponse(request, "source_form.html", {"error": None})


@router.post("")
def create_source(
    request: Request,
    db: Session = Depends(get_db),
    hostname: str = Form(...),
    share: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    domain: str = Form(""),
    smb_version: str = Form("3.0"),
):
    try:
        source = sm.create_source(
            db, hostname=hostname, share=share, username=username, password=password,
            domain=domain, smb_version=smb_version,
        )
        db.commit()
    except (ValueError, MountError) as exc:
        db.rollback()
        return templates.TemplateResponse(
            request, "source_form.html", {"error": str(exc)}, status_code=400
        )
    return RedirectResponse(f"/sources/{source.id}", status_code=303)


@router.get("/{source_id}")
def source_detail(source_id: int, request: Request, db: Session = Depends(get_db)):
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="unknown source")
    return templates.TemplateResponse(request, "source_detail.html", {"source": source})


@router.post("/{source_id}/recheck")
def recheck_source(source_id: int, db: Session = Depends(get_db)):
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="unknown source")
    sm.check_health(db, source)
    db.commit()
    return RedirectResponse(f"/sources/{source_id}", status_code=303)


@router.post("/{source_id}/delete")
def delete_source(source_id: int, db: Session = Depends(get_db)):
    try:
        sm.delete_source(db, source_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MountError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return RedirectResponse("/sources", status_code=303)
