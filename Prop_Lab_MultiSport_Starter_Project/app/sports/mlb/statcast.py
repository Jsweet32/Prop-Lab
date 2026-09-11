import io, time
from datetime import date, timedelta
import requests
import pandas as pd

CSV_URL = "https://baseballsavant.mlb.com/leaderboard/custom"
_cache = {"time": 0, "season": None, "rows": {}}

def _num(row, *names):
    for n in names:
        if n in row and pd.notna(row[n]):
            try: return float(row[n])
            except Exception: pass
    return None

def fetch_statcast_leaderboard(season=None, cache_seconds=21600):
    """Fetch a Baseball Savant custom batting leaderboard CSV.
    Savant can change column names; parsing below is intentionally defensive.
    """
    season = season or date.today().year
    now = time.time()
    if _cache["season"] == season and now - _cache["time"] < cache_seconds:
        return _cache["rows"]

    params = {
        "year": season,
        "type": "batter",
        "filter": "",
        "sort": "4",
        "sortDir": "desc",
        "min": "1",
        "selections": ",".join([
            "pa","xba","xslg","xwoba","exit_velocity_avg","launch_angle_avg",
            "hard_hit_percent","barrel_batted_rate","whiff_percent"
        ]),
        "chart": "false",
        "x": "pa",
        "y": "pa",
        "r": "no",
        "csv": "true"
    }
    r = requests.get(CSV_URL, params=params, timeout=45)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    rows = {}
    for _, rr in df.iterrows():
        d = rr.to_dict()
        pid = d.get("player_id") or d.get("id")
        name = d.get("last_name, first_name") or d.get("player_name") or d.get("name")
        if pid is None:
            continue
        try: pid = int(pid)
        except Exception: continue
        rows[pid] = {
            "statcast_name": name,
            "xba": _num(d,"xba","est_ba","estimated_ba_using_speedangle"),
            "xslg": _num(d,"xslg","est_slg"),
            "xwoba": _num(d,"xwoba","est_woba"),
            "avg_exit_velocity": _num(d,"exit_velocity_avg","avg_hit_speed","launch_speed"),
            "avg_launch_angle": _num(d,"launch_angle_avg","avg_launch_angle","launch_angle"),
            "hard_hit_pct": _num(d,"hard_hit_percent","hardhit_percent","hard_hit_pct"),
            "barrel_pct": _num(d,"barrel_batted_rate","barrel_batted_rate_percent","barrel_pct"),
            "whiff_pct": _num(d,"whiff_percent","whiff_pct"),
        }
    _cache.update({"time": now, "season": season, "rows": rows})
    return rows

def statcast_adjustment(metrics, market_code):
    """Small bounded quality-of-contact adjustment.
    Expected stats remove some defense/park noise, but are context rather than a standalone probability.
    """
    if not metrics:
        return 0.0, "NONE"
    adj = 0.0
    xba = metrics.get("xba")
    xslg = metrics.get("xslg")
    hard = metrics.get("hard_hit_pct")
    barrel = metrics.get("barrel_pct")
    whiff = metrics.get("whiff_pct")

    # Normalize percentage fields if supplied as whole percentages.
    if hard is not None and hard > 1: hard /= 100
    if barrel is not None and barrel > 1: barrel /= 100
    if whiff is not None and whiff > 1: whiff /= 100

    if market_code == "H" and xba is not None:
        adj += max(-0.025, min(0.025, (xba - .250) * .16))
    elif market_code in {"TB","HR"}:
        if xslg is not None: adj += max(-0.025, min(0.025, (xslg - .420) * .08))
        if barrel is not None: adj += max(-0.020, min(0.020, (barrel - .075) * .25))
        if hard is not None: adj += max(-0.015, min(0.015, (hard - .40) * .10))
    elif market_code in {"R","BB"}:
        if xwoba := metrics.get("xwoba"):
            adj += max(-0.020, min(0.020, (xwoba - .320) * .10))

    return max(-0.04, min(0.04, adj)), "FULL" if any(v is not None for k,v in metrics.items() if k!="statcast_name") else "NONE"
