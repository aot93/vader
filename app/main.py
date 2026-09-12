"""FastAPI application entry point.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import contextlib
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import init_db, session_scope
from app.jobs import start_worker, stop_worker
from app.routers import (
    audit,
    auth_routes,
    connections,
    dashboard,
    export,
    help,
    jobs,
    library,
    restores,
    search,
    tapes,
)
from app.services.connection_health_monitor import (
    start_connection_health_monitor,
    stop_connection_health_monitor,
)
from app.web import templates

settings = get_settings()


def _seed_scratch_tapes() -> None:
    """On first run with the simulator, register the seeded cartridges as
    scratch tapes so the write pipeline has somewhere to go."""
    from sqlalchemy import select

    from app.hardware import get_hardware
    from app.models import Tape, TapeStatus

    hw = get_hardware()
    try:
        state = hw.library_status()
    except Exception:  # noqa: BLE001
        return
    with session_scope() as db:
        known = {t.barcode for t in db.scalars(select(Tape)).all()}
        for slot in state.slots:
            if not slot.barcode or slot.is_cleaning_tape or slot.barcode in known:
                continue
            db.add(Tape(
                barcode=slot.barcode,
                lto_generation="LTO-8",
                capacity_native_bytes=settings.sim_tape_capacity_bytes,
                status=TapeStatus.scratch,
                physical_location=f"slot {slot.number}",
            ))


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if settings.hardware_backend == "simulator":
        _seed_scratch_tapes()
    worker = start_worker()
    health_monitor = start_connection_health_monitor()
    try:
        yield
    finally:
        stop_worker()
        worker.join(timeout=2)
        stop_connection_health_monitor()
        health_monitor.join(timeout=2)


app = FastAPI(title="Vader — Tape Archive Control", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="lax")

_static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

app.include_router(auth_routes.router)
app.include_router(dashboard.router)
app.include_router(connections.router)
app.include_router(library.router)
app.include_router(tapes.router)
app.include_router(search.router)
app.include_router(jobs.router)
app.include_router(restores.router)
app.include_router(export.router)
app.include_router(audit.router)
app.include_router(help.router)


@app.exception_handler(404)
async def not_found(request: Request, exc):  # noqa: ANN001, ARG001
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(
            templates.get_template("error.html").render(request=request, code=404,
                                                        detail="Page not found"),
            status_code=404,
        )
    return HTMLResponse('{"detail":"not found"}', status_code=404, media_type="application/json")
