"""Runtime guardrails for NFL DFS primary projections.

ParlayAPI exposes alternate NFL DFS ladders as distinct canonical markets using
an `_alternate` suffix. The normal full-game market (for example
`player_receptions`) is the primary projection. Do not infer the primary line
from its price or from whether the line looks high/low.
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

    # ParlayAPI's canonical NFL alternate ladders are separate *_alternate
    # markets. These are never the standard Underdog board projection.
    if market_key.endswith("_alternate") or "alternate" in market_label:
        return False

    # Explicit DFS alternate tiers are never primary.
    if odds_type in {"demon", "goblin", "alternate", "alt"}:
        return False
    if projection_type in {"demon", "goblin", "alternate", "alt"}:
        return False

    # Confirmed segmented props are not full-game props. UNKNOWN/missing is
    # allowed because legacy rows can lack retained segment metadata.
    if period and period not in {"FULL", "UNKNOWN"}:
        return False

    return True


def install_primary_line_filter(updater_module):
    """Wrap updater.fetch_props once so alternate Underdog markets never enter it."""
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
