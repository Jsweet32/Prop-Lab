import os
from dotenv import load_dotenv

load_dotenv()

PARLAY_API_KEY = os.getenv("PARLAY_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATABASE_PATH = os.getenv("NFL_DATABASE_PATH", "nfl_props.db")
TIMEZONE = os.getenv("TIMEZONE", "America/New_York")

SPORT_KEY = "americanfootball_nfl"
# NFL Underdog is temporarily disabled until its native primary-line feed can
# be verified against the app. MLB Underdog is completely unaffected.
BOOKMAKERS = ["prizepicks", "prizepicks_mobile", "fliff", "kalshi"]
BOOK_TITLES = {
    "prizepicks": "PrizePicks",
    "prizepicks_mobile": "PrizePicks",
    "fliff": "Fliff",
    "kalshi": "Kalshi",
}

DFS_BOOKS = {"PrizePicks"}
LINE_MAX_AGE_SECONDS = int(os.getenv("NFL_LINE_MAX_AGE_SECONDS", "3600"))
UPCOMING_HOURS = int(os.getenv("NFL_UPCOMING_HOURS", "168"))

# Slightly conservative because NFL has far fewer games than MLB.
MIN_HISTORY_GAMES = int(os.getenv("NFL_MIN_HISTORY_GAMES", "6"))
A_EDGE = float(os.getenv("NFL_A_EDGE", "0.12"))
B_EDGE = float(os.getenv("NFL_B_EDGE", "0.08"))
C_EDGE = float(os.getenv("NFL_C_EDGE", "0.05"))

NFLVERSE_CACHE_SECONDS = int(os.getenv("NFLVERSE_CACHE_SECONDS", "900"))
SCOREBOARD_CACHE_SECONDS = int(os.getenv("NFL_SCOREBOARD_CACHE_SECONDS", "60"))
