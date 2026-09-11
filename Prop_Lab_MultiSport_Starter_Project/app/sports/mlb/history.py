import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import DATABASE_PATH, DATABASE_URL, TIMEZONE
from .model import SUPPORTED, stat_value
from .providers import fetch_game_log

USE_POSTGRES = bool(DATABASE_URL)


def _connect():
    if USE_POSTGRES:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)

    import sqlite3
    c = sqlite3.connect(DATABASE_PATH)
    c.row_factory = sqlite3.Row
    return c


def _execute_many(sql_pg, sql_sqlite, rows):
    c = _connect()
    try:
        if USE_POSTGRES:
            with c.cursor() as cur:
                cur.executemany(sql_pg, rows)
        else:
            c.executemany(sql_sqlite, rows)
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_history_db():
    c = _connect()
    try:
        if USE_POSTGRES:
            c.execute("""
                CREATE TABLE IF NOT EXISTS mlb_prop_history (
                    id BIGSERIAL PRIMARY KEY,
                    pick_key TEXT NOT NULL UNIQUE,
                    captured_at TEXT NOT NULL,
                    snapshot_time TEXT,
                    game_date TEXT NOT NULL,
                    commence_time TEXT,
                    game_pk BIGINT,
                    bookmaker_title TEXT,
                    player TEXT NOT NULL,
                    mlb_player_id BIGINT,
                    market_key TEXT NOT NULL,
                    market TEXT,
                    line DOUBLE PRECISION,
                    recommended_side TEXT NOT NULL,
                    grade TEXT NOT NULL,
                    verdict TEXT,
                    recommended_prob DOUBLE PRECISION,
                    display_edge DOUBLE PRECISION,
                    edge_type TEXT,
                    home_team TEXT,
                    away_team TEXT,
                    player_team TEXT,
                    opponent TEXT,
                    actual_value DOUBLE PRECISION,
                    result TEXT NOT NULL DEFAULT 'PENDING',
                    graded_at TEXT
                )
            """)
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS mlb_prop_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pick_key TEXT NOT NULL UNIQUE,
                    captured_at TEXT NOT NULL,
                    snapshot_time TEXT,
                    game_date TEXT NOT NULL,
                    commence_time TEXT,
                    game_pk INTEGER,
                    bookmaker_title TEXT,
                    player TEXT NOT NULL,
                    mlb_player_id INTEGER,
                    market_key TEXT NOT NULL,
                    market TEXT,
                    line REAL,
                    recommended_side TEXT NOT NULL,
                    grade TEXT NOT NULL,
                    verdict TEXT,
                    recommended_prob REAL,
                    display_edge REAL,
                    edge_type TEXT,
                    home_team TEXT,
                    away_team TEXT,
                    player_team TEXT,
                    opponent TEXT,
                    actual_value REAL,
                    result TEXT NOT NULL DEFAULT 'PENDING',
                    graded_at TEXT
                )
            """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_mlb_history_date ON mlb_prop_history(game_date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mlb_history_result ON mlb_prop_history(result)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mlb_history_grade ON mlb_prop_history(grade)")
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _parse_dt(raw):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _game_date(row):
    dt = _parse_dt(row.get("commence_time"))
    if dt:
        return dt.astimezone(ZoneInfo(TIMEZONE)).date().isoformat()

    snap = _parse_dt(row.get("snapshot_time"))
    if snap:
        return snap.astimezone(ZoneInfo(TIMEZONE)).date().isoformat()

    return datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()


def _pick_key(row, game_date):
    # Frozen exact recommendation. If a line changes later, that new line is a
    # distinct pick, while repeated refreshes of the same line are not counted twice.
    parts = [
        game_date,
        str(row.get("commence_time") or ""),
        str(row.get("bookmaker_title") or ""),
        str(row.get("mlb_player_id") or row.get("player") or ""),
        str(row.get("market_key") or ""),
        str(row.get("line") if row.get("line") is not None else ""),
        str(row.get("recommended_side") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def capture_actionable_props(rows, snapshot_time=None):
    """
    Permanently save A/B recommendations before games start.

    GOOD and PROVISIONAL are both captured. Repeated refreshes of the same
    exact pick are de-duplicated by pick_key.
    """
    init_history_db()
    now = datetime.now(timezone.utc).isoformat()
    inserts = []

    for row in rows or []:
        grade = str(row.get("grade") or "").upper()
        verdict = str(row.get("verdict") or "").upper()
        side = str(row.get("recommended_side") or "").upper()

        if grade not in {"A", "B"}:
            continue
        if verdict not in {"GOOD", "PROVISIONAL"}:
            continue
        if side not in {"OVER", "UNDER"}:
            continue
        if row.get("line") is None or not row.get("market_key"):
            continue
        if not row.get("mlb_player_id"):
            continue

        gd = _game_date(row)
        key = _pick_key(row, gd)

        inserts.append((
            key,
            now,
            snapshot_time or row.get("snapshot_time"),
            gd,
            row.get("commence_time"),
            row.get("game_pk"),
            row.get("bookmaker_title"),
            row.get("player"),
            row.get("mlb_player_id"),
            row.get("market_key"),
            row.get("market"),
            row.get("line"),
            side,
            grade,
            verdict,
            row.get("recommended_prob"),
            row.get("display_edge"),
            row.get("edge_type"),
            row.get("home_team"),
            row.get("away_team"),
            row.get("player_team"),
            row.get("opponent"),
        ))

    if not inserts:
        return 0

    columns = """
        pick_key,captured_at,snapshot_time,game_date,commence_time,game_pk,
        bookmaker_title,player,mlb_player_id,market_key,market,line,
        recommended_side,grade,verdict,recommended_prob,display_edge,edge_type,
        home_team,away_team,player_team,opponent
    """
    pg = f"""
        INSERT INTO mlb_prop_history ({columns})
        VALUES ({','.join(['%s'] * 22)})
        ON CONFLICT (pick_key) DO NOTHING
    """
    sq = f"""
        INSERT OR IGNORE INTO mlb_prop_history ({columns})
        VALUES ({','.join(['?'] * 22)})
    """

    _execute_many(pg, sq, inserts)
    return len(inserts)


def _pending_rows():
    c = _connect()
    try:
        if USE_POSTGRES:
            rows = c.execute("""
                SELECT * FROM mlb_prop_history
                WHERE result='PENDING'
                ORDER BY game_date ASC, id ASC
            """).fetchall()
        else:
            rows = c.execute("""
                SELECT * FROM mlb_prop_history
                WHERE result='PENDING'
                ORDER BY game_date ASC, id ASC
            """).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def _split_date(split):
    raw = split.get("date") or (split.get("game") or {}).get("gameDate")
    if not raw:
        return None
    return str(raw)[:10]


def _split_game_pk(split):
    game = split.get("game") or {}
    try:
        return int(game.get("gamePk")) if game.get("gamePk") is not None else None
    except Exception:
        return None


def _choose_split(splits, row):
    wanted_date = row.get("game_date")
    wanted_pk = row.get("game_pk")

    matches = [s for s in splits if _split_date(s) == wanted_date]
    if not matches:
        return None

    if wanted_pk:
        for s in matches:
            if _split_game_pk(s) == int(wanted_pk):
                return s

    # Normal single-game day.
    if len(matches) == 1:
        return matches[0]

    # Doubleheader fallback: opponent/team context when available in StatsAPI.
    opponent = str(row.get("opponent") or "").lower()
    if opponent:
        for s in matches:
            opp = (
                ((s.get("opponent") or {}).get("name"))
                or ((s.get("team") or {}).get("name"))
                or ""
            ).lower()
            if opponent and opponent in opp:
                return s

    # Ambiguous doubleheader: do not guess.
    return None


def _result(side, actual, line):
    actual = float(actual)
    line = float(line)
    if actual == line:
        return "PUSH"
    if side == "OVER":
        return "WIN" if actual > line else "LOSS"
    if side == "UNDER":
        return "WIN" if actual < line else "LOSS"
    return "PENDING"


def grade_pending_history():
    """
    Grade completed historical picks from MLB Stats game logs.

    A pick remains PENDING when the player has no matching completed game log
    yet (postponement, very late completion, or ambiguous doubleheader). This
    prevents the tracker from inventing a loss or win.
    """
    init_history_db()
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    pending = _pending_rows()
    if not pending:
        return {"checked": 0, "graded": 0}

    cache = {}
    updates = []

    for row in pending:
        try:
            gd = datetime.strptime(row["game_date"], "%Y-%m-%d").date()
        except Exception:
            continue

        # Never grade today's games. The automatic morning job handles yesterday.
        if gd >= today:
            continue

        market_def = SUPPORTED.get(row.get("market_key"))
        player_id = row.get("mlb_player_id")
        if not market_def or not player_id:
            continue

        group, stat_key, _ = market_def
        season = gd.year
        ck = (int(player_id), season, group)

        if ck not in cache:
            try:
                cache[ck] = fetch_game_log(player_id, season, group)
            except Exception:
                cache[ck] = []

        split = _choose_split(cache[ck], row)
        if split is None:
            continue

        try:
            actual = stat_value(split, stat_key)
            outcome = _result(row.get("recommended_side"), actual, row.get("line"))
        except Exception:
            continue

        updates.append((
            float(actual),
            outcome,
            datetime.now(timezone.utc).isoformat(),
            row["id"],
        ))

    if updates:
        c = _connect()
        try:
            if USE_POSTGRES:
                with c.cursor() as cur:
                    cur.executemany("""
                        UPDATE mlb_prop_history
                        SET actual_value=%s,result=%s,graded_at=%s
                        WHERE id=%s
                    """, updates)
            else:
                c.executemany("""
                    UPDATE mlb_prop_history
                    SET actual_value=?,result=?,graded_at=?
                    WHERE id=?
                """, updates)
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    return {"checked": len(pending), "graded": len(updates)}


def _all_history():
    init_history_db()
    c = _connect()
    try:
        rows = c.execute("""
            SELECT * FROM mlb_prop_history
            ORDER BY game_date DESC, id DESC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def _summary(rows):
    wins = sum(1 for r in rows if r.get("result") == "WIN")
    losses = sum(1 for r in rows if r.get("result") == "LOSS")
    pushes = sum(1 for r in rows if r.get("result") == "PUSH")
    pending = sum(1 for r in rows if r.get("result") == "PENDING")
    graded = wins + losses
    return {
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "pending": pending,
        "graded": graded,
        "hit_rate": (wins / graded) if graded else None,
        "total": len(rows),
    }


