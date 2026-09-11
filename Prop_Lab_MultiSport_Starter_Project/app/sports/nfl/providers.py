import io
import re
import time
import threading
from datetime import datetime, timezone

import pandas as pd
import requests

from .config import (
    PARLAY_API_KEY, SPORT_KEY, BOOKMAKERS, BOOK_TITLES,
    NFLVERSE_CACHE_SECONDS, SCOREBOARD_CACHE_SECONDS
)

PARLAY_BASE = "https://parlay-api.com/v1"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
NFLVERSE_STATS = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv"
NFLVERSE_PLAYERS = "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv"

_lock = threading.RLock()
_cache = {}

def _cache_get(key, max_age):
    # These cached objects are treated as read-only. Returning them directly is
    # intentional: deep-copying full NFL season datasets on every player lookup
    # made the original refresh take many minutes.
    with _lock:
        item = _cache.get(key)
        if not item:
            return None
        ts, value = item
        if time.time() - ts > max_age:
            return None
        return value

def _cache_set(key, value):
    with _lock:
        _cache[key] = (time.time(), value)
    return value

def normalize_name(v):
    s = str(v or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    return " ".join(s.split())

def nfl_season(now=None):
    now = now or datetime.now(timezone.utc)
    # January/February games belong to the season that started the prior fall.
    return now.year if now.month >= 3 else now.year - 1

def fetch_props(markets=None):
    if not PARLAY_API_KEY:
        raise RuntimeError("PARLAY_API_KEY is not set")
    rows, offset, limit = [], 0, 10000
    while True:
        r = requests.get(
            f"{PARLAY_BASE}/sports/{SPORT_KEY}/props",
            headers={"X-API-Key": PARLAY_API_KEY},
            params={
                "bookmakers": ",".join(BOOKMAKERS),
                "markets": ",".join(markets) if markets else None,
                "limit": limit,
                "offset": offset,
                "maxAgeSec": 3600,
                "dfsOdds": "effective",
            },
            timeout=45,
        )
        if not r.ok:
            raise RuntimeError(f"ParlayAPI NFL props failed ({r.status_code}): {r.text[:500]}")
        payload = r.json()
        batch = (
            payload.get("data") or payload.get("props") or payload.get("results") or []
            if isinstance(payload, dict) else payload if isinstance(payload, list) else []
        )
        rows.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return rows

def _to_records(url):
    r = requests.get(url, timeout=45)
    r.raise_for_status()
    return pd.read_csv(io.BytesIO(r.content), low_memory=False).where(pd.notna, None).to_dict("records")

def fetch_player_stats(season):
    key = ("stats", int(season))
    cached = _cache_get(key, NFLVERSE_CACHE_SECONDS)
    if cached is not None:
        return cached
    try:
        return _cache_set(key, _to_records(NFLVERSE_STATS.format(season=int(season))))
    except Exception:
        return _cache_set(key, [])

def fetch_players():
    key = ("players",)
    cached = _cache_get(key, 12 * 3600)
    if cached is not None:
        return cached
    try:
        return _cache_set(key, _to_records(NFLVERSE_PLAYERS))
    except Exception:
        return _cache_set(key, [])


def _players_by_name():
    key = ("players_by_name",)
    cached = _cache_get(key, 12 * 3600)
    if cached is not None:
        return cached

    index = {}
    for p in fetch_players():
        candidates = [
            p.get("display_name"),
            p.get("common_first_name") and f"{p.get('common_first_name')} {p.get('last_name')}",
            p.get("short_name"),
            p.get("football_name"),
        ]
        for candidate in candidates:
            n = normalize_name(candidate)
            if n:
                index.setdefault(n, p)
    return _cache_set(key, index)


def _stats_by_player(season):
    season = int(season)
    key = ("stats_by_player", season)
    cached = _cache_get(key, NFLVERSE_CACHE_SECONDS)
    if cached is not None:
        return cached

    index = {}
    for r in fetch_player_stats(season):
        n = normalize_name(r.get("player_display_name") or r.get("player_name"))
        if n:
            index.setdefault(n, []).append(r)
    for rows in index.values():
        rows.sort(key=lambda x: int(x.get("week") or 0))
    return _cache_set(key, index)


def player_profile(name):
    target = normalize_name(name)
    if not target:
        return {}
    p = _players_by_name().get(target)
    if not p:
        return {}
    return {
        "player_id": p.get("gsis_id") or p.get("espn_id"),
        "headshot_url": p.get("headshot") or p.get("headshot_url"),
        "position": p.get("position") or p.get("position_group"),
        "player_team": p.get("team_abbr") or p.get("team"),
    }


def player_history(name, current_season=None):
    current_season = current_season or nfl_season()
    target = normalize_name(name)
    if not target:
        return []

    prior = _stats_by_player(current_season - 1).get(target, [])
    current = _stats_by_player(current_season).get(target, [])
    out = list(prior) + list(current)
    out.sort(key=lambda x: (int(x.get("season") or 0), int(x.get("week") or 0)))
    return out


def fetch_scoreboard():
    key = ("scoreboard",)
    cached = _cache_get(key, SCOREBOARD_CACHE_SECONDS)
    if cached is not None:
        return cached
    r = requests.get(ESPN_SCOREBOARD, timeout=25)
    r.raise_for_status()
    payload = r.json()
    league = (payload.get("leagues") or [{}])[0]
    week = ((league.get("calendar") or [{}])[0].get("entries") or [])
    games = []
    for ev in payload.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        teams = {}
        for c in competitors:
            t = c.get("team") or {}
            side = c.get("homeAway")
            recs = c.get("records") or []
            teams[side] = {
                "name": t.get("displayName") or t.get("name"),
                "abbr": t.get("abbreviation"),
                "logo": t.get("logo"),
                "score": c.get("score"),
                "record": recs[0].get("summary") if recs else "",
            }
        status = ((comp.get("status") or {}).get("type") or {})
        detail = status.get("shortDetail") or status.get("detail") or ""
        games.append({
            "event_id": ev.get("id"),
            "date": ev.get("date"),
            "week": ((ev.get("week") or {}).get("number") or
                     ((payload.get("week") or {}).get("number"))),
            "home": teams.get("home") or {},
            "away": teams.get("away") or {},
            "status": status.get("state") or "pre",
            "status_detail": detail,
        })
    return _cache_set(key, games)

def game_context(home_team, away_team, commence_time=None):
    home = normalize_name(home_team)
    away = normalize_name(away_team)
    for g in fetch_scoreboard():
        gh = normalize_name((g.get("home") or {}).get("name"))
        ga = normalize_name((g.get("away") or {}).get("name"))
        if {home, away} == {gh, ga}:
            return g
    return {}

def book_title(row):
    key = str(row.get("bookmaker") or row.get("source") or "").lower().strip()

    # Keep DFS source names identical to the MLB board even when ParlayAPI
    # supplies display titles such as "Underdog Fantasy".
    if key in {"underdog", "underdog_fantasy"}:
        return "Underdog"
    if key in {"prizepicks", "prizepicks_mobile", "prizepicks_fantasy"}:
        return "PrizePicks"
    if key == "fliff":
        return "Fliff"
    if key == "kalshi":
        return "Kalshi"

    raw_title = row.get("bookmaker_title") or row.get("source_title")
    if raw_title:
        lowered = str(raw_title).lower()
        if "underdog" in lowered:
            return "Underdog"
        if "prizepicks" in lowered:
            return "PrizePicks"

    return BOOK_TITLES.get(key) or raw_title or key.title()
