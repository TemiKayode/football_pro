"""
data_enricher.py — Build fully enriched feature dicts for each match.

Data sources (all configurable via .env):
  API-Football (https://www.api-football.com) — free tier: 100 req/day
    → recent fixtures + stats, injuries, lineups, H2H
  Understat (free, scraped) — xG per match
  ELO system (elo.py) — built from your historical CSV

Set API_FOOTBALL_KEY in .env.
Set HISTORICAL_CSV in .env pointing to your historical data CSV.
"""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from functools import lru_cache

import requests
from dotenv import load_dotenv

load_dotenv()

_API_KEY = os.getenv("API_FOOTBALL_KEY", "")
_BASE = "https://v3.football.api-sports.io"
_HEADERS = {"x-apisports-key": _API_KEY}
_HISTORICAL_CSV = os.getenv("HISTORICAL_CSV", "data.csv")

# Optional: preload ELO ratings once at import time
_elo = None


def _get_elo():
    global _elo
    if _elo is None:
        from elo import EloRatings
        try:
            _elo = EloRatings(_HISTORICAL_CSV)
        except Exception:
            _elo = EloRatings()  # empty — returns default ELO 1500
    return _elo


def _api_get(endpoint: str, params: dict) -> dict:
    """Thin wrapper around API-Football with basic rate-limit backoff."""
    url = f"{_BASE}/{endpoint}"
    for attempt in range(3):
        resp = requests.get(url, headers=_HEADERS, params=params, timeout=15)
        if resp.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"API-Football {endpoint} failed after 3 attempts")


# ── Team ID resolution ──────────────────────────────────────────────────────

@lru_cache(maxsize=256)
def _team_id(team_name: str) -> int | None:
    data = _api_get("teams", {"name": team_name, "league": os.getenv("LEAGUE_ID", "39"),
                               "season": os.getenv("SEASON", "2024")})
    results = data.get("response", [])
    return results[0]["team"]["id"] if results else None


# ── xG (Expected Goals) ─────────────────────────────────────────────────────

def _xg_from_stats(stats: list[dict], team_id: int) -> float:
    """
    Extract xG from fixture statistics. API-Football provides xG directly
    on paid plans; on free plans we proxy via shots on target.
    """
    for entry in stats:
        if entry.get("team", {}).get("id") != team_id:
            continue
        for stat in entry.get("statistics", []):
            if stat["type"] == "expected_goals" and stat["value"] is not None:
                return float(stat["value"])
            # Proxy: shots on target * 0.35 (rough xG estimate)
            if stat["type"] == "Shots on Goal" and stat["value"] is not None:
                return round(float(stat["value"]) * 0.35, 2)
    return 0.0


def _last5_xg(team_id: int) -> float:
    """Average xG over the last 5 fixtures for a team."""
    data = _api_get("fixtures", {
        "team": team_id,
        "last": 5,
        "status": "FT",
    })
    fixtures = data.get("response", [])
    xg_values = []
    for fx in fixtures:
        stats_data = _api_get("fixtures/statistics", {"fixture": fx["fixture"]["id"]})
        xg = _xg_from_stats(stats_data.get("response", []), team_id)
        xg_values.append(xg)
    return round(sum(xg_values) / len(xg_values), 3) if xg_values else 0.0


# ── Weighted form ───────────────────────────────────────────────────────────

def _weighted_form(team_id: int, last_n: int = 5, decay: float = 0.8) -> float:
    """
    Exponentially decayed form over last N matches.
    Win=1, Draw=0.5, Loss=0. Most recent match has highest weight.
    Returns value in [0, 1].
    """
    data = _api_get("fixtures", {
        "team": team_id,
        "last": last_n,
        "status": "FT",
    })
    fixtures = data.get("response", [])
    if not fixtures:
        return 0.5  # neutral default

    points, weight_sum = 0.0, 0.0
    for i, fx in enumerate(reversed(fixtures)):  # most recent first
        teams = fx["teams"]
        home_id = teams["home"]["id"]
        goals = fx["goals"]
        home_goals, away_goals = goals["home"], goals["away"]
        if home_goals is None or away_goals is None:
            continue

        is_home = home_id == team_id
        if home_goals > away_goals:
            result = 1.0 if is_home else 0.0
        elif home_goals == away_goals:
            result = 0.5
        else:
            result = 0.0 if is_home else 1.0

        w = decay ** i
        points += result * w
        weight_sum += w

    return round(points / weight_sum, 4) if weight_sum else 0.5


