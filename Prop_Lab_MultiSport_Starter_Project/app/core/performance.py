from typing import Dict, Any

from app.sports.mlb.history import performance_data as mlb_performance_data
from app.sports.nfl.history import performance_data as nfl_performance_data


EMPTY_SUMMARY = {
    "wins": 0,
    "losses": 0,
    "pushes": 0,
    "pending": 0,
    "graded": 0,
    "hit_rate": None,
    "total": 0,
}


def _empty_sport(name: str, slug: str) -> Dict[str, Any]:
    return {
        "sport": name,
        "slug": slug,
        "status": "COMING SOON",
        "overall": dict(EMPTY_SUMMARY),
        "grade_a": dict(EMPTY_SUMMARY),
        "grade_b": dict(EMPTY_SUMMARY),
        "last_7": dict(EMPTY_SUMMARY),
        "last_30": dict(EMPTY_SUMMARY),
        "markets": [],
        "books": [],
        "recent": [],
    }


def global_performance_data() -> Dict[str, Any]:
    """
    Site-wide performance hub.

    MLB is live now. NFL/NBA/CFB are intentionally represented here already
    so their model-performance modules can be plugged into the same page later
    without changing the page structure.
    """
    mlb = mlb_performance_data()
    mlb.update({
        "sport": "MLB",
        "slug": "mlb",
        "status": "LIVE",
    })

    nfl = nfl_performance_data()
    nfl.update({
        "sport": "NFL",
        "slug": "nfl",
        "status": "LIVE",
    })

    sports = [
        mlb,
        nfl,
        _empty_sport("NBA", "nba"),
        _empty_sport("CFB", "cfb"),
    ]

    live_sports = [s for s in sports if s["status"] == "LIVE"]

    # Aggregate site-wide counts across all currently-live sports.
    wins = sum(s["overall"]["wins"] for s in live_sports)
    losses = sum(s["overall"]["losses"] for s in live_sports)
    pushes = sum(s["overall"]["pushes"] for s in live_sports)
    pending = sum(s["overall"]["pending"] for s in live_sports)
    graded = wins + losses

    a_wins = sum(s["grade_a"]["wins"] for s in live_sports)
    a_losses = sum(s["grade_a"]["losses"] for s in live_sports)
    b_wins = sum(s["grade_b"]["wins"] for s in live_sports)
    b_losses = sum(s["grade_b"]["losses"] for s in live_sports)

    last7_wins = sum(s["last_7"]["wins"] for s in live_sports)
    last7_losses = sum(s["last_7"]["losses"] for s in live_sports)
    last30_wins = sum(s["last_30"]["wins"] for s in live_sports)
    last30_losses = sum(s["last_30"]["losses"] for s in live_sports)

    return {
        "sports": sports,
        "overall": {
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "pending": pending,
            "graded": graded,
            "hit_rate": wins / graded if graded else None,
        },
        "grade_a": {
            "wins": a_wins,
            "losses": a_losses,
            "graded": a_wins + a_losses,
            "hit_rate": a_wins / (a_wins + a_losses) if (a_wins + a_losses) else None,
        },
        "grade_b": {
            "wins": b_wins,
            "losses": b_losses,
            "graded": b_wins + b_losses,
            "hit_rate": b_wins / (b_wins + b_losses) if (b_wins + b_losses) else None,
        },
        "last_7": {
            "wins": last7_wins,
            "losses": last7_losses,
            "graded": last7_wins + last7_losses,
            "hit_rate": last7_wins / (last7_wins + last7_losses) if (last7_wins + last7_losses) else None,
        },
        "last_30": {
            "wins": last30_wins,
            "losses": last30_losses,
            "graded": last30_wins + last30_losses,
            "hit_rate": last30_wins / (last30_wins + last30_losses) if (last30_wins + last30_losses) else None,
        },
    }
