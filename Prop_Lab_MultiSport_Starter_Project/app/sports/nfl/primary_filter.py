"""Runtime guardrails for NFL DFS primary projections.

ParlayAPI exposes DFS projection metadata on /props rows. For Underdog,
`odds_type=standard` is the plain two-way projection; demon/goblin are alternate
tiers. `period=FULL` identifies confirmed full-game lines.

Important: older/current fallback rows are not guaranteed to carry every tag.
We therefore reject rows that are explicitly known to be alternates or periods,
but we do not erase the entire Underdog board just because a tag is absent.
The updater's cross-book/main-line selection remains the second guardrail.
"""


def _book_key(row):
    return str(row.get("bookmaker") or row.get("source") or "").strip().lower()


def _is_underdog(row):
    return _book_key(row) in {"underdog", "underdog_fantasy"}


def _is_parlay_fallback(row):
    return row.get("_fallback_source") == "parlay"


def _is_allowed_full_game_candidate(row):
    odds_type = str(row.get("odds_type") or "").strip().lower()
    projection_type = str(row.get("projection_type") or "").strip().lower()
    period = str(row.get("period") or "").strip().upper()

    # Explicit DFS alternate tags are authoritative and must be excluded.
    if odds_type in {"demon", "goblin", "promo", "special", "alternate", "alt"}:
        return False
    if projection_type in {"DEMON", "GOBLIN", "PROMO", "SPECIAL", "ALTERNATE", "ALT"}:
        return False

    # Reject any explicit non-full-game segment. UNKNOWN is legacy/ambiguous,
    # not proof that the row is a quarter/half line.
    if period and period not in {"FULL", "UNKNOWN"}:
        return False

    return True


def install_primary_line_filter(updater_module):
    """Wrap updater.fetch_props once with conservative Underdog guardrails."""
    current = updater_module.fetch_props
    if getattr(current, "_prop_lab_primary_filter", False):
        return

    def fetch_primary_props(*args, **kwargs):
        rows = current(*args, **kwargs)
        filtered = []
        for row in rows:
            if _is_underdog(row) and _is_parlay_fallback(row):
                if not _is_allowed_full_game_candidate(row):
                    continue
            filtered.append(row)
        return filtered

    fetch_primary_props._prop_lab_primary_filter = True
    updater_module.fetch_props = fetch_primary_props
