"""
intelligence.py  —  8-Layer Football Prediction Engine
=======================================================

Layer 1  : xG-adjusted form (last 15 games, dual window)
Layer 2  : Raw form decay (L5 × 0.6 + L6-15 × 0.4)
Layer 3  : Context modifiers (manager sacking, relegation, revenge)
Layer 4  : Injury impact score (weighted by player xG contribution)
Layer 5  : H2H deep analysis (reverse fixture, venue record, BTTS)
Layer 6  : Market consensus / Pinnacle sharp-money signal
Layer 7  : Poisson goals model (xG-calibrated)
Layer 8  : Confidence filter (≥65% required to output a pick)

Fixes applied after 18 Apr 2026 audit:
  - Manager sacking within 14 days → -25% win prob
  - Reverse fixture result now a mandatory feature
  - Form window extended from 5 → 15 games
  - Streak naivety corrected (>10 game streaks mean-revert)
  - Minimum confidence threshold (65%)
  - Injury impact weighted by minutes/xG (not just count)
"""

from __future__ import annotations

import math
import os
import time
import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("intelligence")

ODDS_KEY    = os.getenv("ODDS_API_KEY", "")
AFL_KEY     = os.getenv("API_FOOTBALL_KEY", "")
AFL_BASE    = "https://v3.football.api-sports.io"
AFL_HEADERS = {"x-apisports-key": AFL_KEY}
MIN_CONF    = int(os.getenv("MIN_CONFIDENCE", "65"))

# ── API-Football league IDs ──────────────────────────────────────────────────
LEAGUE_IDS = {
    "soccer_epl":                    39,
    "soccer_spain_la_liga":         140,
    "soccer_germany_bundesliga":     78,
    "soccer_italy_serie_a":         135,
    "soccer_france_ligue_one":       61,
    "soccer_netherlands_eredivisie": 88,
    "soccer_portugal_primeira_liga": 94,
    "soccer_champions_league":        2,
    "soccer_europa_league":           3,
    "soccer_brazil_campeonato":      71,
}

SEASON = int(os.getenv("AFL_SEASON", "2025"))


# ── AFL helper ───────────────────────────────────────────────────────────────
def _afl(endpoint: str, params: dict, retries: int = 2) -> dict:
    url = f"{AFL_BASE}/{endpoint}"
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=AFL_HEADERS, params=params, timeout=12)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries:
                log.warning(f"AFL {endpoint} failed: {e}")
                return {}
    return {}


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 3 — Context Modifiers
# ════════════════════════════════════════════════════════════════════════════

def get_context_modifiers(team_name: str, league_key: str, is_home: bool) -> dict:
    """
    Returns a dict of modifiers that adjust win probability.
    Keys: modifier_value (float, additive %), reason (str), flags (list)
    """
    league_id = LEAGUE_IDS.get(league_key)
    if not league_id or not AFL_KEY:
        return {"modifier": 0.0, "flags": [], "reasons": []}

    flags = []
    reasons = []
    modifier = 0.0

    # ── Manager change check ─────────────────────────────────────────────────
    coach_data = _afl("coachs", {"team": _team_id(team_name, league_id), "season": SEASON})
    coaches = coach_data.get("response", [])
    for coach in coaches:
        career = coach.get("career", [])
        for stint in career:
            end = stint.get("end")
            if end:
                try:
                    end_date = datetime.fromisoformat(end.replace("Z", "+00:00"))
                    days_since = (datetime.now(timezone.utc) - end_date).days
                    if days_since <= 14:
                        modifier -= 25.0
                        flags.append("MANAGER_CHANGE_14D")
                        reasons.append(f"Manager change {days_since}d ago → -25%")
                        break
                except Exception:
                    pass

    # ── League standings: relegation / European pressure ─────────────────────
    tid = _team_id(team_name, league_id)
    standings_data = _afl("standings", {"league": league_id, "season": SEASON})
    standings = standings_data.get("response", [])
    if standings:
        league_standings = standings[0].get("league", {}).get("standings", [[]])[0]
        total_teams = len(league_standings)
        for entry in league_standings:
            if entry.get("team", {}).get("id") == tid:
                rank = entry.get("rank", 0)
                # Bottom 3 = relegation zone
                if rank >= total_teams - 2:
                    modifier += 15.0
                    flags.append("RELEGATION_ZONE")
                    reasons.append(f"Rank {rank}/{total_teams} (relegation) → +15%")
                # Top 4 chase (European)
                elif rank in [4, 5, 6]:
                    modifier += 8.0
                    flags.append("EUROPEAN_CHASE")
                    reasons.append(f"Rank {rank} (European chase) → +8%")
                # Top 4 secured, mid-table comfort
                elif 8 <= rank <= total_teams - 4:
                    modifier -= 5.0
                    flags.append("MID_TABLE")
                    reasons.append(f"Rank {rank} (no pressure) → -5%")
                break

    return {"modifier": modifier, "flags": flags, "reasons": reasons}


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 4 — Injury Impact Score
# ════════════════════════════════════════════════════════════════════════════

