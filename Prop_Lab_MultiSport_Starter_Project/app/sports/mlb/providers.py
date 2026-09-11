import requests
import time
import threading
import json
from copy import deepcopy
from datetime import date, datetime, timezone, timedelta
from .config import PARLAY_API_KEY, SPORT_KEY, BOOKMAKERS, DATABASE_URL
try:
    from .config import (
        SCOREBOARD_CACHE_SECONDS,
        STANDINGS_CACHE_SECONDS,
        PLAYER_SEARCH_CACHE_SECONDS,
        PERSON_CACHE_SECONDS,
        GAME_LOG_CACHE_SECONDS,
        TEAM_SCHEDULE_CACHE_SECONDS,
        GAME_FEED_CACHE_SECONDS,
        PITCHER_STATS_CACHE_SECONDS,
    )
except Exception:
    SCOREBOARD_CACHE_SECONDS = 20
    STANDINGS_CACHE_SECONDS = 600
    PLAYER_SEARCH_CACHE_SECONDS = 43200
    PERSON_CACHE_SECONDS = 43200
    GAME_LOG_CACHE_SECONDS = 1800
    TEAM_SCHEDULE_CACHE_SECONDS = 90
    GAME_FEED_CACHE_SECONDS = 20
    PITCHER_STATS_CACHE_SECONDS = 1800

PARLAY_BASE = "https://parlay-api.com/v1"
MLB_BASE = "https://statsapi.mlb.com/api/v1"



_mlb_cache_lock = threading.RLock()
_mlb_cache = {
    "player_search": {},
    "person": {},
    "game_log": {},
    "team_schedule": {},
    "game_feed": {},
    "pitcher_stats": {},
}

_persistent_cache_ready = False