# ── H2H ────────────────────────────────────────────────────────────────────

def _h2h_xg_diff(home_id: int, away_id: int, last_n: int = 6) -> float:
    """xG difference in H2H matches (home perspective). Positive = home team scores more."""
    data = _api_get("fixtures/headtohead", {
        "h2h": f"{home_id}-{away_id}",
        "last": last_n,
        "status": "FT",
    })
    fixtures = data.get("response", [])
    diffs = []
    for fx in fixtures:
        stats_data = _api_get("fixtures/statistics", {"fixture": fx["fixture"]["id"]})
        home_xg = _xg_from_stats(stats_data.get("response", []), home_id)
        away_xg = _xg_from_stats(stats_data.get("response", []), away_id)
        diffs.append(home_xg - away_xg)
    return round(sum(diffs) / len(diffs), 3) if diffs else 0.0


# ── Injuries ────────────────────────────────────────────────────────────────

def _injury_count(team_id: int, fixture_id: int) -> int:
    """Number of confirmed injuries/suspensions for a team before the fixture."""
    data = _api_get("injuries", {"fixture": fixture_id, "team": team_id})
    return len(data.get("response", []))


# ── Days since last match ───────────────────────────────────────────────────

def _days_since_last_match(team_id: int) -> int:
    data = _api_get("fixtures", {"team": team_id, "last": 1, "status": "FT"})
    fixtures = data.get("response", [])
    if not fixtures:
        return 7  # neutral default
    last_date_str = fixtures[0]["fixture"]["date"]
    last_date = datetime.fromisoformat(last_date_str.replace("Z", "+00:00"))
    delta = datetime.now(timezone.utc) - last_date
    return max(delta.days, 0)


# ── Odds closing drift ──────────────────────────────────────────────────────

def _odds_closing_drift(opening_home_odds: float, current_home_odds: float) -> float:
    """
    Proportional movement in home odds from open to now.
    Negative = odds shortened (market moved toward home win).
    """
    if opening_home_odds <= 0:
        return 0.0
    return round((current_home_odds - opening_home_odds) / opening_home_odds, 4)


# ── Odds extraction helper ──────────────────────────────────────────────────

def _extract_flat_odds(match: dict) -> tuple[float, float, float]:
    """
    Return (home_odds, draw_odds, away_odds) from either:
      - flat keys: match["home_odds"] / match["draw_odds"] / match["away_odds"]
      - bookmakers list: best price across all listed bookmakers
    """
    if "home_odds" in match and match["home_odds"]:
        return float(match["home_odds"]), float(match["draw_odds"]), float(match["away_odds"])
    best_h = best_d = best_a = 0.0
    for bk in match.get("bookmakers", []):
        if bk.get("home", 0) > best_h:
            best_h = bk["home"]
        if bk.get("draw", 0) > best_d:
            best_d = bk["draw"]
        if bk.get("away", 0) > best_a:
            best_a = bk["away"]
    return best_h, best_d, best_a


# ── Main enrichment entry point ─────────────────────────────────────────────