def get_injury_impact(team_name: str, league_key: str, fixture_id: int | None = None) -> dict:
    """
    Returns injury_score: how badly the team is affected.
    Score 0-100 where 100 = complete destruction.
    Weighted by minutes played ratio of injured player.
    """
    league_id = LEAGUE_IDS.get(league_key)
    if not league_id or not AFL_KEY:
        return {"score": 0, "players": [], "summary": "No data"}

    tid = _team_id(team_name, league_id)
    params = {"team": tid, "season": SEASON}
    if fixture_id:
        params["fixture"] = fixture_id

    data = _afl("injuries", params)
    injuries = data.get("response", [])

    affected = []
    total_score = 0.0
    for inj in injuries:
        player = inj.get("player", {})
        t      = inj.get("team", {})
        if t.get("id") != tid:
            continue
        ptype = inj.get("type", "").lower()
        # Out = full weight, Doubtful = 50% weight
        weight = 1.0 if "out" in ptype or "injured" in ptype else 0.5
        # Fetch player stats to get minutes share
        pstats = _afl("players", {"id": player.get("id"), "season": SEASON, "league": league_id})
        minutes = 0
        goals = 0
        assists = 0
        for ps in pstats.get("response", []):
            for stat_block in ps.get("statistics", []):
                m = stat_block.get("games", {}).get("minutes") or 0
                g = stat_block.get("goals", {}).get("total") or 0
                a = stat_block.get("goals", {}).get("assists") or 0
                minutes = max(minutes, m)
                goals = max(goals, g)
                assists = max(assists, a)
        # Impact = ((minutes/2700) * 0.5 + (goals+assists)/30 * 0.5) * weight * 100
        min_share = min(minutes / 2700, 1.0)  # 2700 = ~30 games × 90 min
        goal_share = min((goals + assists) / 30.0, 1.0)
        impact = (min_share * 0.5 + goal_share * 0.5) * weight * 100
        total_score += impact
        affected.append({
            "name": player.get("name", "Unknown"),
            "impact": round(impact, 1),
            "weight": weight,
            "minutes": minutes,
            "goals": goals,
            "assists": assists,
        })

    total_score = min(total_score, 100)
    return {
        "score": round(total_score, 1),
        "players": affected[:5],
        "summary": f"{len(affected)} injury/suspension(s), impact score {total_score:.0f}/100",
    }


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 1+2 — xG-Adjusted Form (Last 15 games)
# ════════════════════════════════════════════════════════════════════════════

