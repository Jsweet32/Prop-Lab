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


_DIRECT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36"
    ),
}

_NFL_MARKET_ALIASES = {
    # Passing
    "passing yards": "player_pass_yds",
    "pass yards": "player_pass_yds",
    "passing_yards": "player_pass_yds",
    "pass_yards": "player_pass_yds",
    "passing touchdowns": "player_pass_tds",
    "passing tds": "player_pass_tds",
    "pass tds": "player_pass_tds",
    "passing_touchdowns": "player_pass_tds",
    "passing_tds": "player_pass_tds",
    "completions": "player_pass_completions",
    "pass completions": "player_pass_completions",
    "passing completions": "player_pass_completions",
    "passing_completions": "player_pass_completions",
    "pass attempts": "player_pass_attempts",
    "passing attempts": "player_pass_attempts",
    "pass_attempts": "player_pass_attempts",
    "passing_attempts": "player_pass_attempts",
    "interceptions": "player_pass_interceptions",
    "passing interceptions": "player_pass_interceptions",
    "pass interceptions": "player_pass_interceptions",
    "passing_interceptions": "player_pass_interceptions",
    # Rushing
    "rushing yards": "player_rush_yds",
    "rush yards": "player_rush_yds",
    "rushing_yards": "player_rush_yds",
    "rush_yards": "player_rush_yds",
    "rushing attempts": "player_rush_attempts",
    "rush attempts": "player_rush_attempts",
    "carries": "player_rush_attempts",
    "rushing_attempts": "player_rush_attempts",
    "rush_attempts": "player_rush_attempts",
    "rushing touchdowns": "player_rush_tds",
    "rushing tds": "player_rush_tds",
    "rush tds": "player_rush_tds",
    "rushing_touchdowns": "player_rush_tds",
    "rushing_tds": "player_rush_tds",
    # Receiving
    "receiving yards": "player_reception_yds",
    "reception yards": "player_reception_yds",
    "receiving_yards": "player_reception_yds",
    "reception_yards": "player_reception_yds",
    "receptions": "player_receptions",
    "reception": "player_receptions",
    "receiving receptions": "player_receptions",
    "receiving touchdowns": "player_reception_tds",
    "receiving tds": "player_reception_tds",
    "reception tds": "player_reception_tds",
    "receiving_touchdowns": "player_reception_tds",
    "receiving_tds": "player_reception_tds",
    # TD
    "anytime touchdown": "player_anytime_td",
    "anytime td": "player_anytime_td",
    "touchdowns": "player_anytime_td",
    "total touchdowns": "player_anytime_td",
    "total tds": "player_anytime_td",
}


def _market_from_native(stat):
    s = str(stat or "").strip().lower()
    s = s.replace("+", " + ")
    s = re.sub(r"\s+", " ", s)
    return _NFL_MARKET_ALIASES.get(s)


def _price_from_options(options, choice):
    choice = choice.lower()
    for opt in options or []:
        if str(opt.get("choice") or "").lower() == choice:
            for key in ("american_price", "americanPrice", "price_american"):
                if opt.get(key) is not None:
                    try:
                        return float(opt.get(key))
                    except Exception:
                        pass
    return None


