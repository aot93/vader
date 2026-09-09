"""Single-operator auth (framework doc §6).

If ``AUTH_TOKEN`` is unset, auth is disabled entirely — appropriate when the VM
is already network-isolated to trusted machines. If set, every page/API call
requires a session established at ``/login`` with that shared token.
"""
from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status

from app.config import get_settings


def login(request: Request, token: str) -> bool:
    settings = get_settings()
    if not settings.auth_enabled:
        return True
    if hmac.compare_digest(token.strip(), settings.auth_token):
        request.session["authed"] = True
        return True
    return False


def logout(request: Request) -> None:
    request.session.pop("authed", None)


def is_authed(request: Request) -> bool:
    settings = get_settings()
    if not settings.auth_enabled:
        return True
    return bool(request.session.get("authed"))


def require_auth(request: Request) -> None:
    if is_authed(request):
        return
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/login?next={request.url.path}"},
        )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")


AuthDep = Depends(require_auth)
