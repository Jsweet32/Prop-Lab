from io import BytesIO
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from threading import Lock, Thread

from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook

from .db import latest_rows, latest_snapshot
from .updater import refresh_fast
from .providers import fetch_kalshi_markets, fetch_mlb_scoreboard
from .kalshi_store import (
    replace_kalshi_snapshot,
    latest_kalshi_rows,
    latest_kalshi_snapshot,
)
from .config import TIMEZONE
from .history import performance_data, grade_pending_history

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_refresh_lock = Lock()
_refresh_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_result": None,
    "last_error": None,
}


def _refresh_kalshi_background():
    try:
        kalshi_rows = fetch_kalshi_markets()
        replace_kalshi_snapshot(kalshi_rows)
    except Exception:
        pass


def _run_refresh_background():
    if not _refresh_lock.acquire(blocking=False):
        return

    _refresh_state["running"] = True
    _refresh_state["started_at"] = datetime.now(timezone.utc).isoformat()
    _refresh_state["finished_at"] = None
    _refresh_state["last_error"] = None

    try:
        result = refresh_fast()
        _refresh_state["last_result"] = {"props": result}
        Thread(
            target=_refresh_kalshi_background,
            name="mlb-kalshi-refresh",
            daemon=True,
        ).start()
    except Exception as exc:
        _refresh_state["last_error"] = str(exc)
    finally:
        _refresh_state["running"] = False
        _refresh_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        _refresh_lock.release()


def _format_eastern_timestamp(value):
    if not value:
        return "No data yet"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        eastern = dt.astimezone(ZoneInfo(TIMEZONE))
        zone = eastern.tzname() or "ET"
        return eastern.strftime("%-I:%M %p ") + zone
    except Exception:
        return str(value)


@router.get("/mlb", response_class=HTMLResponse)
def mlb_dashboard(request: Request):
    snapshot = latest_snapshot()
    kalshi_rows = latest_kalshi_rows()
    return templates.TemplateResponse(
        "mlb/index.html",
        {
            "request": request,
            "rows": latest_rows(),
            "snapshot": snapshot,
            "snapshot_display": _format_eastern_timestamp(snapshot),
            "kalshi_rows": kalshi_rows,
            "kalshi_snapshot": latest_kalshi_snapshot(),
            "refresh_state": dict(_refresh_state),
            "performance": performance_data(),
        },
    )


@router.post("/mlb/refresh")
def refresh(background_tasks: BackgroundTasks):
    if not _refresh_state["running"]:
        background_tasks.add_task(_run_refresh_background)
    return RedirectResponse("/mlb?refresh=started", status_code=303)


@router.get("/api/mlb/refresh-status")
def refresh_status():
    return {
        **_refresh_state,
        "snapshot": latest_snapshot(),
    }


@router.get("/api/mlb/scoreboard")
def api_scoreboard():
    try:
        eastern_now = datetime.now(ZoneInfo(TIMEZONE))
        games = fetch_mlb_scoreboard(eastern_now.date().isoformat())
        return {
            "date": eastern_now.date().isoformat(),
            "games": games,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {
            "date": datetime.now(ZoneInfo(TIMEZONE)).date().isoformat(),
            "games": [],
            "error": str(exc),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


@router.get("/api/mlb/board")
def api_board():
    return {"snapshot": latest_snapshot(), "rows": latest_rows()}


@router.get("/mlb/performance")
def mlb_performance():
    return RedirectResponse("/performance?sport=mlb", status_code=303)


@router.post("/mlb/performance/grade")
def grade_performance_now():
    grade_pending_history()
    return RedirectResponse("/performance?sport=mlb", status_code=303)


@router.get("/mlb/export.xlsx")
def export_excel():
    rows = latest_rows()
    wb = Workbook()
    ws = wb.active
    ws.title = "MLB Prop Board"
    headers = [
        "Verdict", "Side", "Grade", "Sportsbook", "Player", "Market", "Line", "Price",
        "Model %", "Edge %", "Edge Type", "Season %", "L30 %", "L10 %",
        "Lineup", "Line Age Sec", "Opponent", "Probable Pitcher", "Venue", "Status"
    ]
    ws.append(headers)

    for r in rows:
        price = r["under_price"] if r.get("recommended_side") == "UNDER" else r["over_price"]
        ws.append([
            r["verdict"], r["recommended_side"], r["grade"], r["bookmaker_title"],
            r["player"], r["market"], r["line"], price, r["recommended_prob"],
            r["display_edge"], r["edge_type"], r["season_rate"], r["l30_rate"],
            r["l10_rate"], r["lineup_status"], r["line_age_seconds"],
            r["opponent"], r["probable_pitcher"], r["venue"], r["status_note"]
        ])

    for col in ["I", "J", "L", "M", "N"]:
        for cell in ws[col][1:]:
            cell.number_format = "0.0%"

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)

    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=MLB_Prop_Board.xlsx"},
    )