def enrich_match(
    match: dict,
    market_probs: dict[str, float],
    pinnacle_probs: dict[str, float] | None = None,
    opening_home_odds: float | None = None,
    fixture_id: int | None = None,
) -> dict | None:
    """
    Build a complete feature dict (matching model.FEATURE_COLS) for one match.

    match: must have keys home_team, away_team, home_odds, draw_odds, away_odds
    market_probs: {"H": p, "D": p, "A": p} from market_consensus_probs()
    pinnacle_probs: {"H": p, "D": p, "A": p} or None
    opening_home_odds: home odds at market open (for drift calculation)
    fixture_id: API-Football fixture ID (enables injury lookup)

    Returns None if API calls fail fatally.
    """
    if not _API_KEY:
        # No API key — return zeros for live-data fields, odds-only features intact
        return _odds_only_features(match, market_probs, pinnacle_probs, opening_home_odds)

    home_name = match["home_team"]
    away_name = match["away_team"]

    home_id = _team_id(home_name)
    away_id = _team_id(away_name)
    if home_id is None or away_id is None:
        return _odds_only_features(match, market_probs, pinnacle_probs, opening_home_odds)

    elo = _get_elo()
    h_odds, d_odds, a_odds = _extract_flat_odds(match)

    try:
        features = {
            "xg_home_last5": _last5_xg(home_id),
            "xg_away_last5": _last5_xg(away_id),
            "home_form_weighted": _weighted_form(home_id),
            "away_form_weighted": _weighted_form(away_id),
            "h2h_xg_diff": _h2h_xg_diff(home_id, away_id),
            "odds_closing_drift": _odds_closing_drift(
                opening_home_odds or h_odds,
                h_odds,
            ),
            "pinnacle_prob_home": (pinnacle_probs or {}).get("H", market_probs.get("H", 0.0)),
            "pinnacle_prob_draw": (pinnacle_probs or {}).get("D", market_probs.get("D", 0.0)),
            "pinnacle_prob_away": (pinnacle_probs or {}).get("A", market_probs.get("A", 0.0)),
            "market_consensus_home": market_probs.get("H", 0.0),
            "market_consensus_draw": market_probs.get("D", 0.0),
            "market_consensus_away": market_probs.get("A", 0.0),
            "days_since_last_match_home": _days_since_last_match(home_id),
            "days_since_last_match_away": _days_since_last_match(away_id),
            "home_advantage_index": elo.get_home_advantage_index(home_name),
            "elo_rating_diff": elo.get_diff(home_name, away_name),
            "home_odds": h_odds,
            "draw_odds": d_odds,
            "away_odds": a_odds,
        }
    except Exception as exc:
        print(f"  [enricher] API error for {home_name} vs {away_name}: {exc}")
        return _odds_only_features(match, market_probs, pinnacle_probs, opening_home_odds)

    return features


def _odds_only_features(
    match: dict,
    market_probs: dict[str, float],
    pinnacle_probs: dict[str, float] | None,
    opening_home_odds: float | None,
) -> dict:
    """Fallback when API is unavailable — uses market signals only."""
    elo = _get_elo()
    h_odds, d_odds, a_odds = _extract_flat_odds(match)
    current_home = h_odds
    return {
        "xg_home_last5": 0.0,
        "xg_away_last5": 0.0,
        "home_form_weighted": 0.5,
        "away_form_weighted": 0.5,
        "h2h_xg_diff": 0.0,
        "odds_closing_drift": _odds_closing_drift(
            opening_home_odds or current_home, current_home
        ),
        "pinnacle_prob_home": (pinnacle_probs or {}).get("H", market_probs.get("H", 0.0)),
        "pinnacle_prob_draw": (pinnacle_probs or {}).get("D", market_probs.get("D", 0.0)),
        "pinnacle_prob_away": (pinnacle_probs or {}).get("A", market_probs.get("A", 0.0)),
        "market_consensus_home": market_probs.get("H", 0.0),
        "market_consensus_draw": market_probs.get("D", 0.0),
        "market_consensus_away": market_probs.get("A", 0.0),
        "days_since_last_match_home": 7,
        "days_since_last_match_away": 7,
        "home_advantage_index": elo.get_home_advantage_index(match["home_team"]),
        "elo_rating_diff": elo.get_diff(match["home_team"], match["away_team"]),
        "home_odds": h_odds,
        "draw_odds": d_odds,
        "away_odds": a_odds,
    }