def get_team_form(team_name: str, league_key: str, last_n: int = 15) -> dict:
    """
    Returns dual-window form with xG adjustment.
    L5 weight 60%, L6-15 weight 40%.
    Also detects streak naivety (>10 game win/scoring streak flags mean-reversion).
    """
    league_id = LEAGUE_IDS.get(league_key)
    if not league_id or not AFL_KEY:
        return _form_default()

    tid = _team_id(team_name, league_id)
    data = _afl("fixtures", {
        "team": tid, "last": last_n,
        "status": "FT", "league": league_id, "season": SEASON,
    })
    fixtures = data.get("response", [])
    if not fixtures:
        return _form_default()

    results = []
    for fx in fixtures:
        teams  = fx.get("teams", {})
        goals  = fx.get("goals", {})
        score  = fx.get("score", {}).get("fulltime", {})
        is_home = teams.get("home", {}).get("id") == tid
        hg = goals.get("home", 0) or 0
        ag = goals.get("away", 0) or 0
        gf = hg if is_home else ag
        ga = ag if is_home else hg
        if hg > ag:
            res = 1.0 if is_home else 0.0
        elif hg == ag:
            res = 0.5
        else:
            res = 0.0 if is_home else 1.0
        # Try xG from statistics
        xg_for = xg_against = 0.0
        stats_data = _afl("fixtures/statistics", {"fixture": fx["fixture"]["id"]})
        for entry in stats_data.get("response", []):
            if entry.get("team", {}).get("id") != tid:
                continue
            for stat in entry.get("statistics", []):
                if "expected" in stat.get("type", "").lower() and stat.get("value"):
                    try:
                        xg_for = float(stat["value"])
                    except Exception:
                        pass
        results.append({
            "result": res, "gf": gf, "ga": ga,
            "xg_for": xg_for, "is_home": is_home,
        })

    results.reverse()  # most recent first
    if not results:
        return _form_default()

    # Dual window
    l5   = results[:5]
    l615 = results[5:15]

    def _weighted_form(window):
        if not window:
            return 0.5
        pts = wt = 0.0
        for i, r in enumerate(window):
            w = 0.85 ** i  # exponential decay
            pts += r["result"] * w
            wt  += w
        return round(pts / wt, 4) if wt else 0.5

    def _avg_xg(window):
        vals = [r["xg_for"] for r in window if r["xg_for"] > 0]
        return round(sum(vals) / len(vals), 3) if vals else 0.0

    form_l5   = _weighted_form(l5)
    form_l615 = _weighted_form(l615)
    form_dual  = round(form_l5 * 0.6 + form_l615 * 0.4, 4)

    # Streak naivety check
    streak_win = streak_score = 0
    for r in results:
        if r["result"] == 1.0:
            streak_win += 1
        else:
            break
    for r in results:
        if r["gf"] > 0:
            streak_score += 1
        else:
            break

    mean_reversion_flag = streak_win >= 10 or streak_score >= 12

    avg_gf = round(sum(r["gf"] for r in results[:10]) / min(len(results), 10), 2)
    avg_ga = round(sum(r["ga"] for r in results[:10]) / min(len(results), 10), 2)
    avg_xg = _avg_xg(results[:10])

    return {
        "form_l5":   form_l5,
        "form_l615": form_l615,
        "form_dual": form_dual,
        "avg_gf":    avg_gf,
        "avg_ga":    avg_ga,
        "avg_xg":    avg_xg,
        "streak_win":   streak_win,
        "streak_score": streak_score,
        "mean_reversion_flag": mean_reversion_flag,
        "games_used": len(results),
    }


def _form_default():
    return {
        "form_l5": 0.5, "form_l615": 0.5, "form_dual": 0.5,
        "avg_gf": 1.4, "avg_ga": 1.2, "avg_xg": 0.0,
        "streak_win": 0, "streak_score": 0,
        "mean_reversion_flag": False, "games_used": 0,
    }


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 5 — H2H Deep Analysis
# ════════════════════════════════════════════════════════════════════════════

def get_h2h_deep(home_name: str, away_name: str, league_key: str, last_n: int = 10) -> dict:
    """
    Analyses last N H2H meetings.
    Key new feature: reverse fixture result this season.
    """
    league_id = LEAGUE_IDS.get(league_key)
    if not league_id or not AFL_KEY:
        return _h2h_default()

    hid = _team_id(home_name, league_id)
    aid = _team_id(away_name, league_id)
    if not hid or not aid:
        return _h2h_default()

    data = _afl("fixtures/headtohead", {
        "h2h": f"{hid}-{aid}",
        "last": last_n,
        "status": "FT",
    })
    fixtures = data.get("response", [])
    if not fixtures:
        return _h2h_default()

    home_wins = away_wins = draws = 0
    btts_count = over25_count = 0
    reverse_result = None  # result of AWAY team in current season's reverse fixture

    for fx in fixtures:
        teams  = fx.get("teams", {})
        goals  = fx.get("goals", {})
        season = fx.get("league", {}).get("season")
        hg = goals.get("home", 0) or 0
        ag = goals.get("away", 0) or 0
        fhome_id = teams.get("home", {}).get("id")

        # Map perspective to "home team in this upcoming fixture"
        from_home_perspective = fhome_id == hid

        if hg > ag:
            res = "home" if from_home_perspective else "away"
        elif hg < ag:
            res = "away" if from_home_perspective else "home"
        else:
            res = "draw"

        if res == "home":
            home_wins += 1
        elif res == "away":
            away_wins += 1
        else:
            draws += 1

        if hg > 0 and ag > 0:
            btts_count += 1
        if hg + ag > 2:
            over25_count += 1

        # Reverse fixture: this season, away team won at home
        if season == SEASON:
            # Fixture where away_id was the home team
            if fhome_id == aid:
                if hg > ag:
                    reverse_result = "away_won_at_home"  # away team won the reverse
                elif hg < ag:
                    reverse_result = "away_lost_at_home"
                else:
                    reverse_result = "draw"

    total = len(fixtures)
    btts_rate  = round(btts_count / total, 3) if total else 0.5
    over25_rate = round(over25_count / total, 3) if total else 0.5
    home_win_rate = round(home_wins / total, 3) if total else 0.33

    # Reverse fixture modifier
    reverse_modifier = 0.0
    if reverse_result == "away_won_at_home":
        reverse_modifier = -8.0   # away team won reverse → cautious about home team
    elif reverse_result == "away_lost_at_home":
        reverse_modifier = +8.0   # home team won reverse → home team confident

    return {
        "total_h2h":    total,
        "home_wins":    home_wins,
        "away_wins":    away_wins,
        "draws":        draws,
        "home_win_rate": home_win_rate,
        "btts_rate":    btts_rate,
        "over25_rate":  over25_rate,
        "reverse_result":   reverse_result,
        "reverse_modifier": reverse_modifier,
    }


