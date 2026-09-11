from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from .sports.mlb.routes import router as mlb_router
from .sports.nfl.routes import router as nfl_router
from .sports.nba.routes import router as nba_router
from .sports.cfb.routes import router as cfb_router
from .sports.mlb.db import init_db as init_mlb_db
from .sports.mlb.kalshi_store import init_kalshi_db
from .sports.mlb.history import init_history_db
from .sports.mlb.scheduler import start_scheduler as start_mlb_scheduler
from .sports.mlb.updater import refresh_all as refresh_mlb_all
from .sports.nfl.db import init_db as init_nfl_db
from .sports.nfl.history import init_history_db as init_nfl_history_db
from .sports.nfl.scheduler import start_scheduler as start_nfl_scheduler
from .sports.nfl.updater import refresh_all as refresh_nfl_all
from .core.performance import global_performance_data

app = FastAPI(
    title="Prop Lab",
    description="Multi-sport player prop analytics",
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

app.include_router(mlb_router)
app.include_router(nfl_router)
app.include_router(nba_router)
app.include_router(cfb_router)

_mlb_scheduler = None
_nfl_scheduler = None


@app.on_event("startup")
def startup():
    global _mlb_scheduler, _nfl_scheduler
    init_mlb_db()
    init_kalshi_db()
    init_history_db()
    _mlb_scheduler = start_mlb_scheduler()
    init_nfl_db()
    init_nfl_history_db()
    _nfl_scheduler = start_nfl_scheduler()


def _refresh_all_boards():
    """Refresh every currently-live prop model without one sport blocking the other."""
    from concurrent.futures import ThreadPoolExecutor

    jobs = [refresh_mlb_all, refresh_nfl_all]
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(job) for job in jobs]
        for future in futures:
            try:
                future.result()
            except Exception:
                # One sport failing should not prevent the other live boards from refreshing.
                pass


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("home.html", {"request": request})


@app.post("/refresh-all")
def refresh_all_boards(background_tasks: BackgroundTasks):
    background_tasks.add_task(_refresh_all_boards)
    return RedirectResponse("/?refresh=started", status_code=303)


@app.get("/performance", response_class=HTMLResponse)
def performance(request: Request, sport: str = "all"):
    data = global_performance_data()
    valid = {"all", "mlb", "nfl", "nba", "cfb"}
    selected = sport.lower() if sport.lower() in valid else "all"
    return templates.TemplateResponse(
        "performance.html",
        {
            "request": request,
            "selected_sport": selected,
            **data,
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}
