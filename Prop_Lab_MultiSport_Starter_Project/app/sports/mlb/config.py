import os
from dotenv import load_dotenv
load_dotenv()

PARLAY_API_KEY = os.getenv("PARLAY_API_KEY", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "mlb_props.db")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TIMEZONE = os.getenv("TIMEZONE", "America/New_York")
MAX_PLAYERS = int(os.getenv("MAX_PLAYERS", "0"))
ENABLE_ADVANCED_CONTEXT = os.getenv("ENABLE_ADVANCED_CONTEXT", "true").lower() == "true"

LINE_MAX_AGE_SECONDS = int(os.getenv("LINE_MAX_AGE_SECONDS", "3600"))
UPCOMING_HOURS = int(os.getenv("UPCOMING_HOURS", "36"))

BOOKMAKERS = ["prizepicks", "prizepicks_mobile", "underdog", "fliff", "kalshi"]

BOOK_TITLES = {
    "prizepicks": "PrizePicks",
    "prizepicks_mobile": "PrizePicks",
    "underdog": "Underdog",
    "fliff": "Fliff",
    "kalshi": "Kalshi",
}

SPORT_KEY = "baseball_mlb"

DFS_BOOKS = {"PrizePicks", "Underdog"}
DFS_MIN_SEASON_GAMES = int(os.getenv("DFS_MIN_SEASON_GAMES", "20"))
DFS_MIN_RECENT_GAMES = int(os.getenv("DFS_MIN_RECENT_GAMES", "5"))

# Model-edge grading for DFS pick'em. This is a ranking metric, not payout EV.
DFS_GOOD_EDGE = float(os.getenv("DFS_GOOD_EDGE", "0.08"))
DFS_A_EDGE = float(os.getenv("DFS_A_EDGE", "0.12"))
DFS_BORDERLINE_EDGE = float(os.getenv("DFS_BORDERLINE_EDGE", "0.05"))

# Compatibility setting retained so older diagnostics imports cannot break deployment.
BOOK_DIAGNOSTIC_MAX_AGE_SECONDS = int(os.getenv("BOOK_DIAGNOSTIC_MAX_AGE_SECONDS", "3600"))

# Conservative source-UI rules.
DFS_SIDE_POLICIES = {
    "PrizePicks": {
        "player_home_runs": "OVER_ONLY",
        "batter_home_runs": "OVER_ONLY",
    },
    "Underdog": {
        "player_home_runs": "OVER_ONLY",
        "batter_home_runs": "OVER_ONLY",
    },
}

# Dedicated Kalshi prediction-market settings.
KALSHI_MARKETS_MAX_AGE_SECONDS = int(os.getenv("KALSHI_MARKETS_MAX_AGE_SECONDS", "3600"))


# Live MLB game-awareness settings.
SCOREBOARD_POLL_SECONDS = int(os.getenv("SCOREBOARD_POLL_SECONDS", "30"))
EXCLUDE_STARTED_GAME_PROPS = os.getenv("EXCLUDE_STARTED_GAME_PROPS", "true").lower() == "true"
SCOREBOARD_CACHE_SECONDS = int(os.getenv("SCOREBOARD_CACHE_SECONDS", "20"))
STANDINGS_CACHE_SECONDS = int(os.getenv("STANDINGS_CACHE_SECONDS", "600"))


# Fast-refresh MLB cache settings.
PLAYER_SEARCH_CACHE_SECONDS = int(os.getenv("PLAYER_SEARCH_CACHE_SECONDS", "43200"))
PERSON_CACHE_SECONDS = int(os.getenv("PERSON_CACHE_SECONDS", "43200"))
GAME_LOG_CACHE_SECONDS = int(os.getenv("GAME_LOG_CACHE_SECONDS", "1800"))
TEAM_SCHEDULE_CACHE_SECONDS = int(os.getenv("TEAM_SCHEDULE_CACHE_SECONDS", "90"))
GAME_FEED_CACHE_SECONDS = int(os.getenv("GAME_FEED_CACHE_SECONDS", "20"))
PITCHER_STATS_CACHE_SECONDS = int(os.getenv("PITCHER_STATS_CACHE_SECONDS", "1800"))

# Manual Refresh Now optimization.
# Reuse the latest fully modeled row when player + market + line + matchup match.
FAST_REFRESH_REUSE_SECONDS = int(os.getenv("FAST_REFRESH_REUSE_SECONDS", "21600"))
