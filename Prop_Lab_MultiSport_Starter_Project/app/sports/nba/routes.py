from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

@router.get("/nba", response_class=HTMLResponse)
def page(request: Request):
    return templates.TemplateResponse(
        "sports/coming_soon.html",
        {
            "request": request,
            "league": "NBA",
            "league_full": "National Basketball Association",
            "next_step": "player props, minutes, usage, injuries, pace and matchup data",
        },
    )
