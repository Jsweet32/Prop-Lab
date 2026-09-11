# Prop Lab — Multi-Sport Starter

This project turns the existing MLB Prop Dashboard into the first sport inside one expandable website.

## Current routes

- `/` — Prop Lab home page
- `/mlb` — live MLB Prop Model
- `/nfl` — NFL placeholder page
- `/nba` — NBA placeholder page
- `/cfb` — CFB placeholder page
- `/health` — health check

## Project layout

```text
app/
├── main.py
├── sports/
│   ├── mlb/
│   │   ├── routes.py
│   │   ├── config.py
│   │   ├── db.py
│   │   ├── kalshi_store.py
│   │   ├── providers.py
│   │   ├── updater.py
│   │   ├── model.py
│   │   ├── statcast.py
│   │   └── scheduler.py
│   ├── nfl/
│   │   └── routes.py
│   ├── nba/
│   │   └── routes.py
│   └── cfb/
│       └── routes.py
├── templates/
│   ├── home.html
│   ├── mlb/index.html
│   └── sports/coming_soon.html
└── static/
    ├── site.css
    └── style.css
```

## Environment variables

Keep the same values you already use in Render:

- `PARLAY_API_KEY`
- `DATABASE_URL`

Do not commit secret values into GitHub.

## Render

Build command:
`pip install -r requirements.txt`

Start command:
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`

## Important migration detail

The MLB model is not linked out to another website. It is served directly at `/mlb` from this same FastAPI application.

The MLB model/data code has been moved into `app/sports/mlb/`. Small compatibility files remain under `app/` so older imports do not immediately break.

## Future expansion

When NFL is ready, add its real files under `app/sports/nfl/`:
- `model.py`
- `providers.py`
- `db.py`
- `updater.py`
- `scheduler.py`

Then replace its placeholder router with the real dashboard route. NBA and CFB follow the same pattern.
