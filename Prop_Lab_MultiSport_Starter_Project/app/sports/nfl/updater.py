from datetime import datetime, timezone, timedelta
from statistics import median

from .config import DFS_BOOKS, LINE_MAX_AGE_SECONDS, UPCOMING_HOURS
from .db import replace_snapshot
from .history import capture_actionable_props
from .model import (
    SUPPORTED, canonical_market_key, american_implied,
    build_history_metrics, grade
)
from .providers import (
    fetch_props, player_history, player_profile, game_context, book_title, nfl_season
)

def _dt(v):
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def _age(row):
    for k in ("age_seconds", "ageSeconds", "age"):
        if row.get(k) is not None:
            try:
                return float(row[k])
            except Exception:
                pass
    d = _dt(row.get("last_update") or row.get("lastUpdate") or row.get("snapshot_time"))
    if not d:
        return None
    return max(0.0, (datetime.now(timezone.utc) - d.astimezone(timezone.utc)).total_seconds())

def _source_key(row):
    return str(row.get("bookmaker") or row.get("source") or "").lower()

def _player(row):
    return row.get("player") or row.get("player_name") or row.get("outcome_name")

def _line(row):
    try:
        return float(row.get("line"))
    except Exception:
        return None

def _normalize_rows(raw):
    rows = []
    now = datetime.now(timezone.utc)
    for p in raw:
        player = _player(p)
        line = _line(p)
        mk = canonical_market_key(p.get("market_key"), p.get("market_label") or p.get("market"))
        if not player or line is None or mk not in SUPPORTED:
            continue
        # Do not model explicitly-labelled alternate lines as standard projections.
        if str(p.get("market_key") or "").lower().endswith("_alternate"):
            continue
        commence = _dt(p.get("commence_time"))
        if commence:
            if commence <= now:
                continue
            if commence > now + timedelta(hours=UPCOMING_HOURS):
                continue
        age = _age(p)
        if age is not None and age > LINE_MAX_AGE_SECONDS:
            continue
        rows.append({
            **p,
            "_player": player,
            "_market_key": mk,
            "_line": line,
            "_book_title": book_title(p),
            "_book_key": _source_key(p),
            "_age": age,
        })
    return rows

