from datetime import datetime, timezone
import json
import subprocess
import sys
from threading import Lock, Thread
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import TIMEZONE
from .db import latest_rows, latest_snapshot
from .history import performance_data, grade_pending
from .providers import fetch_scoreboard

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_refresh_lock = Lock()
_refresh_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_result": None,
    "last_error": None,
    "stage": None,
}

def _format_eastern_timestamp(value):
    if not value:
        return "No data yet"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        eastern = dt.astimezone(ZoneInfo(TIMEZONE))
        return eastern.strftime("%-I:%M %p ") + (eastern.tzname() or "ET")
    except Exception:
        return str(value)

def _refresh_background():
    if not _refresh_lock.acquire(blocking=False):
        return

    _refresh_state["running"] = True
    _refresh_state["started_at"] = datetime.now(timezone.utc).isoformat()
    _refresh_state["finished_at"] = None
    _refresh_state["last_error"] = None
    _refresh_state["last_result"] = None
    _refresh_state["stage"] = "Refreshing NFL props"

    try:
        code = (
            "import json; "
            "from app.sports.nfl.updater import refresh_all; "
            "result=refresh_all(); "
            "print(json.dumps({'props': result}, default=str))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=360,
            check=False,
        )

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            detail = detail[-1800:] if detail else "NFL refresh process exited with an error."
            raise RuntimeError(detail)

        output = (completed.stdout or "").strip().splitlines()
        result = None
        if output:
            try:
                result = json.loads(output[-1])
            except Exception:
                result = {"message": output[-1]}

        _refresh_state["last_result"] = result or {"status": "complete"}
        _refresh_state["stage"] = "Complete"

    except subprocess.TimeoutExpired:
        _refresh_state["last_error"] = (
            "NFL refresh exceeded 6 minutes and was stopped. "
            "The previous snapshot was preserved."
        )
        _refresh_state["stage"] = "Timed out"
    except Exception as exc:
        _refresh_state["last_error"] = str(exc)
        _refresh_state["stage"] = "Failed"
    finally:
        _refresh_state["running"] = False
        _refresh_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        _refresh_lock.release()



def start_refresh():
    """
    Start the NFL refresh as a server-side thread.

    It continues running even if the user changes pages or closes the browser tab.
    """
    if _refresh_state["running"]:
        return False

    Thread(
        target=_refresh_background,
        name="nfl-prop-refresh",
        daemon=True,
    ).start()
    return True

@router.get("/nfl", response_class=HTMLResponse)
def nfl_dashboard(request: Request):
    snap = latest_snapshot()
    return templates.TemplateResponse(
        "nfl/index.html",
        {
            "request": request,
            "rows": latest_rows(),
            "snapshot": snap,
            "snapshot_display": _format_eastern_timestamp(snap),
            "refresh_state": dict(_refresh_state),
            "performance": performance_data(),
        },
    )

@router.post("/nfl/refresh")
def refresh():
    start_refresh()
    return RedirectResponse("/nfl?refresh=started", status_code=303)

@router.get("/api/nfl/refresh-status")
def refresh_status():
    return {**_refresh_state, "snapshot": latest_snapshot()}

@router.get("/api/nfl/board")
def board():
    return {"snapshot": latest_snapshot(), "rows": latest_rows()}

@router.get("/api/nfl/scoreboard")
def scoreboard():
    try:
        return {
            "games": fetch_scoreboard(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {
            "games": [],
            "error": str(exc),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

@router.post("/nfl/performance/grade")
def grade_history():
    grade_pending()
    return RedirectResponse("/performance?sport=nfl", status_code=303)

@router.get("/nfl/performance")
def old_performance_route():
    return RedirectResponse("/performance?sport=nfl", status_code=303)
