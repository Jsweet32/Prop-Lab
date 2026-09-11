import sqlite3
from datetime import datetime, timezone
from .config import DATABASE_PATH, DATABASE_URL

USE_POSTGRES = bool(DATABASE_URL)

def _pg_connect():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)

def _conn():
    if USE_POSTGRES:
        return _pg_connect()
    c = sqlite3.connect(DATABASE_PATH)
    c.row_factory = sqlite3.Row
    return c

def _ensure_schema(c):
    if USE_POSTGRES:
        c.execute("""
        CREATE TABLE IF NOT EXISTS kalshi_markets (
            id BIGSERIAL PRIMARY KEY,
            snapshot_time TEXT NOT NULL,
            event_id TEXT, commence_time TEXT, home_team TEXT, away_team TEXT,
            selection TEXT, market_key TEXT, yes_price REAL, no_price REAL,
            yes_implied_prob REAL, no_implied_prob REAL, volume_24h_usd REAL,
            raw_json TEXT
        )""")
    else:
        c.execute("""
        CREATE TABLE IF NOT EXISTS kalshi_markets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_time TEXT NOT NULL,
            event_id TEXT, commence_time TEXT, home_team TEXT, away_team TEXT,
            selection TEXT, market_key TEXT, yes_price REAL, no_price REAL,
            yes_implied_prob REAL, no_implied_prob REAL, volume_24h_usd REAL,
            raw_json TEXT
        )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_kalshi_snapshot ON kalshi_markets(snapshot_time)")

def init_kalshi_db():
    with _conn() as c:
        _ensure_schema(c)
        c.commit()

def replace_kalshi_snapshot(rows):
    snapshot = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        _ensure_schema(c)
        c.execute("DELETE FROM kalshi_markets")
        vals=[]
        for r in rows:
            vals.append((
                snapshot, r.get("event_id"), r.get("commence_time"),
                r.get("home_team"), r.get("away_team"),
                r.get("selection") or r.get("title") or r.get("name"),
                r.get("market_key") or r.get("market"),
                r.get("yes_price"), r.get("no_price"),
                r.get("yes_implied_prob"), r.get("no_implied_prob"),
                r.get("volume_24h_usd"),
                r.get("raw_json") if isinstance(r.get("raw_json"), str) else None,
            ))
        if vals:
            ph = ",".join(["%s"]*13) if USE_POSTGRES else ",".join(["?"]*13)
            sql=f"""INSERT INTO kalshi_markets (
                snapshot_time,event_id,commence_time,home_team,away_team,selection,
                market_key,yes_price,no_price,yes_implied_prob,no_implied_prob,
                volume_24h_usd,raw_json
            ) VALUES ({ph})"""
            if USE_POSTGRES:
                with c.cursor() as cur:
                    cur.executemany(sql, vals)
            else:
                c.executemany(sql, vals)
        c.commit()
    return {"snapshot_time": snapshot, "rows": len(rows)}

def latest_kalshi_rows(limit=5000):
    with _conn() as c:
        _ensure_schema(c)
        ph="%s" if USE_POSTGRES else "?"
        rows=c.execute(f"""
            SELECT * FROM kalshi_markets
            ORDER BY COALESCE(volume_24h_usd,0) DESC, commence_time ASC
            LIMIT {ph}
        """,(limit,)).fetchall()
        c.commit()
        return [dict(r) for r in rows]

def latest_kalshi_snapshot():
    with _conn() as c:
        _ensure_schema(c)
        row=c.execute("SELECT MAX(snapshot_time) AS t FROM kalshi_markets").fetchone()
        c.commit()
        return row["t"] if row and row["t"] else None
