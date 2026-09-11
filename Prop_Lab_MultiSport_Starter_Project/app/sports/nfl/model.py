import math
from statistics import mean, median, pstdev, NormalDist
from .providers import nfl_season

# ParlayAPI uses canonical NFL market keys. Aliases cover older/alternate labels.
SUPPORTED = {
    "player_pass_yds": ("passing_yards", "Passing Yards", "pass_attempts"),
    "player_pass_tds": ("passing_tds", "Passing TDs", "pass_attempts"),
    "player_pass_completions": ("completions", "Pass Completions", "pass_attempts"),
    "player_pass_attempts": ("attempts", "Pass Attempts", "attempts"),
    "player_pass_interceptions": ("interceptions", "Interceptions", "pass_attempts"),
    "player_interceptions": ("interceptions", "Interceptions", "pass_attempts"),
    "player_rush_yds": ("rushing_yards", "Rushing Yards", "carries"),
    "player_rush_attempts": ("carries", "Rush Attempts", "carries"),
    "player_rush_tds": ("rushing_tds", "Rushing TDs", "carries"),
    "player_reception_yds": ("receiving_yards", "Receiving Yards", "targets"),
    "player_rec_yds": ("receiving_yards", "Receiving Yards", "targets"),
    "player_receptions": ("receptions", "Receptions", "targets"),
    "player_reception_tds": ("receiving_tds", "Receiving TDs", "targets"),
    "player_anytime_td": ("total_tds", "Anytime TD", "opportunities"),
}

def canonical_market_key(key, label=None):
    k = str(key or "").lower().strip()
    if k.endswith("_alternate"):
        k = k[:-10]
    aliases = {
        "passing yards": "player_pass_yds",
        "passing touchdowns": "player_pass_tds",
        "pass completions": "player_pass_completions",
        "pass attempts": "player_pass_attempts",
        "interceptions": "player_pass_interceptions",
        "rushing yards": "player_rush_yds",
        "rush attempts": "player_rush_attempts",
        "receiving yards": "player_reception_yds",
        "receptions": "player_receptions",
        "anytime touchdown": "player_anytime_td",
        "anytime td": "player_anytime_td",
    }
    if k in SUPPORTED:
        return k
    clean = str(label or "").lower().strip()
    return aliases.get(clean)

def american_implied(odds):
    if odds is None:
        return None
    try:
        odds = float(odds)
    except Exception:
        return None
    if odds == 0:
        return None
    return (-odds / (-odds + 100.0)) if odds < 0 else (100.0 / (odds + 100.0))

def _float(v, default=0.0):
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return default
        return float(v)
    except Exception:
        return default

def stat_value(row, stat_col):
    if stat_col == "total_tds":
        return (
            _float(row.get("passing_tds")) +
            _float(row.get("rushing_tds")) +
            _float(row.get("receiving_tds"))
        )
    return _float(row.get(stat_col))

def usage_value(row, usage_col):
    if usage_col == "opportunities":
        return _float(row.get("carries")) + _float(row.get("targets"))
    if usage_col == "pass_attempts":
        return _float(row.get("attempts"))
    return _float(row.get(usage_col))

def build_history_metrics(history, stat_col, usage_col, line):
    if not history:
        return None
    season = nfl_season()
    current = [r for r in history if int(r.get("season") or 0) == season and str(r.get("season_type") or "REG").upper().startswith("REG")]
    prior = [r for r in history if int(r.get("season") or 0) == season - 1 and str(r.get("season_type") or "REG").upper().startswith("REG")]
    recent = (prior + current)[-5:]

    def summarize(rows):
        vals = [stat_value(r, stat_col) for r in rows]
        vals = [v for v in vals if v is not None]
        if not vals:
            return {"rate": None, "n": 0, "avg": None, "sd": None, "values": []}
        wins = sum(1 for v in vals if v > float(line))
        # Beta smoothing prevents tiny-sample 0/100 estimates.
        rate = (wins + 1.5) / (len(vals) + 3.0)
        return {
            "rate": rate,
            "n": len(vals),
            "avg": mean(vals),
            "sd": pstdev(vals) if len(vals) > 1 else None,
            "values": vals,
        }

    sc, pr, re = summarize(current), summarize(prior), summarize(recent)
    all_rows = prior + current
    all_vals = [stat_value(r, stat_col) for r in all_rows]
    all_vals = [v for v in all_vals if v is not None]
    if not all_vals:
        return None

    # Distribution-based probability: how far the line is from the player's
    # historical center, tempered so one outlier does not dominate.
    center = mean(re["values"] or all_vals)
    sd = re["sd"] or (pstdev(all_vals) if len(all_vals) > 1 else max(abs(center) * .25, 1.0))
    sd = max(sd, max(abs(center) * .12, 0.75))
    dist_prob = 1.0 - NormalDist(mu=center, sigma=sd).cdf(float(line))

    weighted = []
    if pr["rate"] is not None:
        weighted.append((0.25, pr["rate"]))
    if sc["rate"] is not None:
        # Current-season evidence grows in importance as games accumulate.
        w = min(0.35, 0.12 + 0.04 * sc["n"])
        weighted.append((w, sc["rate"]))
    if re["rate"] is not None:
        weighted.append((0.25, re["rate"]))
    weighted.append((0.25, dist_prob))
    total_w = sum(w for w, _ in weighted)
    base = sum(w * p for w, p in weighted) / total_w

    # Usage is one of the strongest leading indicators for yardage/reception props.
    recent_usage = [usage_value(r, usage_col) for r in recent]
    baseline_usage = [usage_value(r, usage_col) for r in prior[-8:] or all_rows]
    recent_usage = [x for x in recent_usage if x is not None]
    baseline_usage = [x for x in baseline_usage if x is not None]
    usage_adj = 0.0
    if recent_usage and baseline_usage and mean(baseline_usage) > 0:
        ratio = mean(recent_usage) / mean(baseline_usage)
        usage_adj = max(-0.045, min(0.045, (ratio - 1.0) * 0.12))

    return {
        "base_prob": max(.04, min(.96, base)),
        "usage_adjustment": usage_adj,
        "season_rate": sc["rate"],
        "recent5_rate": re["rate"],
        "prior_rate": pr["rate"],
        "games_season": sc["n"],
        "games_recent5": re["n"],
        "games_prior": pr["n"],
        "season_average": sc["avg"],
        "recent5_average": re["avg"],
        "prior_average": pr["avg"],
        "sample_total": len(all_vals),
    }

def grade(edge, sample_total, confidence):
    if edge is None:
        return "", "NEEDS DATA"
    if sample_total < 6:
        return "", "WAIT"
    # A requires both a strong edge and reasonable supporting evidence.
    if edge >= .12 and confidence >= .62:
        return "A", "GOOD"
    if edge >= .08 and confidence >= .52:
        return "B", "GOOD"
    if edge >= .05:
        return "C", "BORDERLINE"
    return "PASS", "PASS"
