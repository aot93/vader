from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_db
from app.services.export_csv import full_catalog_csv, tape_catalog_csv

router = APIRouter(prefix="/export", dependencies=[Depends(require_auth)])


@router.get("/catalog.csv")
def export_catalog(db: Session = Depends(get_db)):
    return PlainTextResponse(
        full_catalog_csv(db),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=vader-catalog.csv"},
    )


@router.get("/tape/{barcode}.csv")
def export_tape(barcode: str, db: Session = Depends(get_db)):
    try:
        body = tape_catalog_csv(db, barcode)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PlainTextResponse(
        body,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=tape-{barcode}.csv"},
    )