def _cache_key(key):
    try:
        return json.dumps(key, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return str(key)

def _ensure_persistent_cache():
    global _persistent_cache_ready
    if _persistent_cache_ready or not DATABASE_URL:
        return
    try:
        import psycopg
        with psycopg.connect(DATABASE_URL, connect_timeout=10) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS mlb_api_cache (
                    bucket TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    created_at DOUBLE PRECISION NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (bucket, cache_key)
                )
            """)
        _persistent_cache_ready = True
    except Exception:
        # Cache persistence is an optimization; never break the board for it.
        return

def _persistent_get(bucket, key, ttl):
    if not DATABASE_URL:
        return None
    _ensure_persistent_cache()
    if not _persistent_cache_ready:
        return None
    try:
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10) as c:
            row=c.execute(
                "SELECT created_at,payload FROM mlb_api_cache WHERE bucket=%s AND cache_key=%s",
                (bucket, _cache_key(key))
            ).fetchone()
            if not row:
                return None
            if time.time() - float(row["created_at"]) > ttl:
                c.execute(
                    "DELETE FROM mlb_api_cache WHERE bucket=%s AND cache_key=%s",
                    (bucket, _cache_key(key))
                )
                return None
            return json.loads(row["payload"])
    except Exception:
        return None

def _persistent_set(bucket, key, value):
    if not DATABASE_URL:
        return
    _ensure_persistent_cache()
    if not _persistent_cache_ready:
        return
    try:
        import psycopg
        payload=json.dumps(value, separators=(",", ":"), default=str)
        with psycopg.connect(DATABASE_URL, connect_timeout=10) as c:
            c.execute("""
                INSERT INTO mlb_api_cache(bucket,cache_key,created_at,payload)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT(bucket,cache_key)
                DO UPDATE SET created_at=EXCLUDED.created_at,payload=EXCLUDED.payload
            """,(bucket,_cache_key(key),time.time(),payload))
    except Exception:
        pass

def _cache_get(bucket, key, ttl):
    now = time.time()
    with _mlb_cache_lock:
        item = _mlb_cache[bucket].get(key)
        if item:
            created, value = item
            if now - created <= ttl:
                return value
            _mlb_cache[bucket].pop(key, None)

    value = _persistent_get(bucket, key, ttl)
    if value is not None:
        with _mlb_cache_lock:
            _mlb_cache[bucket][key] = (now, value)
        return value
    return None

def _cache_set(bucket, key, value):
    with _mlb_cache_lock:
        _mlb_cache[bucket][key] = (time.time(), value)
    _persistent_set(bucket, key, value)
    return value

_MLB_MODEL_MARKETS = [
    "player_hits",
    "player_total_bases",
    "player_home_runs",
    "player_rbis",
    "player_runs",
    "player_walks",
    "player_strikeouts",
]


def _fetch_prop_batch(markets):
    r = requests.get(
        f"{PARLAY_BASE}/sports/{SPORT_KEY}/props",
        headers={"X-API-Key": PARLAY_API_KEY},
        params={
            "bookmakers": ",".join(BOOKMAKERS),
            "markets": ",".join(markets),
            "limit": 10000,
            "maxAgeSec": 3600,
            "dfsOdds": "effective",
        },
        timeout=45,
    )
    if not r.ok:
        raise RuntimeError(
            f"ParlayAPI props request failed ({r.status_code}): {r.text[:500]}"
        )

    payload = r.json()
    if isinstance(payload, dict):
        return payload.get("data") or payload.get("props") or payload.get("results") or []
    if isinstance(payload, list):
        return payload
    return []


def _cache_warm(bucket, items, ttl):
    """
    Prime the in-process cache for a set of keys from persistent storage using
    one Postgres connection. This is used by the manual MLB refresh so we do not
    open a new Neon connection for every player/game-log lookup.
    """
    if not items or not DATABASE_URL:
        return

    _ensure_persistent_cache()
    if not _persistent_cache_ready:
        return

    missing = []
    now = time.time()
    with _mlb_cache_lock:
        for key in items:
            current = _mlb_cache[bucket].get(key)
            if current and now - current[0] <= ttl:
                continue
            missing.append(key)

    if not missing:
        return

    try:
        import psycopg
        from psycopg.rows import dict_row

        cache_keys = [_cache_key(k) for k in missing]
        placeholders = ",".join(["%s"] * len(cache_keys))
        sql = (
            "SELECT cache_key,created_at,payload FROM mlb_api_cache "
            f"WHERE bucket=%s AND cache_key IN ({placeholders})"
        )

        with psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10) as c:
            rows = c.execute(sql, (bucket, *cache_keys)).fetchall()

        key_by_serialized = {_cache_key(k): k for k in missing}
        with _mlb_cache_lock:
            for row in rows:
                if now - float(row["created_at"]) > ttl:
                    continue
                original_key = key_by_serialized.get(row["cache_key"])
                if original_key is None:
                    continue
                try:
                    value = json.loads(row["payload"])
                except Exception:
                    continue
                _mlb_cache[bucket][original_key] = (now, value)
    except Exception:
        return


def fetch_props():
    """
    Fetch only the MLB markets this model actually uses.

    Important: ParlayAPI's documented /props endpoint supports `limit` but does
    not document offset pagination. The previous loop kept sending `offset`
    whenever exactly 10,000 rows were returned. If the API ignored that
    unsupported parameter, the same 10,000-row page could be fetched repeatedly,
    leaving the dashboard stuck in REFRESHING until the worker died/restarted.

    We now make one bounded request. If it actually hits the 10,000-row ceiling,
    retry in two smaller market batches and merge them client-side.
    """
    if not PARLAY_API_KEY:
        raise RuntimeError("PARLAY_API_KEY is not set")

    rows = _fetch_prop_batch(_MLB_MODEL_MARKETS)
    if len(rows) < 10000:
        return rows

    # Safety path for an unusually large slate. Split by market instead of using
    # unsupported offset pagination.
    midpoint = (len(_MLB_MODEL_MARKETS) + 1) // 2
    batches = [
        _MLB_MODEL_MARKETS[:midpoint],
        _MLB_MODEL_MARKETS[midpoint:],
    ]

    merged = []
    seen = set()
    for markets in batches:
        for row in _fetch_prop_batch(markets):
            key = (
                row.get("canonical_event_id") or row.get("event_id"),
                row.get("bookmaker") or row.get("source"),
                row.get("player") or row.get("player_name"),
                row.get("market_key"),
                row.get("line"),
                row.get("last_update") or row.get("lastUpdate"),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)

    return merged


def search_player(name):
    key = " ".join(str(name or "").lower().replace(".", "").split())
    cached = _cache_get("player_search", key, PLAYER_SEARCH_CACHE_SECONDS)
    if cached is not None:
        return cached

    r = requests.get(
        f"{MLB_BASE}/people/search",
        params={"names": name, "active": "true", "sportIds": 1, "hydrate": "currentTeam"},
        timeout=20,
    )
    r.raise_for_status()
    people = r.json().get("people", [])
    if not people:
        return _cache_set("player_search", key, None)

    target = key
    def norm(s):
        return " ".join((s or "").lower().replace(".", "").split())

    for p in people:
        if norm(p.get("fullName")) == target:
            return _cache_set("player_search", key, p)

    return _cache_set("player_search", key, people[0])

def fetch_person(person_id):
    key = int(person_id)
    cached = _cache_get("person", key, PERSON_CACHE_SECONDS)
    if cached is not None:
        return cached

    r = requests.get(
        f"{MLB_BASE}/people/{person_id}",
        params={"hydrate": "currentTeam"},
        timeout=20,
    )
    r.raise_for_status()
    people = r.json().get("people", [])
    value = people[0] if people else None
    return _cache_set("person", key, value)

def fetch_game_log(player_id, season, group):
    key = (int(player_id), int(season), str(group))
    cached = _cache_get("game_log", key, GAME_LOG_CACHE_SECONDS)
    if cached is not None:
        return cached

    r = requests.get(
        f"{MLB_BASE}/people/{player_id}/stats",
        params={"stats": "gameLog", "group": group, "season": season},
        timeout=30,
    )
    r.raise_for_status()
    stats = r.json().get("stats", [])
    value = stats[0].get("splits", []) if stats else []
    return _cache_set("game_log", key, value)

def fetch_team_schedule(team_id, target_date=None):
    target_date = target_date or date.today().isoformat()
    key = (int(team_id), str(target_date))
    cached = _cache_get("team_schedule", key, TEAM_SCHEDULE_CACHE_SECONDS)
    if cached is not None:
        return cached

    r = requests.get(
        f"{MLB_BASE}/schedule",
        params={
            "sportId": 1,
            "teamId": team_id,
            "date": target_date,
            "hydrate": "probablePitcher,venue",
        },
        timeout=25,
    )
    r.raise_for_status()
    dates = r.json().get("dates", [])
    value = dates[0].get("games", []) if dates else []
    return _cache_set("team_schedule", key, value)

def fetch_game_feed(game_pk):
    key = int(game_pk)
    cached = _cache_get("game_feed", key, GAME_FEED_CACHE_SECONDS)
    if cached is not None:
        return cached

    r = requests.get(
        f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live",
        timeout=30,
    )
    r.raise_for_status()
    return _cache_set("game_feed", key, r.json())

def get_player_context(person, target_date=None):
    team = person.get("currentTeam") or {}
    team_id = team.get("id")
    player_team = team.get("name")
    player_hand = (person.get("batSide") or {}).get("code") or (person.get("pitchHand") or {}).get("code")
    ctx = {
        "player_team": player_team,
        "player_hand": player_hand,
        "opponent": None,
        "probable_pitcher": None,
        "probable_pitcher_id": None,
        "pitcher_hand": None,
        "venue": None,
        "game_pk": None,
        "lineup_status": "UNKNOWN",
        "is_probable_starter": False,
    }
    if not team_id:
        return ctx
    games = fetch_team_schedule(team_id, target_date)
    if not games:
        return ctx
    game = games[0]
    ctx["game_pk"] = game.get("gamePk")
    ctx["venue"] = (game.get("venue") or {}).get("name")
    teams = game.get("teams", {})
    home = (teams.get("home") or {}).get("team") or {}
    away = (teams.get("away") or {}).get("team") or {}
    is_home = home.get("id") == team_id
    ctx["opponent"] = (away if is_home else home).get("name")

    own_side = teams.get("home") if is_home else teams.get("away")
    own_pp = (own_side or {}).get("probablePitcher") or {}
    ctx["is_probable_starter"] = own_pp.get("id") == person.get("id")

    opp_side = teams.get("away") if is_home else teams.get("home")
    pp = (opp_side or {}).get("probablePitcher") or {}
    if pp.get("id"):
        ctx["probable_pitcher_id"] = pp.get("id")
        ctx["probable_pitcher"] = pp.get("fullName")
        try:
            pitcher = fetch_person(pp.get("id"))
            ctx["pitcher_hand"] = ((pitcher or {}).get("pitchHand") or {}).get("code")
        except Exception:
            pass

    try:
        if ctx["game_pk"]:
            feed = fetch_game_feed(ctx["game_pk"])
            box = ((feed.get("liveData") or {}).get("boxscore") or {}).get("teams") or {}
            team_box = box.get("home" if is_home else "away") or {}
            order = team_box.get("battingOrder") or []
            if order:
                ctx["lineup_status"] = "STARTING" if person.get("id") in order else "NOT_STARTING"
            else:
                ctx["lineup_status"] = "UNCONFIRMED"
    except Exception:
        ctx["lineup_status"] = "UNCONFIRMED"
    return ctx


def fetch_pitcher_season_stats(player_id, season):
    key = (int(player_id), int(season))
    cached = _cache_get("pitcher_stats", key, PITCHER_STATS_CACHE_SECONDS)
    if cached is not None:
        return cached

    r = requests.get(
        f"{MLB_BASE}/people/{player_id}/stats",
        params={"stats": "season", "group": "pitching", "season": season},
        timeout=25,
    )
    r.raise_for_status()
    stats = r.json().get("stats", [])
    if not stats or not stats[0].get("splits"):
        return _cache_set("pitcher_stats", key, {})
    return _cache_set("pitcher_stats", key, stats[0]["splits"][0].get("stat", {}))

def fetch_kalshi_markets():
    """
    Fetch Kalshi's dedicated MLB prediction-market contracts.

    Prediction markets are binary YES/NO contracts, so they are intentionally
    kept separate from the player-prop Over/Under normalization.
    """
    if not PARLAY_API_KEY:
        raise RuntimeError("PARLAY_API_KEY is not set")

    r = requests.get(
        f"{PARLAY_BASE}/prediction-markets/{SPORT_KEY}",
        headers={"X-API-Key": PARLAY_API_KEY},
        timeout=45,
    )
    if not r.ok:
        raise RuntimeError(
            f"ParlayAPI Kalshi request failed ({r.status_code}): {r.text[:500]}"
        )

    payload = r.json()
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("markets") or payload.get("results") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    return [
        row for row in rows
        if str(row.get("source") or row.get("bookmaker") or "").lower() == "kalshi"
    ]


_scoreboard_cache = {"key": None, "time": 0.0, "games": []}
_standings_cache = {"season": None, "time": 0.0, "records": {}}

def _team_logo_url(team_id):
    if not team_id:
        return None
    return f"https://www.mlbstatic.com/team-logos/{team_id}.svg"

def _standings_records(season):
    now = time.time()
    if (
        _standings_cache["season"] == season
        and now - _standings_cache["time"] < STANDINGS_CACHE_SECONDS
    ):
        return _standings_cache["records"]

    records = {}
    try:
        r = requests.get(
            f"{MLB_BASE}/standings",
            params={
                "leagueId": "103,104",
                "season": season,
                "standingsTypes": "regularSeason",
                "hydrate": "team",
            },
            timeout=25,
        )
        r.raise_for_status()
        for block in r.json().get("records", []):
            for tr in block.get("teamRecords", []):
                team = tr.get("team") or {}
                tid = team.get("id")
                if tid:
                    records[int(tid)] = {
                        "wins": tr.get("wins"),
                        "losses": tr.get("losses"),
                        "pct": tr.get("winningPercentage"),
                    }
    except Exception:
        # The scoreboard still works without records if standings briefly fail.
        records = _standings_cache.get("records") or {}

    _standings_cache.update({
        "season": season,
        "time": now,
        "records": records,
    })
    return records

def fetch_mlb_scoreboard(target_date=None):
    """
    Today's MLB schedule with live scores, game state, records, logos,
    probable pitchers and inning detail.

    Cached briefly so browser polling does not hammer the MLB Stats API.
    """
    target_date = target_date or date.today().isoformat()
    cache_key = str(target_date)
    now = time.time()

    if (
        _scoreboard_cache["key"] == cache_key
        and now - _scoreboard_cache["time"] < SCOREBOARD_CACHE_SECONDS
    ):
        return _scoreboard_cache["games"]

    season = int(str(target_date)[:4])
    records = _standings_records(season)

    r = requests.get(
        f"{MLB_BASE}/schedule",
        params={
            "sportId": 1,
            "date": target_date,
            "hydrate": "linescore,probablePitcher,venue,team",
        },
        timeout=30,
    )
    r.raise_for_status()

    games = []
    for d in r.json().get("dates", []):
        for g in d.get("games", []):
            status = g.get("status") or {}
            teams = g.get("teams") or {}
            home_side = teams.get("home") or {}
            away_side = teams.get("away") or {}
            home = home_side.get("team") or {}
            away = away_side.get("team") or {}
            home_id = home.get("id")
            away_id = away.get("id")

            linescore = g.get("linescore") or {}
            inning = linescore.get("currentInningOrdinal")
            inning_state = linescore.get("inningState")
            offense = linescore.get("offense") or {}
            defense = linescore.get("defense") or {}

            home_rec = records.get(int(home_id)) if home_id else None
            away_rec = records.get(int(away_id)) if away_id else None

            home_pp = (home_side.get("probablePitcher") or {})
            away_pp = (away_side.get("probablePitcher") or {})

            # Sanity-check MLB status against the official scheduled first pitch.
            # Occasionally hydrated schedule data can carry an early/stale LIVE
            # state. Never consider a game live before its scheduled gameDate.
            game_date_raw = g.get("gameDate")
            effective_abstract_state = status.get("abstractGameState")
            effective_detailed_state = status.get("detailedState")
            effective_linescore = linescore

            scheduled_dt = None
            if game_date_raw:
                try:
                    scheduled_dt = datetime.fromisoformat(
                        str(game_date_raw).replace("Z", "+00:00")
                    )
                    if scheduled_dt.tzinfo is None:
                        scheduled_dt = scheduled_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    scheduled_dt = None

            now_utc = datetime.now(timezone.utc)
            state_lower = str(effective_abstract_state or "").lower()

            # Five-minute tolerance allows legitimate "Warmup" / just-started
            # transitions without declaring a game live 20-30 minutes early.
            if (
                scheduled_dt is not None
                and scheduled_dt > now_utc + timedelta(minutes=5)
                and state_lower == "live"
            ):
                effective_abstract_state = "Preview"
                effective_detailed_state = "Scheduled"
                effective_linescore = {}

            games.append({
                "game_pk": g.get("gamePk"),
                "game_date": g.get("gameDate"),
                "official_date": g.get("officialDate") or target_date,
                "abstract_state": effective_abstract_state,
                "detailed_state": effective_detailed_state,
                "coded_state": status.get("codedGameState"),
                "status_code": status.get("statusCode"),
                "home_team": home.get("name"),
                "home_abbr": home.get("abbreviation"),
                "home_team_id": home_id,
                "home_logo": _team_logo_url(home_id),
                "home_score": home_side.get("score"),
                "home_record": home_rec,
                "away_team": away.get("name"),
                "away_abbr": away.get("abbreviation"),
                "away_team_id": away_id,
                "away_logo": _team_logo_url(away_id),
                "away_score": away_side.get("score"),
                "away_record": away_rec,
                "venue": (g.get("venue") or {}).get("name"),
                "inning": (
                    effective_linescore.get("currentInningOrdinal")
                    if effective_linescore else None
                ),
                "inning_state": (
                    effective_linescore.get("inningState")
                    if effective_linescore else None
                ),
                "balls": effective_linescore.get("balls") if effective_linescore else None,
                "strikes": effective_linescore.get("strikes") if effective_linescore else None,
                "outs": effective_linescore.get("outs") if effective_linescore else None,
                "home_probable_pitcher": home_pp.get("fullName"),
                "away_probable_pitcher": away_pp.get("fullName"),
            })

    _scoreboard_cache.update({
        "key": cache_key,
        "time": now,
        "games": games,
    })
    return games
