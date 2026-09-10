from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.auth import require_auth
from app.services.manual import asset_path, list_pages, render_page
from app.web import templates

router = APIRouter(prefix="/help", dependencies=[Depends(require_auth)])


@router.get("")
def help_home(request: Request):
    return _render(request, "index")


@router.get("/assets/{rel:path}")
def help_asset(rel: str):
    try:
        return FileResponse(asset_path(rel))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="manual asset not found") from exc


@router.get("/{slug}")
def help_page(slug: str, request: Request):
    return _render(request, slug)


def _render(request: Request, slug: str):
    try:
        page = render_page(slug)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="manual page not found") from exc
    return templates.TemplateResponse(request, "help.html", {
        "page": page,
        "pages": list_pages(),
        "current": page.slug,
    })
