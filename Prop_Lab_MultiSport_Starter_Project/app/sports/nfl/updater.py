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


def _as_bool(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _is_explicit_alternate(row):
    """Detect alternate/boosted/lowered rows before they enter the model."""
    market_key = str(row.get("market_key") or "").lower()
    market_label = str(row.get("market_label") or row.get("market") or "").lower()

    # Canonical ParlayAPI alternate markets use the _alternate suffix.
    if market_key.endswith("_alternate") or " alternate" in market_label:
        return True

    # Also handle source-specific metadata if it is present.
    for key in (
        "is_alternate", "isAlternate", "alternate", "is_alt", "isAlt",
        "is_special", "isSpecial", "boosted", "is_boosted", "isBoosted",
    ):
        if _as_bool(row.get(key)):
            return True

    text_fields = (
        "line_type", "lineType", "projection_type", "projectionType",
        "option_type", "optionType", "variant", "tier", "label", "type",
    )
    alt_words = (
        "alternate", "alt line", "demon", "goblin", "scorcher",
        "discount", "boost", "special", "higher payout", "lower payout",
    )
    for key in text_fields:
        value = str(row.get(key) or "").strip().lower()
        if value and any(word in value for word in alt_words):
            return True

    return False


def _american_implied_local(price):
    try:
        p = float(price)
    except Exception:
        return None
    if p == 0:
        return None
    return 100.0 / (p + 100.0) if p > 0 else (-p) / ((-p) + 100.0)


def _dfs_balance_score(row):
    """
    Lower is better.

    Main PrizePicks/Underdog projections are the line intended to be roughly
    balanced between higher/lower. Alternate ladders are intentionally skewed.
    When a provider publishes several unlabeled rows for the same player/market,
    use effective prices to select the row whose fair two-sided probability is
    closest to 50/50.
    """
    over = _american_implied_local(row.get("over_price"))
    under = _american_implied_local(row.get("under_price"))

    if over is not None and under is not None and (over + under) > 0:
        fair_over = over / (over + under)
        return abs(fair_over - 0.50)

    # Missing DFS prices give us no price-based signal. Keep them eligible but
    # behind a clearly balanced priced row.
    return 0.30


def _looks_main_marker(row):
    for key in (
        "line_type", "lineType", "projection_type", "projectionType",
        "option_type", "optionType", "variant", "tier", "label", "type",
    ):
        value = str(row.get(key) or "").strip().lower()
        if value in {"main", "standard", "regular", "base", "normal"}:
            return True
    return False


def _row_age_seconds(row):
    """
    Return the source observation age. Smaller = newer/current.

    ParlayAPI normally supplies age_seconds. The fallbacks handle older payload
    variants that used ISO or epoch-millisecond timestamps.
    """
    for key in ("age_seconds", "ageSeconds", "age"):
        value = row.get(key)
        if value is not None:
            try:
                return max(0.0, float(value))
            except Exception:
                pass

    value = row.get("last_update") or row.get("lastUpdate") or row.get("snapshot_time")
    if value is None:
        return float("inf")

    try:
        numeric = float(value)
        # API last_update may be epoch milliseconds.
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return max(0.0, datetime.now(timezone.utc).timestamp() - numeric)
    except Exception:
        pass

    parsed = _dt(value)
    if parsed:
        return max(
            0.0,
            (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds(),
        )
    return float("inf")

def _normalize_rows(raw):
    rows = []
    now = datetime.now(timezone.utc)
    for p in raw:
        player = _player(p)
        line = _line(p)
        mk = canonical_market_key(p.get("market_key"), p.get("market_label") or p.get("market"))
        if not player or line is None or mk not in SUPPORTED:
            continue
        # Do not let alternate / boosted / discounted DFS ladders enter the
        # normal NFL prop board.
        if _is_explicit_alternate(p):
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
    """
    Keep one REGULAR/main projection per player / market / book.

    Native rows are authoritative:
      - PrizePicks native feed is standard-only.
      - Underdog native feed is balanced-only.

    If Render cannot reach Underdog natively, providers.py marks its Parlay rows
    with _fallback_source='parlay'. Those rows may include the full alt ladder.
    We DO NOT guess from the lowest line, newest line, or payout price.

    Instead, fallback Underdog lines are allowed only when another trustworthy
    book has the same player+market:
      - PrizePicks native STANDARD line
      - Fliff line

    The Underdog rung closest to the median reference line is selected.
    If no cross-book reference exists, the fallback Underdog prop is omitted
    rather than showing an almost-guaranteed alternate line.
    """
    # Build trustworthy cross-book reference lines by player + market.
    references = {}
    for r in rows:
        book = r.get("_book_title")
        if book not in {"PrizePicks", "Fliff"}:
            continue
        player = str(r.get("_player") or "").strip().lower()
        market = r.get("_market_key")
        line = r.get("_line")
        if not player or market is None or line is None:
            continue
        references.setdefault((player, market), []).append(float(line))

    def ref_median(player, market):
        vals = sorted(references.get((player, market), []))
        if not vals:
            return None
        n = len(vals)
        return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0

    groups = {}
    for r in rows:
        book = r["_book_title"]

        if book in DFS_BOOKS:
            commence = _dt(r.get("commence_time"))
            if commence:
                slate_date = commence.astimezone(timezone.utc).date().isoformat()
            else:
                snap = _dt(
                    r.get("snapshot_time")
                    or r.get("last_update")
                    or r.get("lastUpdate")
                )
                slate_date = (
                    snap.astimezone(timezone.utc).date().isoformat()
                    if snap else ""
                )
            # Ignore event_id for DFS. Alt ladder rungs can carry synthetic IDs.
            key = ("dfs", book, r["_player"], r["_market_key"], slate_date)
        else:
            key = (
                "book",
                book,
                r["_player"],
                r["_market_key"],
                r.get("canonical_event_id")
                or r.get("event_id")
                or r.get("commence_time")
                or "",
            )
        groups.setdefault(key, []).append(r)

    selected = []

    for group in groups.values():
        clean = [r for r in group if not _is_explicit_alternate(r)]
        if not clean:
            continue

        book = clean[0]["_book_title"]
        marked = [r for r in clean if _looks_main_marker(r)]
        candidates = marked or clean

        if book == "Underdog":
            # Hard safety gate: the only authoritative main-line marker we have
            # is Underdog's own native `line_type=balanced`. Flash/discount rows
            # and all flattened Parlay fallback rows are excluded.
            native = [
                r for r in candidates
                if str(r.get("line_type") or "").lower() == "balanced"
                and r.get("_fallback_source") != "parlay"
                and r.get("flash_line") in (None, "", False)
            ]
            if not native:
                continue

            native.sort(key=_row_age_seconds)
            selected.append(native[0])
            continue

        if book in DFS_BOOKS and len(candidates) > 1:
            # PrizePicks native input is standard-only; duplicates are usually
            # refresh copies, so newest wins.
            candidates.sort(key=_row_age_seconds)
            selected.append(candidates[0])
            continue

        candidates.sort(
            key=lambda r: (
                _row_age_seconds(r),
                str(
                    r.get("last_update")
                    or r.get("lastUpdate")
                    or r.get("snapshot_time")
                    or ""
                ),
            )
        )
        selected.append(candidates[0])

    return selected

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
    raw = fetch_props(markets=list(SUPPORTED.keys()))
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
