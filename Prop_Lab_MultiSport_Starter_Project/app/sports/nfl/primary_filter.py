"""Runtime guardrails for NFL DFS primary projections.

ParlayAPI exposes DFS projection metadata on /props rows. For Underdog,
`odds_type=standard` is the plain two-way projection; demon/goblin/special
rows are alternate tiers. `period=FULL` identifies confirmed full-game lines.

This wrapper is intentionally applied after providers.fetch_props so it also
covers the Parlay fallback used when Underdog's native endpoint is blocked from
Render.
"""


def _book_key(row):
    return str(row.get("bookmaker") or row.get("source") or "").strip().lower()


def _is_underdog(row):
    return _book_key(row) in {"underdog", "underdog_fantasy"}


def _is_parlay_fallback(row):
    return row.get("_fallback_source") == "parlay"


def _is_primary_full_game(row):
    odds_type = str(row.get("odds_type") or "").strip().lower()
    projection_type = str(row.get("projection_type") or "").strip().lower()
    period = str(row.get("period") or "").strip().upper()

    # ParlayAPI's authoritative DFS tag. Do not guess from price or line size.
    if odds_type and odds_type != "standard":
        return False
    if projection_type and projection_type != "standard":
        return False

    # Confirmed segmented props must never be mixed into the full-game board.
    if period and period != "FULL":
        return False

    # Legacy UNKNOWN/missing tags cannot prove a row is primary. The current
    # Parlay feed supplies these tags, so reject untagged fallback rows rather
    # than allowing low alternate OVER lines back onto the board.
    if not odds_type and not projection_type:
        return False

    return True


def install_primary_line_filter(updater_module):
    """Wrap updater.fetch_props once so only primary Underdog rows reach it."""
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
