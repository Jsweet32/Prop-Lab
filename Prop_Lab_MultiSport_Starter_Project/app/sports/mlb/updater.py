from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from statistics import median
import json

from .providers import fetch_props, search_player, fetch_game_log, get_player_context, fetch_mlb_scoreboard
from .model import (
    SUPPORTED, canonical_market_key, american_implied, rates_from_game_log,
    weighted_probability, dfs_model_edge, sportsbook_grade
)
from .statcast import fetch_statcast_leaderboard
from .db import replace_snapshot, latest_rows, latest_snapshot
from .history import capture_actionable_props
from .config import *

_LOCAL_DFS_SIDE_POLICIES = {
    "PrizePicks": {
        "player_home_runs": "OVER_ONLY",
        "batter_home_runs": "OVER_ONLY",
    },
    "Underdog": {
        "player_home_runs": "OVER_ONLY",
        "batter_home_runs": "OVER_ONLY",
    },
}

def parse_dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z","+00:00"))
    except Exception:
        return None

def prop_age(p):
    for k in ("age_seconds","ageSeconds","age"):
        if p.get(k) is not None:
            try:
                return float(p[k])
            except Exception:
                pass
    last = parse_dt(p.get("last_update") or p.get("lastUpdate") or p.get("updated_at"))
    if last:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return max(0,(datetime.now(timezone.utc)-last.astimezone(timezone.utc)).total_seconds())
    return None

def implied(p, side):
    v = p.get(f"{side}_implied_prob")
    if v is not None:
        try:
            v = float(v)
            return v/100 if v > 1 else v
        except Exception:
            pass
    return american_implied(p.get(f"{side}_price"))

def dfs_grade(edge, rates):
    if edge is None or not rates:
        return "", "NEEDS DATA"
    ns = int(rates.get("games_season") or 0)
    recent = max(int(rates.get("games_l30") or 0), int(rates.get("games_l10") or 0))
    if ns < DFS_MIN_SEASON_GAMES or recent < DFS_MIN_RECENT_GAMES:
        return "", "WAIT"
    if edge >= DFS_A_EDGE:
        return "A", "GOOD"
    if edge >= DFS_GOOD_EDGE:
        return "B", "GOOD"
    if edge >= DFS_BORDERLINE_EDGE:
        return "C", "BORDERLINE"
    return "PASS", "PASS"

def available_sides(book_title, market_key, prop):
    policies = globals().get("DFS_SIDE_POLICIES", _LOCAL_DFS_SIDE_POLICIES)
    if book_title in DFS_BOOKS:
        policy = (policies.get(book_title) or {}).get(market_key, "BOTH")
        if policy == "OVER_ONLY":
            return ("OVER",)
        if policy == "UNDER_ONLY":
            return ("UNDER",)
        return ("OVER","UNDER")

    sides = []
    if prop.get("over_price") is not None or prop.get("over_implied_prob") is not None:
        sides.append("OVER")
    if prop.get("under_price") is not None or prop.get("under_implied_prob") is not None:
        sides.append("UNDER")
    return tuple(sides)

def _float_line(p):
    try:
        return float(p.get("line"))
    except Exception:
        return None

def _normalized_team(v):
    return " ".join(str(v or "").lower().replace(".", "").split())

def _game_key(p):
    """
    Cross-book game key that does NOT depend on provider event IDs.
    Provider event IDs can differ between PrizePicks, Underdog and Fliff.
    """
    home = _normalized_team(p.get("home_team"))
    away = _normalized_team(p.get("away_team"))

    start = p.get("_start") or parse_dt(
        p.get("commence_time") or p.get("commenceTime") or p.get("start_time")
    )
    day = ""
    if start:
        try:
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            day = start.astimezone(timezone.utc).date().isoformat()
        except Exception:
            day = ""

    if home or away:
        return (day, home, away)
    return (day,)

def _market_consensus_key(p):
    # Player + normalized market + actual matchup/date.
    return (
        _game_key(p),
        " ".join(str(p.get("player") or "").lower().split()),
        p.get("_model_key") or p.get("market_key"),
    )

