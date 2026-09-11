import hashlib
from datetime import datetime, timezone, timedelta

from .db import conn, USE_POSTGRES
from .model import SUPPORTED, stat_value
from .providers import fetch_player_stats, normalize_name

HISTORY_COLUMNS = {
    "pick_key": "TEXT NOT NULL",
    "captured_at": "TEXT NOT NULL",
    "snapshot_time": "TEXT",
    "season": "INTEGER",
    "week": "INTEGER",
    "game_date": "TEXT",
    "event_id": "TEXT",
    "commence_time": "TEXT",
    "bookmaker_title": "TEXT",
    "player": "TEXT",
    "player_id": "TEXT",
    "market_key": "TEXT",
    "market": "TEXT",
    "line": "REAL",
    "recommended_side": "TEXT",
    "grade": "TEXT",
    "verdict": "TEXT",
    "recommended_prob": "REAL",
    "display_edge": "REAL",
    "edge_type": "TEXT",
    "home_team": "TEXT",
    "away_team": "TEXT",
    "actual_value": "REAL",
    "result": "TEXT",
    "graded_at": "TEXT",
}

def init_history_db():
    with conn() as c:
        if USE_POSTGRES:
            defs = ["id BIGSERIAL PRIMARY KEY"] + [f"{k} {v}" for k, v in HISTORY_COLUMNS.items()]
            c.execute(f"CREATE TABLE IF NOT EXISTS nfl_prop_history ({','.join(defs)})")
            for k, v in HISTORY_COLUMNS.items():
                c.execute(f"ALTER TABLE nfl_prop_history ADD COLUMN IF NOT EXISTS {k} {v.replace(' NOT NULL','')}")
        else:
            c.execute("CREATE TABLE IF NOT EXISTS nfl_prop_history (id INTEGER PRIMARY KEY AUTOINCREMENT)")
            existing = {r["name"] for r in c.execute("PRAGMA table_info(nfl_prop_history)").fetchall()}
            for k, v in HISTORY_COLUMNS.items():
                if k not in existing:
                    c.execute(f"ALTER TABLE nfl_prop_history ADD COLUMN {k} {v}")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_nfl_history_key ON nfl_prop_history(pick_key)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_nfl_history_result ON nfl_prop_history(result)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_nfl_history_date ON nfl_prop_history(game_date)")