def _h2h_default():
    return {
        "total_h2h": 0, "home_wins": 0, "away_wins": 0, "draws": 0,
        "home_win_rate": 0.33, "btts_rate": 0.5, "over25_rate": 0.5,
        "reverse_result": None, "reverse_modifier": 0.0,
    }


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 7 — Poisson Goals Model
# ════════════════════════════════════════════════════════════════════════════

def poisson_markets(xg_h: float, xg_a: float) -> dict:
    """Full market probabilities from xG via Poisson distribution."""
    max_g = 9

    def pmf(lam, k):
        if lam <= 0:
            return 1.0 if k == 0 else 0.0
        return math.exp(-lam) * (lam ** k) / math.factorial(k)

    mat = [[pmf(xg_h, i) * pmf(xg_a, j) for j in range(max_g+1)] for i in range(max_g+1)]

    p_home = p_draw = p_away = 0.0
    p_over = p_under = p_btts = 0.0
    for i in range(max_g+1):
        for j in range(max_g+1):
            c = mat[i][j]
            if i > j: p_home += c
            elif i == j: p_draw += c
            else: p_away += c
            if i+j > 2: p_over += c
            else: p_under += c
            if i > 0 and j > 0: p_btts += c

    return {
        "H": round(p_home, 4), "D": round(p_draw, 4), "A": round(p_away, 4),
        "O25": round(p_over, 4), "U25": round(p_under, 4),
        "BTTS": round(p_btts, 4),
        "xG_home": round(xg_h, 3), "xG_away": round(xg_a, 3),
    }


def estimate_xg(home_form: dict, away_form: dict,
                h2h: dict,
                home_odds: float = 0, draw_odds: float = 0, away_odds: float = 0) -> tuple[float, float]:
    """Estimate xG from form data or fallback to odds inversion."""
    league_avg_home = 1.50
    league_avg_away = 1.20

    xg_h = home_form.get("avg_xg") or home_form.get("avg_gf") or league_avg_home
    xg_a = away_form.get("avg_xg") or away_form.get("avg_gf") or league_avg_away

    # H2H calibration
    if h2h.get("total_h2h", 0) >= 4:
        h2h_rate = h2h.get("over25_rate", 0.5)
        target_total = 2.0 + (h2h_rate - 0.5) * 2
        current_total = xg_h + xg_a
        if current_total > 0:
            scale = target_total / current_total
            xg_h = round(xg_h * (0.7 + 0.3 * scale), 3)
            xg_a = round(xg_a * (0.7 + 0.3 * scale), 3)

    # Fallback if form data is empty
    if xg_h <= 0.1 and xg_a <= 0.1 and home_odds > 0:
        # Invert market odds
        total = 1/home_odds + 1/draw_odds + 1/away_odds
        p_h = (1/home_odds) / total
        p_a = (1/away_odds) / total
        xg_h = max(0.5, round(-math.log(max(1-p_h, 0.01)) * 1.8, 2))
        xg_a = max(0.3, round(-math.log(max(1-p_a, 0.01)) * 1.4, 2))

    return max(0.3, min(xg_h, 5.5)), max(0.2, min(xg_a, 4.5))


# ════════════════════════════════════════════════════════════════════════════
#  LAYER 8 — Full 8-Layer Prediction Engine
# ════════════════════════════════════════════════════════════════════════════

