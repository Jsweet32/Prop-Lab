"""Runtime guardrails for NFL DFS primary projections.

ParlayAPI's current /props contract gives us two authoritative signals for DFS
rows: the projection type (`standard`/`demon`/`goblin`) and the prop period
(`FULL`, `Q1`, `1H`, ...).  Legacy `UNKNOWN` rows are unsafe for Underdog: the
API docs explicitly say older segmented projections were once stored under the
unsuffixed full-game key, so those rows can look exactly like a primary line.
"""


def _book_key(row):
    return str(row.get("bookmaker") or row.get("source") or "").strip().lower()


def _is_underdog(row):
    return _book_key(row) in {"underdog", "underdog_fantasy"}


def _is_parlay_fallback(row):
    return row.get("_fallback_source") == "parlay"


def _is_primary_full_game(row):
    market_key = str(row.get("market_key") or row.get("marketType") or "").strip().lower()
    market_label = str(row.get("market_label") or row.get("market") or "").strip().lower()
    period = str(row.get("period") or "").strip().upper()
    odds_type = str(row.get("odds_type") or "").strip().lower()
    projection_type = str(row.get("projection_type") or "").strip().lower()

    # Explicit alternate market families are never the primary projection.
    if market_key.endswith("_alternate") or "alternate" in market_label:
        return False

    # The app's own DFS tag is authoritative when present.
    if odds_type and odds_type != "standard":
        return False
    if projection_type and projection_type != "standard":
        return False

    # Critical safety rule: only confirmed FULL rows may enter from the Parlay
    # fallback. Parlay documents that legacy UNKNOWN rows can contain Q1/1H
    # projections written under the unsuffixed full-game key. That is exactly
    # capable of producing impossible-looking 0.5/1.5 reception lines.
    if period != "FULL":
        return False

    # Current DFS rows should carry the source projection type. If neither alias
    # exists, the row cannot be proven to be Underdog's standard board line.
    if not odds_type and not projection_type:
        return False

    return True


def install_primary_line_filter(updater_module):
    """Wrap updater.fetch_props once so only confirmed primary Underdog rows enter it."""
    current = updater_module.fetch_props
    if getattr(current, "_prop_lab_primary_filter", False):
        return

    def fetch_primary_props(*args, **kwargs):
        rows = current(*args, **kwargs)
        filtered = []
        for row in rows:
            if _is_underdog(row) and _is_parlay_fallback(row):
                if not _is_primary_full_game(row):
                    continue
            filtered.append(row)
        return filtered

    fetch_primary_props._prop_lab_primary_filter = True
    updater_module.fetch_props = fetch_primary_props
