from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.auth import login, logout
from app.web import templates

router = APIRouter()


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": None})


@router.post("/login")
def do_login(request: Request, token: str = Form(...), next: str = Form("/")):
    if login(request, token):
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next": next, "error": "Incorrect token."}, status_code=401
    )


@router.get("/logout")
def do_logout(request: Request):
    logout(request)
    return RedirectResponse("/login", status_code=303)