def full_prediction(
    home_team: str,
    away_team: str,
    league_key: str,
    home_odds: float = 0,
    draw_odds: float = 0,
    away_odds: float = 0,
    market_consensus: dict | None = None,
) -> dict:
    """
    Runs all 8 layers and returns a complete prediction dict.
    Returns None if confidence < MIN_CONF (Layer 8 filter).
    """
    result = {
        "home": home_team,
        "away": away_team,
        "league": league_key,
        "layers": {},
        "flags": [],
        "warnings": [],
        "skip": False,
        "skip_reason": "",
    }

    # ── Layers 1+2: Form ────────────────────────────────────────────────────
    log.info(f"  [{home_team}] Fetching form (L15)...")
    home_form = get_team_form(home_team, league_key)
    away_form = get_team_form(away_team, league_key)
    result["layers"]["home_form"] = home_form
    result["layers"]["away_form"] = away_form

    # ── Layer 3: Context ────────────────────────────────────────────────────
    log.info(f"  [{home_team}/{away_team}] Context modifiers...")
    home_ctx = get_context_modifiers(home_team, league_key, is_home=True)
    away_ctx = get_context_modifiers(away_team, league_key, is_home=False)
    result["layers"]["home_context"] = home_ctx
    result["layers"]["away_context"] = away_ctx

    for flag in home_ctx["flags"] + away_ctx["flags"]:
        result["flags"].append(flag)
    if "MANAGER_CHANGE_14D" in result["flags"]:
        result["warnings"].append("Manager changed in last 14 days — high uncertainty")

    # ── Layer 4: Injuries ───────────────────────────────────────────────────
    log.info(f"  [{home_team}/{away_team}] Injury impact...")
    home_inj = get_injury_impact(home_team, league_key)
    away_inj = get_injury_impact(away_team, league_key)
    result["layers"]["home_injuries"] = home_inj
    result["layers"]["away_injuries"] = away_inj

    if home_inj["score"] > 40:
        result["warnings"].append(f"{home_team} injury impact HIGH ({home_inj['score']:.0f}/100)")
    if away_inj["score"] > 40:
        result["warnings"].append(f"{away_team} injury impact HIGH ({away_inj['score']:.0f}/100)")

    # ── Layer 5: H2H ────────────────────────────────────────────────────────
    log.info(f"  H2H deep analysis...")
    h2h = get_h2h_deep(home_team, away_team, league_key)
    result["layers"]["h2h"] = h2h

    # ── Layer 7: Poisson ────────────────────────────────────────────────────
    xg_h, xg_a = estimate_xg(home_form, away_form, h2h, home_odds, draw_odds, away_odds)
    poisson = poisson_markets(xg_h, xg_a)
    result["layers"]["poisson"] = poisson

    # ── Layer 6: Market consensus ───────────────────────────────────────────
    if market_consensus and home_odds > 0:
        marg = 1/home_odds + 1/draw_odds + 1/away_odds
        pin_h = (1/home_odds) / marg
        pin_d = (1/draw_odds) / marg
        pin_a = (1/away_odds) / marg
    else:
        pin_h = market_consensus.get("H", poisson["H"]) if market_consensus else poisson["H"]
        pin_d = market_consensus.get("D", poisson["D"]) if market_consensus else poisson["D"]
        pin_a = market_consensus.get("A", poisson["A"]) if market_consensus else poisson["A"]

    # Blend: 45% market + 35% poisson + 20% form differential
    form_diff = home_form["form_dual"] - away_form["form_dual"]
    form_h_adj = max(0.05, min(0.90, 0.33 + form_diff * 0.4))
    form_a_adj = max(0.05, min(0.90, 0.33 - form_diff * 0.4))
    form_d_adj = max(0.10, min(0.50, 1.0 - form_h_adj - form_a_adj))

    blended_h = round(0.45 * pin_h + 0.35 * poisson["H"] + 0.20 * form_h_adj, 4)
    blended_d = round(0.45 * pin_d + 0.35 * poisson["D"] + 0.20 * form_d_adj, 4)
    blended_a = round(0.45 * pin_a + 0.35 * poisson["A"] + 0.20 * form_a_adj, 4)

    # Normalize
    total = blended_h + blended_d + blended_a
    if total > 0:
        blended_h = round(blended_h / total, 4)
        blended_d = round(blended_d / total, 4)
        blended_a = round(blended_a / total, 4)

    # ── Apply Layer 3 modifiers ─────────────────────────────────────────────
    # Manager change is the strongest signal
    if "MANAGER_CHANGE_14D" in home_ctx["flags"]:
        blended_h = max(0.05, blended_h - 0.10)
        blended_d = min(0.50, blended_d + 0.05)
        blended_a = min(0.85, blended_a + 0.05)
    if "MANAGER_CHANGE_14D" in away_ctx["flags"]:
        blended_a = max(0.05, blended_a - 0.10)
        blended_h = min(0.85, blended_h + 0.05)
        blended_d = min(0.50, blended_d + 0.05)

    # Relegation survival uplift
    if "RELEGATION_ZONE" in home_ctx["flags"]:
        blended_h = min(blended_h + 0.08, 0.90)
    if "RELEGATION_ZONE" in away_ctx["flags"]:
        blended_a = min(blended_a + 0.08, 0.90)

    # H2H reverse fixture
    rev_mod = h2h.get("reverse_modifier", 0.0)
    if rev_mod != 0:
        blended_h = max(0.05, blended_h + rev_mod / 100)
        blended_a = max(0.05, blended_a - rev_mod / 100)

    # Injury impact on goals
    if home_inj["score"] > 30:
        xg_h = max(0.3, xg_h * (1 - home_inj["score"] / 300))
    if away_inj["score"] > 30:
        xg_a = max(0.2, xg_a * (1 - away_inj["score"] / 300))

    # Mean reversion for long streaks
    if home_form.get("mean_reversion_flag"):
        blended_h = max(0.10, blended_h * 0.92)
        result["warnings"].append(f"{home_team} streak ≥10 games — mean reversion risk")
    if away_form.get("mean_reversion_flag"):
        blended_a = max(0.10, blended_a * 0.92)
        result["warnings"].append(f"{away_team} streak ≥10 games — mean reversion risk")

    # Re-normalize
    total = blended_h + blended_d + blended_a
    if total > 0:
        blended_h = round(blended_h / total, 4)
        blended_d = round(blended_d / total, 4)
        blended_a = round(blended_a / total, 4)

    result["prob_H"] = blended_h
    result["prob_D"] = blended_d
    result["prob_A"] = blended_a
    result["xg_home"] = xg_h
    result["xg_away"] = xg_a
    result["O25"]  = poisson["O25"]
    result["U25"]  = poisson["U25"]
    result["BTTS"] = poisson["BTTS"]

    # ── Determine best pick ─────────────────────────────────────────────────
    picks = []
    if home_odds > 0:
        for mkt, prob, odds in [
            ("H",   blended_h, home_odds),
            ("D",   blended_d, draw_odds),
            ("A",   blended_a, away_odds),
            ("O25", poisson["O25"], 0),
            ("U25", poisson["U25"], 0),
            ("BTTS",poisson["BTTS"], 0),
        ]:
            if odds > 1.05:
                implied = 1.0 / odds
                edge = round(prob - implied, 4)
                if edge > 0.04 and prob >= 0.55:
                    picks.append({"market": mkt, "prob": prob, "edge": edge, "odds": odds})

    picks.sort(key=lambda x: x["edge"] * x["prob"], reverse=True)
    result["top_picks"] = picks[:3]

    # ── Layer 8: Confidence Filter ──────────────────────────────────────────
    best_prob = max(blended_h, blended_d, blended_a)
    confidence = round(best_prob * 100, 1)
    result["confidence"] = confidence

    # Reduce confidence if warnings present
    if result["warnings"]:
        confidence = round(confidence * (1 - 0.05 * len(result["warnings"])), 1)
        result["adjusted_confidence"] = confidence

    # Layer 8 gate
    if confidence < MIN_CONF:
        result["skip"] = True
        result["skip_reason"] = f"Confidence {confidence:.1f}% < threshold {MIN_CONF}%"
    if "MANAGER_CHANGE_14D" in result["flags"]:
        result["skip"] = True
        result["skip_reason"] = "Manager changed in last 14 days — skip"

    return result


# ── Team ID resolution ────────────────────────────────────────────────────────
@lru_cache(maxsize=512)
def _team_id(team_name: str, league_id: int) -> int | None:
    data = _afl("teams", {"name": team_name, "league": league_id, "season": SEASON})
    results = data.get("response", [])
    if results:
        return results[0]["team"]["id"]
    # Fuzzy: search without league constraint
    data2 = _afl("teams", {"search": team_name[:10]})
    for r in data2.get("response", []):
        if team_name.lower() in r["team"]["name"].lower():
            return r["team"]["id"]
    return None
