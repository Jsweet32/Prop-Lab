from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

@router.get("/cfb", response_class=HTMLResponse)
def page(request: Request):
    return templates.TemplateResponse(
        "sports/coming_soon.html",
        {
            "request": request,
            "league": "CFB",
            "league_full": "College Football",
            "next_step": "player props, team context, usage, injuries and matchup data",
        },
    )
