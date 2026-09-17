"""Small runtime guardrails for production data-source issues.

These are intentionally isolated so MLB Underdog remains untouched while NFL
Underdog is temporarily hidden until its native primary-line feed is reliable.
"""
from datetime import datetime, timezone


def apply_runtime_fixes():
    _patch_nfl_board()
    _repair_mlb_history_schema()
    _capture_current_mlb_snapshot()


def _patch_nfl_board():
    from app.sports.nfl import providers, updater

    # Temporarily suppress NFL Underdog only. The provider can still be worked on
    # independently, but unverified lines must not reach the public board/model.
    original_normalize = updater._normalize_rows

    def normalize_without_underdog(raw):
        rows = original_normalize(raw)
        return [r for r in rows if str(r.get("_book_title") or "").lower() != "underdog"]

    updater._normalize_rows = normalize_without_underdog

    # nflverse's headshot column is not consistently populated. ESPN IDs are in
    # the same player record, so use ESPN's stable headshot CDN as a fallback.
    original_profile = providers.player_profile

    def player_profile_with_headshot(name):
        profile = dict(original_profile(name) or {})
        if profile.get("headshot_url"):
            return profile
        try:
            player = providers._players_by_name().get(providers.normalize_name(name)) or {}
            espn_id = player.get("espn_id")
            if espn_id is not None and str(espn_id).strip() not in {"", "nan", "None"}:
                espn_id = str(espn_id).split(".")[0]
                profile["headshot_url"] = f"https://a.espncdn.com/i/headshots/nfl/players/full/{espn_id}.png"
        except Exception:
            pass
        return profile

    # updater imported player_profile directly, so patch both references.
    providers.player_profile = player_profile_with_headshot
    updater.player_profile = player_profile_with_headshot


def _repair_mlb_history_schema():
    """Bring an older persistent mlb_prop_history table up to current schema."""
    from app.sports.mlb import history

    if not history.USE_POSTGRES:
        return

    columns = {
        "snapshot_time": "TEXT",
        "commence_time": "TEXT",
        "game_pk": "BIGINT",
        "bookmaker_title": "TEXT",
        "mlb_player_id": "BIGINT",
        "market": "TEXT",
        "line": "DOUBLE PRECISION",
        "verdict": "TEXT",
        "recommended_prob": "DOUBLE PRECISION",
        "display_edge": "DOUBLE PRECISION",
        "edge_type": "TEXT",
        "home_team": "TEXT",
        "away_team": "TEXT",
        "player_team": "TEXT",
        "opponent": "TEXT",
        "actual_value": "DOUBLE PRECISION",
        "result": "TEXT NOT NULL DEFAULT 'PENDING'",
        "graded_at": "TEXT",
    }

    c = history._connect()
    try:
        for name, sql_type in columns.items():
            c.execute(f"ALTER TABLE mlb_prop_history ADD COLUMN IF NOT EXISTS {name} {sql_type}")
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _capture_current_mlb_snapshot():
    """Seed tracking only from recommendations whose games have not started."""
    try:
        from app.sports.mlb.db import latest_rows, latest_snapshot
        from app.sports.mlb.history import capture_actionable_props

        now = datetime.now(timezone.utc)
        current = []
        for row in latest_rows() or []:
            start = _parse_dt(row.get("commence_time"))
            if start is not None and start.astimezone(timezone.utc) > now:
                current.append(row)
        if current:
            capture_actionable_props(current, latest_snapshot())
    except Exception as exc:
        # Keep startup available, but make the failure visible in Render logs.
        print(f"MLB performance capture startup warning: {exc}")
