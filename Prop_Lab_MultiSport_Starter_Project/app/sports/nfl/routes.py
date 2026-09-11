from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

@router.get("/nfl", response_class=HTMLResponse)
def page(request: Request):
    return templates.TemplateResponse(
        "sports/coming_soon.html",
        {
            "request": request,
            "league": "NFL",
            "league_full": "National Football League",
            "next_step": "player props, usage, injuries, weather, matchup data",
        },
    )