def _dfs_variant(p):
    """
    Conservative PrizePicks variant detection.
    Only explicit variant/type fields are examined.
    """
    candidate_fields = (
        "odds_type", "oddsType",
        "projection_type", "projectionType",
        "pick_type", "pickType",
        "line_type", "lineType",
        "variant",
        "offer_type", "offerType",
        "tier",
        "promotion_type", "promotionType",
    )

    values = []
    for k in candidate_fields:
        v = p.get(k)
        if isinstance(v, str):
            values.append(v.strip().lower())

    raw = p.get("raw_json")
    if isinstance(raw, str) and raw.strip():
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                for k in candidate_fields:
                    v = obj.get(k)
                    if isinstance(v, str):
                        values.append(v.strip().lower())
        except Exception:
            pass
    elif isinstance(raw, dict):
        for k in candidate_fields:
            v = raw.get(k)
            if isinstance(v, str):
                values.append(v.strip().lower())

    for v in values:
        if "demon" in v:
            return "DEMON"
        if "goblin" in v:
            return "GOBLIN"
        if v in {"standard", "normal", "regular", "main"}:
            return "STANDARD"
    return "UNKNOWN"

def _middle_line(rows):
    pairs = [(r, _float_line(r)) for r in rows if _float_line(r) is not None]
    if not pairs:
        return rows[0] if rows else None
    pairs.sort(key=lambda x: x[1])
    return pairs[(len(pairs)-1)//2][0]

def _freshest(rows):
    return min(
        rows,
        key=lambda r: prop_age(r) if prop_age(r) is not None else 999999
    )

def select_prizepicks_main_lines(prizepicks_rows, comparison_rows):
    """
    PrizePicks-only standard-line selection.

    Underdog/Fliff/Kalshi NEVER enter this filter.

    Order:
    1. Explicit STANDARD row.
    2. Remove explicit DEMON/GOBLIN rows.
    3. Choose candidate closest to Underdog/Fliff consensus for the same
       player/market/matchup (provider event IDs are intentionally ignored).
    4. If no comparison is available and only one normal candidate exists, use it.
    5. If several unlabeled candidates remain without consensus, choose the
       central line rather than an extreme alternate.

    Explicit demon/goblin rows are never selected as the main projection.
    """
    comparison = {}
    for p in comparison_rows:
        if p["bookmaker_title"] not in {"Underdog", "Fliff"}:
            continue
        ln = _float_line(p)
        if ln is None:
            continue
        comparison.setdefault(_market_consensus_key(p), []).append(ln)

    groups = {}
    for p in prizepicks_rows:
        groups.setdefault(_market_consensus_key(p), []).append(p)

    selected = []

    for key, group in groups.items():
        tagged = [(r, _dfs_variant(r)) for r in group]

        standards = [r for r, variant in tagged if variant == "STANDARD"]
        if standards:
            row = _freshest(standards)
            row["_pp_variant"] = "STANDARD"
            selected.append(row)
            continue

        normal = [r for r, variant in tagged if variant not in {"DEMON", "GOBLIN"}]
        if not normal:
            # Do not substitute a known alternate for the standard line.
            continue

        consensus_values = comparison.get(key, [])
        if consensus_values:
            target = median(consensus_values)
            with_lines = [r for r in normal if _float_line(r) is not None]
            if with_lines:
                row = min(
                    with_lines,
                    key=lambda r: (
                        abs(_float_line(r) - target),
                        prop_age(r) if prop_age(r) is not None else 999999,
                    )
                )
            else:
                row = _freshest(normal)
        elif len(normal) == 1:
            row = normal[0]
        else:
            row = _middle_line(normal)

        if row is not None:
            row["_pp_variant"] = _dfs_variant(row)
            selected.append(row)

    return selected


def select_underdog_main_lines(underdog_rows, comparison_rows):
    """
    Keep one likely main Underdog projection per game/player/market.
    """
    comparison = {}
    for p in comparison_rows:
        if p["bookmaker_title"] not in {"PrizePicks", "Fliff"}:
            continue
        ln = _float_line(p)
        if ln is None:
            continue
        comparison.setdefault(_market_consensus_key(p), []).append(ln)

    groups = {}
    for p in underdog_rows:
        groups.setdefault(_market_consensus_key(p), []).append(p)

    selected = []

    for key, group in groups.items():
        if len(group) == 1:
            selected.append(group[0])
            continue

        by_line = {}
        for row in group:
            ln = _float_line(row)
            if ln is None:
                continue
            prior = by_line.get(ln)
            if prior is None or (row.get("_age") or 999999) < (prior.get("_age") or 999999):
                by_line[ln] = row

        candidates = list(by_line.values()) or group
        consensus_values = comparison.get(key, [])

        if consensus_values:
            target = median(consensus_values)
            with_lines = [r for r in candidates if _float_line(r) is not None]
            if with_lines:
                row = min(
                    with_lines,
                    key=lambda r: (
                        abs(_float_line(r) - target),
                        prop_age(r) if prop_age(r) is not None else 999999,
                    )
                )
            else:
                row = _freshest(candidates)
        else:
            row = _middle_line(candidates)

        if row is not None:
            selected.append(row)

    return selected

def _book_max_age(book_title):
    if book_title == "PrizePicks":
        return globals().get("PRIZEPICKS_MAX_AGE_SECONDS", LINE_MAX_AGE_SECONDS)
    if book_title == "Underdog":
        return globals().get("UNDERDOG_MAX_AGE_SECONDS", LINE_MAX_AGE_SECONDS)
    if book_title == "Fliff":
        return globals().get("FLIFF_MAX_AGE_SECONDS", LINE_MAX_AGE_SECONDS)
    if book_title == "Kalshi":
        return globals().get("KALSHI_MAX_AGE_SECONDS", LINE_MAX_AGE_SECONDS)
    return LINE_MAX_AGE_SECONDS


def _norm_team_name(v):
    return " ".join(str(v or "").lower().replace(".", "").split())

def _scoreboard_lookup(games):
    lookup = {}
    for g in games:
        home = _norm_team_name(g.get("home_team"))
        away = _norm_team_name(g.get("away_team"))
        day = str(g.get("official_date") or "")
        lookup[(day, home, away)] = g
        # Also tolerate API/provider home-away reversal mistakes.
        lookup[(day, away, home)] = g
    return lookup

def _prop_game_status(prop, lookup, tz):
    start = prop.get("_start")
    if not start:
        return None
    try:
        local_start = start.astimezone(tz)
        day = local_start.date().isoformat()
    except Exception:
        return None

    home = _norm_team_name(prop.get("home_team"))
    away = _norm_team_name(prop.get("away_team"))
    if not home and not away:
        return None
    return lookup.get((day, home, away))

def refresh_all(fast=False):
    raw = fetch_props()
    tz = ZoneInfo(TIMEZONE)
    now_local = datetime.now(tz)
    cutoff = now_local + timedelta(hours=UPCOMING_HOURS)

    # MLB game status is the authority for whether props are still actionable.
    scoreboard_games = []
    if globals().get("EXCLUDE_STARTED_GAME_PROPS", True):
        dates_to_check = {
            now_local.date().isoformat(),
            cutoff.date().isoformat(),
        }
        for d in dates_to_check:
            try:
                scoreboard_games.extend(fetch_mlb_scoreboard(d))
            except Exception:
                pass
    scoreboard_lookup = _scoreboard_lookup(scoreboard_games)

    props = []
    for p in raw:
        book = (p.get("bookmaker") or p.get("source") or "").lower()
        if book not in BOOK_TITLES:
            continue

        book_title = BOOK_TITLES[book]
        player = p.get("player") or p.get("player_name") or p.get("participant")
        if not player:
            continue

        age = prop_age(p)
        if age is not None and age > _book_max_age(book_title):
            continue

        start = parse_dt(p.get("commence_time") or p.get("commenceTime") or p.get("start_time"))
        if start:
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            local = start.astimezone(tz)
            if local < now_local - timedelta(hours=1) or local > cutoff:
                continue

        # Do not model stale lines from games that have already begun.
        if globals().get("EXCLUDE_STARTED_GAME_PROPS", True) and start:
            temp_prop = {
                **p,
                "home_team": p.get("home_team"),
                "away_team": p.get("away_team"),
                "_start": start,
            }
            game = _prop_game_status(temp_prop, scoreboard_lookup, tz)
            if game:
                state = str(game.get("abstract_state") or "").lower()
                detailed = str(game.get("detailed_state") or "").lower()
                if state in {"live", "final"}:
                    continue
                if any(x in detailed for x in ("in progress", "game over", "final")):
                    continue
            else:
                # Conservative fallback if a matchup cannot be resolved:
                # lines well after scheduled first pitch are no longer actionable.
                if start.astimezone(tz) < now_local - timedelta(minutes=10):
                    continue

        label = p.get("market") or p.get("market_label") or p.get("market_key") or p.get("marketType") or ""
        model_key = canonical_market_key(
            p.get("market_key") or p.get("marketType") or "",
            label
        )

        props.append({
            **p,
            "bookmaker": book,
            "bookmaker_title": book_title,
            "_source_book": book,
            "player": player,
            "market": label,
            "_model_key": model_key,
            "market_key": model_key or "SPECIALIZED",
            "_age": age,
            "_last_update": p.get("last_update") or p.get("lastUpdate") or p.get("updated_at"),
            "_start": start,
        })

    # Exact-row de-duplication only. This does NOT collapse different lines.
    dedup = {}
    for p in props:
        dk = (
            p.get("_source_book"),
            _game_key(p),
            p["player"],
            p.get("_model_key"),
            p.get("market"),
            p.get("line"),
        )
        previous = dedup.get(dk)
        if previous is None or (p.get("_age") or 999999) < (previous.get("_age") or 999999):
            dedup[dk] = p
    props = list(dedup.values())

    # Isolate each DFS book's alternate-line cleanup.
    prizepicks_rows = [p for p in props if p["bookmaker_title"] == "PrizePicks"]
    underdog_rows = [p for p in props if p["bookmaker_title"] == "Underdog"]
    fliff_kalshi_rows = [
        p for p in props
        if p["bookmaker_title"] not in {"PrizePicks", "Underdog"}
    ]

    prizepicks_main = select_prizepicks_main_lines(
        prizepicks_rows,
        underdog_rows + fliff_kalshi_rows,
    )

    underdog_main = select_underdog_main_lines(
        underdog_rows,
        prizepicks_main + fliff_kalshi_rows,
    )

    props = fliff_kalshi_rows + prizepicks_main + underdog_main

    now = datetime.now(timezone.utc)
    snapshot = now.isoformat()
    season = now.year
    people, logs, contexts = {}, {}, {}

    # Manual refresh is line-first: reuse the latest fully modeled result when
    # the player/market/line/matchup has not changed. This avoids hundreds of
    # MLB Stats API calls after a Render process restart.
    prior_by_exact = {}
    if fast:
        try:
            for old in latest_rows():
                exact_key = (
                    old.get("bookmaker_title"),
                    " ".join(str(old.get("player") or "").lower().split()),
                    old.get("market_key"),
                    float(old.get("line")) if old.get("line") is not None else None,
                    _norm_team_name(old.get("home_team")),
                    _norm_team_name(old.get("away_team")),
                )
                prior_by_exact[exact_key] = old
        except Exception:
            prior_by_exact = {}

    if fast:
        statcast = {}
    else:
        try:
            statcast = fetch_statcast_leaderboard(season)
        except Exception:
            statcast = {}

    rows = []
    for p in props:
        player, key, line = p["player"], p.get("_model_key"), p.get("line")
        mlb_id = None
        rates = None
        base = None
        model_over = None
        ctx = {}
        note = ""
        lineup = "UNKNOWN"

        prior = None
        if fast:
            try:
                prior_key = (
                    p.get("bookmaker_title"),
                    " ".join(str(player or "").lower().split()),
                    key or "SPECIALIZED",
                    float(line) if line is not None else None,
                    _norm_team_name(p.get("home_team")),
                    _norm_team_name(p.get("away_team")),
                )
                prior = prior_by_exact.get(prior_key)
            except Exception:
                prior = None

        # Exact line reuse is safe for the model because rates/probabilities are
        # line-dependent. Price/age/source fields are still refreshed below.
        if prior:
            mlb_id = prior.get("mlb_player_id")
            rates = {
                "season_rate": prior.get("season_rate"),
                "l30_rate": prior.get("l30_rate"),
                "l10_rate": prior.get("l10_rate"),
                "games_season": prior.get("games_season"),
                "games_l30": prior.get("games_l30"),
                "games_l10": prior.get("games_l10"),
            }
            base = prior.get("base_prob_over")
            model_over = prior.get("model_prob_over")
            ctx = {
                "player_team": prior.get("player_team"),
                "opponent": prior.get("opponent"),
                "probable_pitcher": prior.get("probable_pitcher"),
                "venue": prior.get("venue"),
                "game_pk": prior.get("game_pk"),
                "lineup_status": prior.get("lineup_status"),
                "is_probable_starter": prior.get("lineup_status") == "PROBABLE_STARTER",
            }
            lineup = prior.get("lineup_status") or "UNKNOWN"

        if prior is None and player not in people:
            try:
                people[player] = search_player(player)
            except Exception:
                people[player] = None
        person = people.get(player)
        if prior is None and person:
            mlb_id = person.get("id")

        market_def = SUPPORTED.get(key) if key else None

        if prior is not None:
            note = "Fast refresh: reused latest model/context"
        elif key is None:
            note = "Specialized/inning market excluded from full-game model"
        elif line is None:
            note = "Missing line"
        elif not market_def:
            note = f"Unsupported market: {key}"
        elif not mlb_id:
            note = "MLB player match failed"
        else:
            group = market_def[0]
            stat_key = market_def[1]
            ck = (mlb_id, group)
            if ck not in logs:
                try:
                    logs[ck] = fetch_game_log(mlb_id, season, group)
                except Exception:
                    logs[ck] = []

            if not logs[ck]:
                note = "No MLB game-log data"
            else:
                rates = rates_from_game_log(logs[ck], stat_key, line)
                base = weighted_probability(rates)
                model_over = base

            target_date = (
                p["_start"].astimezone(tz).date().isoformat()
                if p.get("_start") else now_local.date().isoformat()
            )
            ckey = (mlb_id, target_date)
            if ckey not in contexts:
                try:
                    contexts[ckey] = get_player_context(person, target_date)
                except Exception:
                    contexts[ckey] = {}
            ctx = contexts[ckey]
            lineup = ctx.get("lineup_status") or "UNKNOWN"

        model_under = None if model_over is None else 1-model_over
        over_imp = implied(p, "over")
        under_imp = implied(p, "under")
        sides = available_sides(p["bookmaker_title"], key, p)

        # Final hard safety gate for PrizePicks alternates.
        if p["bookmaker_title"] == "PrizePicks" and p.get("_pp_variant") in {"DEMON", "GOBLIN"}:
            sides = ()

        rec_side = rec_prob = None
        display_edge = None
        edge_type = None
        grade = ""
        verdict = "NEEDS DATA"

        if key is None:
            verdict = "NO ACTION"
        elif not sides:
            verdict = "NO ACTION"
            if p["bookmaker_title"] == "PrizePicks" and p.get("_pp_variant") in {"DEMON", "GOBLIN"}:
                note = "PrizePicks alternate line excluded"
            else:
                note = note or "No selectable side found"
        elif model_over is not None:
            if p["bookmaker_title"] in DFS_BOOKS:
                candidates = []
                if "OVER" in sides:
                    candidates.append(("OVER", model_over))
                if "UNDER" in sides:
                    candidates.append(("UNDER", model_under))
                if candidates:
                    rec_side, rec_prob = max(candidates, key=lambda x:x[1])
                    display_edge = dfs_model_edge(rec_prob)
                    edge_type = "MODEL EDGE"
                    grade, verdict = dfs_grade(display_edge, rates)
            else:
                candidates = []
                if "OVER" in sides and over_imp is not None:
                    candidates.append(("OVER", model_over, model_over-over_imp))
                if "UNDER" in sides and under_imp is not None:
                    candidates.append(("UNDER", model_under, model_under-under_imp))
                if candidates:
                    rec_side, rec_prob, display_edge = max(candidates, key=lambda x:x[2])
                    edge_type = "TRUE EDGE"
                    grade, verdict = sportsbook_grade(display_edge)

        group = market_def[0] if market_def else None
        if group == "hitting":
            if lineup == "NOT_STARTING":
                rec_side = None
                grade = ""
                verdict = "NO ACTION"
                note = "Not in confirmed starting lineup"
            elif lineup in {"UNKNOWN","UNCONFIRMED"} and verdict in {"GOOD","BORDERLINE"}:
                verdict = "PROVISIONAL"
                note = note or "Lineup not confirmed yet"
        elif group == "pitching":
            if ctx and ctx.get("is_probable_starter"):
                lineup = "PROBABLE_STARTER"
            elif verdict in {"GOOD","BORDERLINE"}:
                rec_side = None
                grade = ""
                verdict = "NO ACTION"
                note = "Not today's probable starting pitcher"

        if prior is not None:
            sc = {
                "xba": prior.get("xba"),
                "xslg": prior.get("xslg"),
                "xwoba": prior.get("xwoba"),
                "hard_hit_pct": prior.get("hard_hit_pct"),
                "barrel_pct": prior.get("barrel_pct"),
                "whiff_pct": prior.get("whiff_pct"),
            }
        else:
            sc = statcast.get(mlb_id,{}) if mlb_id else {}
        side_text = "/".join(sides) if sides else "NONE"

        if p["bookmaker_title"] == "PrizePicks":
            ppv = p.get("_pp_variant") or "UNKNOWN"
            extra = f"PrizePicks line: {ppv}; Available: {side_text}"
        else:
            extra = f"Available: {side_text}"
        note = f"{note} | {extra}" if note else extra

        rows.append({
            "snapshot_time": snapshot,
            "bookmaker": p["bookmaker"],
            "bookmaker_title": p["bookmaker_title"],
            "player": player,
            "market_key": key or "SPECIALIZED",
            "market": p["market"],
            "line": line,
            "over_price": p.get("over_price"),
            "under_price": p.get("under_price"),
            "home_team": p.get("home_team"),
            "away_team": p.get("away_team"),
            "commence_time": p.get("commence_time") or p.get("commenceTime"),
            "game_pk": ctx.get("game_pk"),
            "model_prob_over": model_over,
            "model_prob_under": model_under,
            "base_prob_over": base,
            "implied_prob_over": over_imp,
            "implied_prob_under": under_imp,
            "recommended_side": rec_side,
            "recommended_prob": rec_prob,
            "display_edge": display_edge,
            "edge_type": edge_type,
            "grade": grade,
            "verdict": verdict,
            "season_rate": rates.get("season_rate") if rates else None,
            "l30_rate": rates.get("l30_rate") if rates else None,
            "l10_rate": rates.get("l10_rate") if rates else None,
            "games_season": rates.get("games_season") if rates else None,
            "games_l30": rates.get("games_l30") if rates else None,
            "games_l10": rates.get("games_l10") if rates else None,
            "mlb_player_id": mlb_id,
            "player_team": ctx.get("player_team"),
            "opponent": ctx.get("opponent"),
            "probable_pitcher": ctx.get("probable_pitcher"),
            "venue": ctx.get("venue"),
            "lineup_status": lineup,
            "line_age_seconds": p.get("_age"),
            "line_last_update": p.get("_last_update"),
            "xba": sc.get("xba"),
            "xslg": sc.get("xslg"),
            "xwoba": sc.get("xwoba"),
            "hard_hit_pct": sc.get("hard_hit_pct"),
            "barrel_pct": sc.get("barrel_pct"),
            "whiff_pct": sc.get("whiff_pct"),
            "status_note": note,
        })

    if not rows:
        raise RuntimeError("Refresh produced zero usable prop rows; previous snapshot preserved.")

    replace_snapshot(rows, snapshot)

    # Freeze A/B GOOD + PROVISIONAL recommendations for honest post-game tracking.
    # History failure must never break the live prop board.
    try:
        capture_actionable_props(rows, snapshot)
    except Exception:
        pass

    return {
        "snapshot_time": snapshot,
        "rows": len(rows),
        "players": len({p["player"] for p in props}),
    }


def refresh_fast():
    """Fast manual line refresh used by the dashboard Refresh now button."""
    return refresh_all(fast=True)
