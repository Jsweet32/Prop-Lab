import sqlite3
from contextlib import contextmanager
from .config import DATABASE_PATH, DATABASE_URL

COLUMNS = {
    "snapshot_time":"TEXT NOT NULL",
    "bookmaker":"TEXT NOT NULL",
    "bookmaker_title":"TEXT",
    "player":"TEXT NOT NULL",
    "market_key":"TEXT NOT NULL",
    "market":"TEXT",
    "line":"REAL",
    "over_price":"REAL",
    "under_price":"REAL",
    "home_team":"TEXT",
    "away_team":"TEXT",
    "commence_time":"TEXT",
    "game_pk":"INTEGER",
    "model_prob_over":"REAL",
    "base_prob_over":"REAL",
    "implied_prob_over":"REAL",
    "edge_over":"REAL",
    "grade":"TEXT",
    "verdict":"TEXT",
    "season_rate":"REAL",
    "l30_rate":"REAL",
    "l10_rate":"REAL",
    "games_season":"INTEGER",
    "games_l30":"INTEGER",
    "games_l10":"INTEGER",
    "mlb_player_id":"INTEGER",
    "player_team":"TEXT",
    "player_hand":"TEXT",
    "opponent":"TEXT",
    "probable_pitcher":"TEXT",
    "probable_pitcher_id":"INTEGER",
    "pitcher_hand":"TEXT",
    "venue":"TEXT",
    "park_factor":"REAL",
    "matchup_adjustment":"REAL",
    "playing_time_adjustment":"REAL",
    "context_confidence":"TEXT",
    "xba":"REAL","xslg":"REAL","xwoba":"REAL",
    "avg_exit_velocity":"REAL","avg_launch_angle":"REAL",
    "hard_hit_pct":"REAL","barrel_pct":"REAL","whiff_pct":"REAL",
    "statcast_adjustment":"REAL","statcast_status":"TEXT",
    "recommended_side":"TEXT","recommended_prob":"REAL","recommended_edge":"REAL",
    "under_model_prob":"REAL","under_implied_prob":"REAL","under_edge":"REAL",
    "score_type":"TEXT","confidence_score":"REAL","sample_ok":"INTEGER",
    "display_edge":"REAL","edge_type":"TEXT",
    "lineup_status":"TEXT","line_age_seconds":"REAL","line_last_update":"TEXT",
    "status_note":"TEXT",
}

USE_POSTGRES = bool(DATABASE_URL)

def _pg_connect():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)

@contextmanager
def conn():
    if USE_POSTGRES:
        c = _pg_connect()
    else:
        c = sqlite3.connect(DATABASE_PATH)
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
            defs = ["id BIGSERIAL PRIMARY KEY"] + [f"{name} {typ}" for name, typ in COLUMNS.items()]
            c.execute(f"CREATE TABLE IF NOT EXISTS props ({','.join(defs)})")
            # Safe upgrades if a future deployment adds columns.
            for name, typ in COLUMNS.items():
                c.execute(f"ALTER TABLE props ADD COLUMN IF NOT EXISTS {name} {typ.replace(' NOT NULL','')}")
        else:
            c.execute("CREATE TABLE IF NOT EXISTS props (id INTEGER PRIMARY KEY AUTOINCREMENT)")
            existing={r["name"] for r in c.execute("PRAGMA table_info(props)").fetchall()}
            for name,typ in COLUMNS.items():
                if name not in existing:
                    c.execute(f"ALTER TABLE props ADD COLUMN {name} {typ}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_props_snapshot ON props(snapshot_time)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_props_player ON props(player)")

def replace_snapshot(rows, snapshot_time):
    if not rows:
        return
    cols=list(COLUMNS.keys())
    clean=[tuple(row.get(col) for col in cols) for row in rows]
    with conn() as c:
        if USE_POSTGRES:
            placeholders=",".join(["%s"]*len(cols))
            sql=f"INSERT INTO props ({','.join(cols)}) VALUES ({placeholders})"
            with c.cursor() as cur:
                cur.executemany(sql, clean)
            # Keep only the newest board. This makes the Neon free tier last.
            c.execute("DELETE FROM props WHERE snapshot_time <> %s", (snapshot_time,))
        else:
            placeholders=",".join(["?"]*len(cols))
            sql=f"INSERT INTO props ({','.join(cols)}) VALUES ({placeholders})"
            c.executemany(sql, clean)
            c.execute("DELETE FROM props WHERE snapshot_time <> ?", (snapshot_time,))

def latest_snapshot():
    with conn() as c:
        r=c.execute("SELECT MAX(snapshot_time) AS t FROM props").fetchone()
        return r["t"] if r and r["t"] else None

def latest_rows(limit=10000):
    snap=latest_snapshot()
    if not snap:
        return []
    sql = """
        SELECT * FROM props WHERE snapshot_time={snap_ph}
        ORDER BY
          CASE verdict
            WHEN 'GOOD' THEN 0
            WHEN 'PROVISIONAL' THEN 1
            WHEN 'BORDERLINE' THEN 2
            WHEN 'WAIT' THEN 3
            WHEN 'PASS' THEN 4
            WHEN 'NO ACTION' THEN 5
            ELSE 6
          END,
          display_edge DESC,
          player ASC
        LIMIT {limit_ph}
    """
    with conn() as c:
        if USE_POSTGRES:
            rows=c.execute(sql.format(snap_ph="%s", limit_ph="%s"), (snap, limit)).fetchall()
        else:
            rows=c.execute(sql.format(snap_ph="?", limit_ph="?"), (snap, limit)).fetchall()
        return [dict(r) for r in rows]