def performance_data():
    rows = _all_history()
    today = datetime.now(ZoneInfo(TIMEZONE)).date()

    graded_rows = [r for r in rows if r.get("result") in {"WIN", "LOSS", "PUSH"}]
    a_rows = [r for r in rows if r.get("grade") == "A"]
    b_rows = [r for r in rows if r.get("grade") == "B"]

    def since(days):
        cutoff = today - timedelta(days=days - 1)
        return [
            r for r in rows
            if r.get("game_date")
            and datetime.strptime(r["game_date"], "%Y-%m-%d").date() >= cutoff
        ]

    # Market breakdown
    market_groups = {}
    for r in rows:
        name = r.get("market") or r.get("market_key") or "Unknown"
        market_groups.setdefault(name, []).append(r)

    market_stats = []
    for name, group in market_groups.items():
        s = _summary(group)
        if s["graded"]:
            market_stats.append({"market": name, **s})
    market_stats.sort(key=lambda x: (-x["graded"], -(x["hit_rate"] or 0)))

    # Book breakdown
    book_groups = {}
    for r in rows:
        name = r.get("bookmaker_title") or "Unknown"
        book_groups.setdefault(name, []).append(r)
    book_stats = []
    for name, group in book_groups.items():
        s = _summary(group)
        if s["graded"]:
            book_stats.append({"book": name, **s})
    book_stats.sort(key=lambda x: -x["graded"])

    return {
        "overall": _summary(rows),
        "grade_a": _summary(a_rows),
        "grade_b": _summary(b_rows),
        "last_7": _summary(since(7)),
        "last_30": _summary(since(30)),
        "markets": market_stats[:12],
        "books": book_stats,
        "recent": rows[:100],
        "graded_count": len(graded_rows),
    }