def _fetch_underdog_native(markets=None):
    """
    Pull Underdog's own board and keep ONLY line_type='balanced'.

    Underdog exposes alternate/special ladders in the same payload. The native
    line_type field is authoritative; 'balanced' is the regular line the user
    sees on the standard board.
    """
    url = "https://api.underdogfantasy.com/beta/v5/over_under_lines"
    r = requests.get(url, headers=_DIRECT_HEADERS, timeout=30)
    r.raise_for_status()
    payload = r.json()

    # Endpoint has historically returned either the collections at top level or
    # under a data object. Support both.
    root = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    lines = (root or {}).get("over_under_lines") or []
    appearances = {str(x.get("id")): x for x in ((root or {}).get("appearances") or [])}
    players = {str(x.get("id")): x for x in ((root or {}).get("players") or [])}
    games = {str(x.get("id")): x for x in ((root or {}).get("games") or [])}

    now_iso = datetime.now(timezone.utc).isoformat()
    allowed = set(markets or [])
    out = []

    for item in lines:
        if str(item.get("line_type") or "").lower() != "balanced":
            continue
        if item.get("live_event") is True:
            continue
        if str(item.get("status") or "active").lower() not in {"active", "open"}:
            continue

        ou = item.get("over_under") or {}
        astat = ou.get("appearance_stat") or {}
        mk = _market_from_native(astat.get("display_stat") or astat.get("stat"))
        if not mk or (allowed and mk not in allowed):
            continue

        app = appearances.get(str(astat.get("appearance_id"))) or {}
        player = players.get(str(app.get("player_id"))) or {}
        if str(player.get("sport_id") or "").upper() not in {"NFL", ""}:
            continue

        name = " ".join(
            x for x in [player.get("first_name"), player.get("last_name")] if x
        ).strip() or player.get("name") or player.get("display_name")
        if not name:
            continue

        try:
            line = float(item.get("stat_value"))
        except Exception:
            continue

        game = games.get(str(app.get("match_id"))) or {}
        options = item.get("options") or []

        out.append({
            "event_id": str(game.get("id") or item.get("id") or ""),
            "canonical_event_id": str(game.get("id") or item.get("id") or ""),
            "sport_key": SPORT_KEY,
            "commence_time": game.get("scheduled_at"),
            "home_team": game.get("home_team_name") or "",
            "away_team": game.get("away_team_name") or "",
            "bookmaker": "underdog",
            "source": "underdog",
            "bookmaker_title": "Underdog",
            "source_title": "Underdog",
            "player_name": name,
            "player": name,
            "market_key": mk,
            "market_label": astat.get("display_stat") or astat.get("stat") or mk,
            "line": line,
            "over_price": _price_from_options(options, "higher"),
            "under_price": _price_from_options(options, "lower"),
            "snapshot_time": now_iso,
            "last_update": now_iso,
            "age_seconds": 0,
            # Retain native marker for diagnostics and downstream guardrails.
            "line_type": "balanced",
            "native_line_id": item.get("id"),
        })
    return out


def _fetch_prizepicks_native(markets=None):
    """
    Pull PrizePicks projections and keep ONLY odds_type='standard'.

    Goblin, demon, promo, flash-sale, and other alternate tiers are intentionally
    excluded rather than guessed from the line value.
    """
    urls = [
        "https://api.prizepicks.com/projections",
        "https://partner-api.prizepicks.com/projections",
    ]
    params = {
        "league_id": 9,       # NFL
        "per_page": 1000,
        "single_stat": "true",
        "game_mode": "pickem",
    }
    headers = {
        **_DIRECT_HEADERS,
        "Referer": "https://app.prizepicks.com/",
        "Origin": "https://app.prizepicks.com",
    }

    payload = None
    last_error = None
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            if payload:
                break
        except Exception as exc:
            last_error = exc

    if not payload:
        if last_error:
            raise last_error
        return []

    included = payload.get("included") or []
    players = {
        str(x.get("id")): (x.get("attributes") or {})
        for x in included
        if x.get("type") in {"new_player", "player"}
    }

    allowed = set(markets or [])
    out = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for proj in payload.get("data") or []:
        attrs = proj.get("attributes") or {}
        odds_type = str(attrs.get("odds_type") or "standard").lower()

        # This is the exact native distinction we need.
        if odds_type != "standard":
            continue
        if attrs.get("is_promo") is True:
            continue
        if attrs.get("flash_sale_line_score") not in (None, "", False):
            continue
        if str(attrs.get("status") or "pre_game").lower() not in {"pre_game", "active", "open"}:
            continue

        mk = _market_from_native(attrs.get("stat_type"))
        if not mk or (allowed and mk not in allowed):
            continue

        rel = proj.get("relationships") or {}
        pdata = ((rel.get("new_player") or rel.get("player") or {}).get("data") or {})
        pinfo = players.get(str(pdata.get("id"))) or {}
        name = (
            pinfo.get("display_name")
            or pinfo.get("name")
            or attrs.get("name")
        )
        if not name:
            continue

        try:
            line = float(attrs.get("line_score"))
        except Exception:
            continue

        start = attrs.get("start_time")
        updated = attrs.get("updated_at") or now_iso

        out.append({
            "event_id": str(
                ((rel.get("game") or {}).get("data") or {}).get("id")
                or proj.get("id")
                or ""
            ),
            "canonical_event_id": str(
                ((rel.get("game") or {}).get("data") or {}).get("id")
                or proj.get("id")
                or ""
            ),
            "sport_key": SPORT_KEY,
            "commence_time": start,
            "home_team": "",
            "away_team": "",
            "bookmaker": "prizepicks",
            "source": "prizepicks",
            "bookmaker_title": "PrizePicks",
            "source_title": "PrizePicks",
            "player_name": name,
            "player": name,
            "market_key": mk,
            "market_label": attrs.get("stat_type") or mk,
            "line": line,
            # Standard PrizePicks is flat pick'em; keep price neutral.
            "over_price": 100.0,
            "under_price": -100.0,
            "snapshot_time": updated,
            "last_update": updated,
            "age_seconds": 0,
            "odds_type": "standard",
            "is_promo": False,
            "native_line_id": proj.get("id"),
        })

    return out


