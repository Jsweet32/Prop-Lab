import sqlite3
from contextlib import contextmanager
from .config import DATABASE_PATH, DATABASE_URL

COLUMNS = {
    "snapshot_time": "TEXT NOT NULL",
    "event_id": "TEXT",
    "season": "INTEGER",
    "week": "INTEGER",
    "bookmaker": "TEXT NOT NULL",
    "bookmaker_title": "TEXT",
    "player": "TEXT NOT NULL",
    "player_id": "TEXT",
    "headshot_url": "TEXT",
    "position": "TEXT",
    "player_team": "TEXT",
    "market_key": "TEXT NOT NULL",
    "market": "TEXT",
    "line": "REAL",
    "over_price": "REAL",
    "under_price": "REAL",
    "home_team": "TEXT",
    "away_team": "TEXT",
    "opponent": "TEXT",
    "commence_time": "TEXT",
    "model_prob_over": "REAL",
    "model_prob_under": "REAL",
    "recommended_side": "TEXT",
    "recommended_prob": "REAL",
    "display_edge": "REAL",
    "edge_type": "TEXT",
    "grade": "TEXT",
    "verdict": "TEXT",
    "season_rate": "REAL",
    "recent5_rate": "REAL",
    "prior_rate": "REAL",
    "games_season": "INTEGER",
    "games_recent5": "INTEGER",
    "games_prior": "INTEGER",
    "season_average": "REAL",
    "recent5_average": "REAL",
    "prior_average": "REAL",
    "usage_adjustment": "REAL",
    "line_value_adjustment": "REAL",
    "market_adjustment": "REAL",
    "confidence_score": "REAL",
    "line_age_seconds": "REAL",
    "line_last_update": "TEXT",
    "status_note": "TEXT",
}

USE_POSTGRES = bool(DATABASE_URL)

def _pg_connect():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)

@contextmanager
def conn():
    c = _pg_connect() if USE_POSTGRES else sqlite3.connect(DATABASE_PATH)
    if not USE_POSTGRES:
        c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def init_db():
    with conn() as c:
        if USE_POSTGRES:
            defs = ["id BIGSERIAL PRIMARY KEY"] + [f"{k} {v}" for k, v in COLUMNS.items()]
            c.execute(f"CREATE TABLE IF NOT EXISTS nfl_props ({','.join(defs)})")
            for k, v in COLUMNS.items():
                c.execute(f"ALTER TABLE nfl_props ADD COLUMN IF NOT EXISTS {k} {v.replace(' NOT NULL','')}")
        else:
            c.execute("CREATE TABLE IF NOT EXISTS nfl_props (id INTEGER PRIMARY KEY AUTOINCREMENT)")
            existing = {r["name"] for r in c.execute("PRAGMA table_info(nfl_props)").fetchall()}
            for k, v in COLUMNS.items():
                if k not in existing:
                    c.execute(f"ALTER TABLE nfl_props ADD COLUMN {k} {v}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_nfl_props_snapshot ON nfl_props(snapshot_time)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_nfl_props_player ON nfl_props(player)")

def replace_snapshot(rows, snapshot_time):
    if not rows:
        return
    cols = list(COLUMNS.keys())
    clean = [tuple(r.get(c) for c in cols) for r in rows]
    with conn() as c:
        if USE_POSTGRES:
            ph = ",".join(["%s"] * len(cols))
            with c.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO nfl_props ({','.join(cols)}) VALUES ({ph})",
                    clean,
                )
            c.execute("DELETE FROM nfl_props WHERE snapshot_time <> %s", (snapshot_time,))
        else:
            ph = ",".join(["?"] * len(cols))
            c.executemany(
                f"INSERT INTO nfl_props ({','.join(cols)}) VALUES ({ph})",
                clean,
            )
            c.execute("DELETE FROM nfl_props WHERE snapshot_time <> ?", (snapshot_time,))

def latest_snapshot():
    with conn() as c:
        r = c.execute("SELECT MAX(snapshot_time) AS t FROM nfl_props").fetchone()
        return r["t"] if r and r["t"] else None

def latest_rows(limit=10000):
    snap = latest_snapshot()
    if not snap:
        return []
    sql = """
        SELECT * FROM nfl_props WHERE snapshot_time={s}
        ORDER BY
          CASE verdict
            WHEN 'GOOD' THEN 0
            WHEN 'PROVISIONAL' THEN 1
            WHEN 'BORDERLINE' THEN 2
            WHEN 'WAIT' THEN 3
            ELSE 4
          END,
          display_edge DESC,
          player ASC
        LIMIT {l}
    """
    with conn() as c:
        if USE_POSTGRES:
            rows = c.execute(sql.format(s="%s", l="%s"), (snap, limit)).fetchall()
        else:
            rows = c.execute(sql.format(s="?", l="?"), (snap, limit)).fetchall()
    return [dict(r) for r in rows]
