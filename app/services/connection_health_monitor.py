"""Background thread that sweeps every configured connection every
``SMB_HEALTH_INTERVAL_SECONDS`` (default 300s / 5 minutes) and records
last_health / last_error, so a share going offline shows up on the
Connections page without the operator having to click "Recheck now" themselves.

Deliberately its own thread rather than a ``Job`` — it is a recurring sweep,
not a one-shot operator-triggered task, so it does not belong in the jobs
list. Mirrors ``app.jobs.worker.JobWorker``'s start/stop shape.
"""
from __future__ import annotations

import threading

from app.config import get_settings
from app.db import session_scope
from app.services.connection_manager import check_all_connections


class ConnectionHealthMonitor(threading.Thread):
    daemon = True

    def __init__(self) -> None:
        super().__init__(name="vader-connection-health")
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        interval = max(30, get_settings().smb_health_interval_seconds)
        while not self._stop.is_set():
            try:
                with session_scope() as db:
                    check_all_connections(db)
            except Exception:  # noqa: BLE001 - a bad sweep must not kill the thread
                pass
            self._stop.wait(interval)


_monitor: ConnectionHealthMonitor | None = None


def start_connection_health_monitor() -> ConnectionHealthMonitor:
    global _monitor
    if _monitor is None or not _monitor.is_alive():
        _monitor = ConnectionHealthMonitor()
        _monitor.start()
    return _monitor


def stop_connection_health_monitor() -> None:
    global _monitor
    if _monitor is not None:
        _monitor.stop()
        _monitor = None