def _fetch_parlay_non_dfs(markets=None):
    if not PARLAY_API_KEY:
        raise RuntimeError("PARLAY_API_KEY is not set")

    # PrizePicks/Underdog are deliberately excluded here. Their native APIs
    # expose authoritative standard/main markers that Parlay's flattened feed
    # can lose, which is what caused alternate ladders to appear as main lines.
    books = [b for b in BOOKMAKERS if b not in {"prizepicks", "underdog"}]
    if not books:
        return []

    rows, offset, limit = [], 0, 10000
    while True:
        r = requests.get(
            f"{PARLAY_BASE}/sports/{SPORT_KEY}/props",
            headers={"X-API-Key": PARLAY_API_KEY},
            params={
                "bookmakers": ",".join(books),
                "markets": ",".join(markets) if markets else None,
                "limit": limit,
                "offset": offset,
                "maxAgeSec": 3600,
                "dfsOdds": "effective",
            },
            timeout=45,
        )
        if not r.ok:
            raise RuntimeError(
                f"ParlayAPI NFL props failed ({r.status_code}): {r.text[:500]}"
            )
        payload = r.json()
        batch = (
            payload.get("data") or payload.get("props") or payload.get("results") or []
            if isinstance(payload, dict)
            else payload if isinstance(payload, list) else []
        )
        rows.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return rows



def _fetch_parlay_underdog(markets=None):
    """Fallback Underdog feed when the native board is blocked from Render."""
    if not PARLAY_API_KEY:
        return []

    rows, offset, limit = [], 0, 10000
    while True:
        r = requests.get(
            f"{PARLAY_BASE}/sports/{SPORT_KEY}/props",
            headers={"X-API-Key": PARLAY_API_KEY},
            params={
                "bookmakers": "underdog",
                "markets": ",".join(markets) if markets else None,
                "limit": limit,
                "offset": offset,
                "maxAgeSec": 3600,
                "dfsOdds": "effective",
            },
            timeout=45,
        )
        if not r.ok:
            raise RuntimeError(
                f"ParlayAPI Underdog fallback failed ({r.status_code}): {r.text[:500]}"
            )
        payload = r.json()
        batch = (
            payload.get("data") or payload.get("props") or payload.get("results") or []
            if isinstance(payload, dict)
            else payload if isinstance(payload, list) else []
        )
        for row in batch:
            row = dict(row)
            row["bookmaker"] = "underdog"
            row["bookmaker_title"] = "Underdog"
            row["_fallback_source"] = "parlay"
            rows.append(row)

        if len(batch) < limit:
            break
        offset += limit
    return rows

def fetch_props(markets=None):
    """
    NFL source strategy:
      - PrizePicks: native standard projections.
      - Underdog: native balanced lines when available.
      - If Render cannot reach Underdog's native endpoint, use ParlayAPI as a
        fallback and let updater.py identify the regular rung by anchoring it to
        PrizePicks' native STANDARD line for the same player/market.
      - Fliff/Kalshi/etc: ParlayAPI.
    """
    rows = _fetch_parlay_non_dfs(markets=markets)
    diagnostics = []

    # PrizePicks first because its native STANDARD line is used as the fallback
    # anchor for Underdog if the native Underdog endpoint is blocked.
    try:
        pp = _fetch_prizepicks_native(markets=markets)
        rows.extend(pp)
    except Exception as exc:
        diagnostics.append(f"PrizePicks native feed unavailable: {exc}")

    try:
        ud = _fetch_underdog_native(markets=markets)
        if ud:
            rows.extend(ud)
        else:
            diagnostics.append("Underdog native feed returned 0 rows; using Parlay fallback.")
            rows.extend(_fetch_parlay_underdog(markets=markets))
    except Exception as exc:
        diagnostics.append(f"Underdog native feed unavailable: {exc}; using Parlay fallback.")
        try:
            rows.extend(_fetch_parlay_underdog(markets=markets))
        except Exception as fallback_exc:
            diagnostics.append(f"Underdog fallback unavailable: {fallback_exc}")

    if diagnostics:
        print(" | ".join(diagnostics))

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