def _pick_key(r):
    raw = "|".join(str(r.get(k) or "") for k in (
        "season", "week", "event_id", "commence_time", "bookmaker_title",
        "player", "market_key", "line", "recommended_side"
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def capture_actionable_props(rows):
    now = datetime.now(timezone.utc).isoformat()
    cols = list(HISTORY_COLUMNS.keys())
    saved = 0
    with conn() as c:
        for r in rows:
            if r.get("grade") not in {"A", "B"}:
                continue
            if r.get("verdict") not in {"GOOD", "PROVISIONAL"}:
                continue
            if r.get("recommended_side") not in {"OVER", "UNDER"}:
                continue
            commence = str(r.get("commence_time") or "")
            game_date = commence[:10] if len(commence) >= 10 else None
            row = {
                "pick_key": _pick_key(r),
                "captured_at": now,
                "snapshot_time": r.get("snapshot_time"),
                "season": r.get("season"),
                "week": r.get("week"),
                "game_date": game_date,
                "event_id": r.get("event_id"),
                "commence_time": r.get("commence_time"),
                "bookmaker_title": r.get("bookmaker_title"),
                "player": r.get("player"),
                "player_id": r.get("player_id"),
                "market_key": r.get("market_key"),
                "market": r.get("market"),
                "line": r.get("line"),
                "recommended_side": r.get("recommended_side"),
                "grade": r.get("grade"),
                "verdict": r.get("verdict"),
                "recommended_prob": r.get("recommended_prob"),
                "display_edge": r.get("display_edge"),
                "edge_type": r.get("edge_type"),
                "home_team": r.get("home_team"),
                "away_team": r.get("away_team"),
                "actual_value": None,
                "result": "PENDING",
                "graded_at": None,
            }
            values = tuple(row.get(k) for k in cols)
            if USE_POSTGRES:
                ph = ",".join(["%s"] * len(cols))
                c.execute(
                    f"INSERT INTO nfl_prop_history ({','.join(cols)}) VALUES ({ph}) ON CONFLICT (pick_key) DO NOTHING",
                    values,
                )
            else:
                ph = ",".join(["?"] * len(cols))
                c.execute(
                    f"INSERT OR IGNORE INTO nfl_prop_history ({','.join(cols)}) VALUES ({ph})",
                    values,
                )
            saved += 1
    return saved

def _pending():
    with conn() as c:
        rows = c.execute("SELECT * FROM nfl_prop_history WHERE result='PENDING'").fetchall()
    return [dict(r) for r in rows]

def _find_stat_row(pick):
    season = int(pick.get("season") or 0)
    week = int(pick.get("week") or 0)
    target = normalize_name(pick.get("player"))
    if not season or not week or not target:
        return None
    for r in fetch_player_stats(season):
        if int(r.get("week") or 0) != week:
            continue
        if normalize_name(r.get("player_display_name") or r.get("player_name")) == target:
            return r
    return None

def grade_pending():
    graded = 0
    now = datetime.now(timezone.utc).isoformat()
    for pick in _pending():
        row = _find_stat_row(pick)
        if not row:
            continue
        cfg = SUPPORTED.get(pick.get("market_key"))
        if not cfg:
            continue
        actual = stat_value(row, cfg[0])
        line = float(pick["line"])
        side = pick["recommended_side"]
        if actual == line:
            result = "PUSH"
        elif side == "OVER":
            result = "WIN" if actual > line else "LOSS"
        else:
            result = "WIN" if actual < line else "LOSS"
        with conn() as c:
            if USE_POSTGRES:
                c.execute(
                    "UPDATE nfl_prop_history SET actual_value=%s,result=%s,graded_at=%s WHERE id=%s",
                    (actual, result, now, pick["id"]),
                )
            else:
                c.execute(
                    "UPDATE nfl_prop_history SET actual_value=?,result=?,graded_at=? WHERE id=?",
                    (actual, result, now, pick["id"]),
                )
        graded += 1
    return graded

def _summary(rows):
    wins = sum(1 for r in rows if r.get("result") == "WIN")
    losses = sum(1 for r in rows if r.get("result") == "LOSS")
    pushes = sum(1 for r in rows if r.get("result") == "PUSH")
    pending = sum(1 for r in rows if r.get("result") == "PENDING")
    decisions = wins + losses
    return {
        "wins": wins, "losses": losses, "pushes": pushes, "pending": pending,
        "graded": decisions, "total": len(rows),
        "hit_rate": wins / decisions if decisions else None,
    }

def performance_data():
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM nfl_prop_history ORDER BY game_date DESC, captured_at DESC"
        ).fetchall()]

    today = datetime.now(timezone.utc).date()
    def since(days):
        cutoff = today - timedelta(days=days)
        return [r for r in rows if r.get("game_date") and datetime.fromisoformat(r["game_date"]).date() >= cutoff]

    def groups(field):
        out = []
        vals = sorted({r.get(field) for r in rows if r.get(field)})
        for v in vals:
            s = _summary([r for r in rows if r.get(field) == v])
            s["market" if field == "market" else "book"] = v
            out.append(s)
        return sorted(out, key=lambda x: (x["hit_rate"] is not None, x["graded"]), reverse=True)

    return {
        "overall": _summary(rows),
        "grade_a": _summary([r for r in rows if r.get("grade") == "A"]),
        "grade_b": _summary([r for r in rows if r.get("grade") == "B"]),
        "last_7": _summary(since(7)),
        "last_30": _summary(since(30)),
        "markets": groups("market"),
        "books": groups("bookmaker_title"),
        "recent": rows[:100],
    }
