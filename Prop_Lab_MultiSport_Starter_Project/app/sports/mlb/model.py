from datetime import datetime, timedelta, timezone

SUPPORTED = {
    "player_hits": ("hitting","hits","H"),
    "batter_hits": ("hitting","hits","H"),
    "player_total_bases": ("hitting","totalBases","TB"),
    "batter_total_bases": ("hitting","totalBases","TB"),
    "player_home_runs": ("hitting","homeRuns","HR"),
    "batter_home_runs": ("hitting","homeRuns","HR"),
    "player_rbis": ("hitting","rbi","R"),
    "batter_rbis": ("hitting","rbi","R"),
    "player_runs": ("hitting","runs","R"),
    "batter_runs": ("hitting","runs","R"),
    "player_walks": ("hitting","baseOnBalls","BB"),
    "batter_walks": ("hitting","baseOnBalls","BB"),
    "player_strikeouts": ("pitching","strikeOuts","SO"),
    "pitcher_strikeouts": ("pitching","strikeOuts","SO"),
}

PARK_FACTOR_OVERRIDES = {
    "Coors Field": 1.12,
    "Chase Field": 1.04,
    "Fenway Park": 1.03,
    "Sutter Health Park": 1.08,
    "Nationals Park": 1.04,
}

def american_implied(odds):
    if odds is None: return None
    odds=float(odds)
    if odds==0: return None
    return (-odds/(-odds+100.0)) if odds<0 else (100.0/(odds+100.0))

def split_date(split):
    raw=split.get("date") or split.get("game",{}).get("gameDate")
    if not raw:return None
    try:return datetime.fromisoformat(raw.replace("Z","+00:00")).date()
    except Exception:
        try:return datetime.strptime(raw[:10],"%Y-%m-%d").date()
        except Exception:return None

def stat_value(split, stat_key):
    stat=split.get("stat",{})
    if stat_key=="totalBases":
        if "totalBases" in stat:return float(stat.get("totalBases") or 0)
        singles=float(stat.get("hits",0))-float(stat.get("doubles",0))-float(stat.get("triples",0))-float(stat.get("homeRuns",0))
        return singles+2*float(stat.get("doubles",0))+3*float(stat.get("triples",0))+4*float(stat.get("homeRuns",0))
    return float(stat.get(stat_key,0) or 0)

def rates_from_game_log(splits, stat_key, line):
    today=datetime.now(timezone.utc).date()
    valid=[]
    for s in splits:
        d=split_date(s)
        if d is not None:
            valid.append((d,stat_value(s,stat_key)))
    valid.sort(key=lambda x:x[0])
    if not valid:return None

    def rate(rows):
        if not rows:return None,0
        wins=sum(1 for _,v in rows if v>float(line))
        # beta smoothing prevents tiny samples from producing literal 0/100
        return (wins+1)/(len(rows)+2),len(rows)

    sr,ns=rate(valid)
    r30,n30=rate([x for x in valid if x[0]>=today-timedelta(days=30)])
    r10,n10=rate(valid[-10:])
    return {"season_rate":sr,"l30_rate":r30,"l10_rate":r10,
            "games_season":ns,"games_l30":n30,"games_l10":n10}

def weighted_probability(rates):
    if not rates:return None
    parts=[]
    if rates["season_rate"] is not None:parts.append((.50,rates["season_rate"]))
    if rates["l30_rate"] is not None:parts.append((.30,rates["l30_rate"]))
    if rates["l10_rate"] is not None:parts.append((.20,rates["l10_rate"]))
    if not parts:return None
    tw=sum(w for w,_ in parts)
    # 3%-97% cap; enough room for strong props while avoiding literal certainty.
    return min(.97,max(.03,sum(w*v for w,v in parts)/tw))

def park_factor(venue):
    return PARK_FACTOR_OVERRIDES.get(venue,1.0)

def pitcher_quality_adjustment(pitcher_stats, market_code):
    if not pitcher_stats:return 0.0
    try:era=float(pitcher_stats.get("era"))
    except Exception:era=None
    try:whip=float(pitcher_stats.get("whip"))
    except Exception:whip=None
    adj=0.0
    if market_code in {"H","TB","HR","R","BB"}:
        if era is not None:adj += max(-.025,min(.025,(era-4.20)*.008))
        if whip is not None:adj += max(-.020,min(.020,(whip-1.30)*.05))
    return max(-.04,min(.04,adj))

def handedness_adjustment(player_hand,pitcher_hand):
    if not player_hand or not pitcher_hand or player_hand=="S":return 0.0
    return -.01 if player_hand==pitcher_hand else .01

def apply_context(base_prob,market_code,venue=None,player_hand=None,pitcher_hand=None,pitcher_stats=None):
    if base_prob is None:return None,0.0,1.0,"LOW"
    pf=park_factor(venue)
    park_adj=max(-.03,min(.03,(pf-1.0)*.25)) if market_code!="SO" else 0.0
    pit_adj=pitcher_quality_adjustment(pitcher_stats,market_code)
    hand_adj=handedness_adjustment(player_hand,pitcher_hand) if market_code!="SO" else 0.0
    total=max(-.07,min(.07,park_adj+pit_adj+hand_adj))
    confidence="HIGH" if venue and pitcher_stats and pitcher_hand else ("MEDIUM" if venue or pitcher_stats else "LOW")
    return min(.97,max(.03,base_prob+total)),total,pf,confidence

def grade_from_edge(edge):
    if edge is None:return ("","NEEDS DATA")
    if edge>=.08:return ("A","GOOD")
    if edge>=.05:return ("B","GOOD")
    if edge>=.02:return ("C","BORDERLINE")
    return ("PASS","PASS")

def canonical_market_key(key, label):
    raw=f"{key or ''} {label or ''}".lower().replace("_"," ").strip()

    # These require pitch-by-pitch / inning-specific modeling and MUST NOT
    # be projected from full-game strikeout or batting game logs.
    special=[
        "1st inn","first inn","1st inning","first inning",
        "2nd inn","second inn","inning strikeout","inning strikeouts",
        "first 5","1st 5"," f5 ","plate appearance","first plate appearance",
    ]
    if any(token in raw for token in special):
        return None

    aliases={
        "strikeouts":"pitcher_strikeouts",
        "pitcher strikeouts":"pitcher_strikeouts",
        "hits":"player_hits",
        "total bases":"player_total_bases",
        "home runs":"player_home_runs",
        "rbi":"player_rbis",
        "rbis":"player_rbis",
        "runs":"player_runs",
        "walks":"player_walks",
    }
    clean=(label or key or "").lower().replace("_"," ").strip()
    if clean in aliases:return aliases[clean]
    return key

def dfs_model_edge(recommended_prob):
    if recommended_prob is None:return None
    # directional margin over a neutral 50% model baseline
    return max(0.0,recommended_prob-.50)

def sportsbook_grade(edge):
    if edge is None:
        return "", "NEEDS DATA"

    if edge >= 0.08:
        return "A", "GOOD"

    if edge >= 0.05:
        return "B", "GOOD"

    if edge >= 0.02:
        return "C", "BORDERLINE"

    return "PASS", "PASS"