def _select_main_lines(rows):
    # One normal line per player/market/game/book. If a DFS provider publishes
    # multiple unlabeled lines, use the middle line rather than an extreme.
    groups = {}
    for r in rows:
        key = (
            r["_book_title"], r["_player"], r["_market_key"],
            r.get("event_id") or r.get("commence_time") or "",
        )
        groups.setdefault(key, []).append(r)
    out = []
    for group in groups.values():
        group.sort(key=lambda x: x["_line"])
        out.append(group[(len(group) - 1) // 2])
    return out

def _consensus_lines(rows):
    groups = {}
    for r in rows:
        key = (r["_player"], r["_market_key"], r.get("event_id") or r.get("commence_time") or "")
        groups.setdefault(key, []).append(r["_line"])
    return {k: median(v) for k, v in groups.items() if v}

def _implied(row, side):
    raw = row.get(f"{side.lower()}_implied_prob")
    if raw is not None:
        try:
            v = float(raw)
            return v / 100.0 if v > 1 else v
        except Exception:
            pass
    return american_implied(row.get(f"{side.lower()}_price"))

def refresh_all():
    raw = fetch_props()
    if not raw:
        raise RuntimeError("ParlayAPI returned no NFL props.")

    rows = _select_main_lines(_normalize_rows(raw))
    if not rows:
        raise RuntimeError(
            "NFL props were returned, but none matched the supported main markets "
            "or freshness window."
        )
    consensus = _consensus_lines(rows)
    snapshot = datetime.now(timezone.utc).isoformat()
    modeled = []

    history_cache = {}
    profile_cache = {}
    game_cache = {}

    for p in rows:
        player = p["_player"]
        market_key = p["_market_key"]
        stat_col, market_label, usage_col = SUPPORTED[market_key]

        if player not in history_cache:
            history_cache[player] = player_history(player)
        metrics = build_history_metrics(history_cache[player], stat_col, usage_col, p["_line"])
        if not metrics:
            continue

        if player not in profile_cache:
            profile_cache[player] = player_profile(player)
        profile = profile_cache[player]

        game_key = (p.get("home_team"), p.get("away_team"))
        if game_key not in game_cache:
            game_cache[game_key] = game_context(*game_key, p.get("commence_time"))
        game = game_cache[game_key] or {}

        base = metrics["base_prob"]
        usage_adj = metrics["usage_adjustment"]

        # Cross-book line value. A lower line than the market median helps the OVER;
        # a higher line helps the UNDER. Scale by the actual size of the prop.
        ckey = (player, market_key, p.get("event_id") or p.get("commence_time") or "")
        consensus_line = consensus.get(ckey, p["_line"])
        denom = max(abs(consensus_line) * .20, 1.0)
        line_adj = max(-.04, min(.04, (consensus_line - p["_line"]) / denom * .02))

        model_over = base + usage_adj + line_adj

        # When a real two-sided sportsbook price exists, blend a modest amount of
        # market information. DFS lines remain driven by the player model.
        market_adj = 0.0
        over_imp = _implied(p, "OVER")
        under_imp = _implied(p, "UNDER")
        if p["_book_title"] not in DFS_BOOKS and over_imp is not None and under_imp is not None:
            total = over_imp + under_imp
            fair_over = over_imp / total if total else .5
            market_adj = max(-.035, min(.035, (fair_over - .5) * .18))
            model_over += market_adj

        model_over = max(.04, min(.96, model_over))
        model_under = 1.0 - model_over

        if model_over >= model_under:
            side, prob = "OVER", model_over
            implied = over_imp
        else:
            side, prob = "UNDER", model_under
            implied = under_imp

        if p["_book_title"] in DFS_BOOKS or implied is None:
            edge = max(0.0, prob - .50)
            edge_type = "Model Edge"
        else:
            edge = max(0.0, prob - implied)
            edge_type = "True Edge"

        sample_total = metrics["games_prior"] + metrics["games_season"]
        confidence = min(
            .95,
            .40
            + min(sample_total, 20) / 100.0
            + min(metrics["games_recent5"], 5) * .025
            + (0.07 if profile.get("player_team") else 0.0)
            + (0.05 if game.get("week") else 0.0)
        )
        g, verdict = grade(edge, sample_total, confidence)

        home = p.get("home_team")
        away = p.get("away_team")
        team = profile.get("player_team")
        opponent = None
        if team:
            # Team abbreviations do not always match full names, so this is best-effort.
            opponent = away if team.lower() in str(home or "").lower() else home

        note_parts = []
        if metrics["games_season"] < 3:
            note_parts.append("early-season weighting uses prior year")
        if abs(usage_adj) >= .02:
            note_parts.append("recent usage trend")
        if abs(line_adj) >= .015:
            note_parts.append("favorable market line")
        status_note = " · ".join(note_parts) or "historical form + usage + market context"

        modeled.append({
            "snapshot_time": snapshot,
            "event_id": p.get("event_id"),
            "season": nfl_season(),
            "week": game.get("week"),
            "bookmaker": p["_book_key"],
            "bookmaker_title": p["_book_title"],
            "player": player,
            "player_id": profile.get("player_id"),
            "headshot_url": profile.get("headshot_url"),
            "position": profile.get("position"),
            "player_team": profile.get("player_team"),
            "market_key": market_key,
            "market": market_label,
            "line": p["_line"],
            "over_price": p.get("over_price"),
            "under_price": p.get("under_price"),
            "home_team": home,
            "away_team": away,
            "opponent": opponent,
            "commence_time": p.get("commence_time"),
            "model_prob_over": model_over,
            "model_prob_under": model_under,
            "recommended_side": side,
            "recommended_prob": prob,
            "display_edge": edge,
            "edge_type": edge_type,
            "grade": g,
            "verdict": verdict,
            "season_rate": metrics["season_rate"],
            "recent5_rate": metrics["recent5_rate"],
            "prior_rate": metrics["prior_rate"],
            "games_season": metrics["games_season"],
            "games_recent5": metrics["games_recent5"],
            "games_prior": metrics["games_prior"],
            "season_average": metrics["season_average"],
            "recent5_average": metrics["recent5_average"],
            "prior_average": metrics["prior_average"],
            "usage_adjustment": usage_adj,
            "line_value_adjustment": line_adj,
            "market_adjustment": market_adj,
            "confidence_score": confidence,
            "line_age_seconds": p["_age"],
            "line_last_update": p.get("last_update") or p.get("snapshot_time"),
            "status_note": status_note,
        })

    if not modeled:
        raise RuntimeError(
            "NFL lines loaded, but no players could be matched to usable historical stats yet."
        )

    replace_snapshot(modeled, snapshot)
    try:
        capture_actionable_props(modeled)
    except Exception:
        # Historical tracking must never break the live NFL board.
        pass
    return len(modeled)
