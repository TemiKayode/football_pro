"""
odds_fetcher.py — Fetch odds across ALL currently active football leagues.

The Odds API /sports endpoint returns only leagues with live/upcoming events,
so we dynamically discover every active soccer competition worldwide and pull
odds for all of them in one session.

API quota notes (free tier = 500 req/month):
  /sports           — does NOT count against quota
  /odds per league  — counts 1 request each
  MAX_LEAGUES env   — caps how many leagues to pull (default 20)
"""

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY     = os.getenv("ODDS_API_KEY", "")
BASE_URL    = "https://api.the-odds-api.com/v4"
MAX_LEAGUES = int(os.getenv("MAX_LEAGUES", "20"))   # API quota guard

# Priority leagues — fetched first before others fill remaining slots
PRIORITY_LEAGUES = [
    "soccer_epl",
    "soccer_germany_bundesliga",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
    "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga",
    "soccer_turkey_super_league",
    "soccer_brazil_campeonato",
    "soccer_argentina_primera_division",
    "soccer_champions_league",
    "soccer_europa_league",
    "soccer_england_league1",
    "soccer_england_league2",
    "soccer_scotland_premiership",
    "soccer_nigeria_npfl",
    "soccer_south_africa_premier_division",
]


# ── Sport discovery ───────────────────────────────────────────────────────────

def fetch_active_football_sports() -> list[dict]:
    """
    Return all currently active soccer sport objects from the API.
    This call does NOT consume API quota.
    Each item: {key, title, active, has_outrights}
    """
    url    = f"{BASE_URL}/sports"
    params = {"apiKey": API_KEY}
    resp   = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    all_sports = resp.json()
    return [
        s for s in all_sports
        if s.get("active")
        and ("soccer" in s.get("key", "").lower()
             or "football" in s.get("title", "").lower())
    ]


def _ordered_sport_keys(active_sports: list[dict]) -> list[str]:
    """
    Return sport keys ordered: priority leagues first, then the rest.
    """
    active_keys = {s["key"] for s in active_sports}
    ordered = [k for k in PRIORITY_LEAGUES if k in active_keys]
    rest    = [s["key"] for s in active_sports if s["key"] not in ordered]
    return ordered + rest


# ── Per-league odds fetch ─────────────────────────────────────────────────────

def fetch_odds(sport: str,
               regions: str = "uk,eu,us,au",
               markets: str = "h2h,totals") -> list[dict]:
    """
    Fetch h2h + totals odds for a single sport/league.
    Returns list of match dicts:
    {
      home_team, away_team, commence_time, league,
      bookmakers: [{name, home, draw, away}],
      totals:     [{name, over25, under25}],
    }
    """
    url    = f"{BASE_URL}/sports/{sport}/odds"
    params = {
        "apiKey":     API_KEY,
        "regions":    regions,
        "markets":    markets,
        "oddsFormat": "decimal",
    }
    resp = requests.get(url, params=params, timeout=15)
    if resp.status_code == 422:
        return []     # league exists but no odds available right now
    resp.raise_for_status()

    matches = []
    for event in resp.json():
        match = {
            "home_team":     event["home_team"],
            "away_team":     event["away_team"],
            "commence_time": event.get("commence_time", ""),
            "league":        sport,
            "bookmakers":    [],
            "totals":        [],
        }
        for bk in event.get("bookmakers", []):
            bk_name = bk.get("title", bk.get("name", "unknown"))
            for market in bk.get("markets", []):

                if market["key"] == "h2h":
                    by_name = {o["name"]: o["price"]
                               for o in market.get("outcomes", [])}
                    h = by_name.get(event["home_team"], 0)
                    d = by_name.get("Draw", 0)
                    a = by_name.get(event["away_team"], 0)
                    if h and d and a:
                        match["bookmakers"].append(
                            {"name": bk_name, "home": h, "draw": d, "away": a}
                        )

                elif market["key"] == "totals":
                    by_label = {}
                    for o in market.get("outcomes", []):
                        label = o["name"].lower()
                        point = str(o.get("description", o.get("point", "")))
                        if "2.5" in point:
                            by_label[label] = o["price"]
                    ov = by_label.get("over",  0)
                    un = by_label.get("under", 0)
                    if ov and un:
                        match["totals"].append(
                            {"name": bk_name, "over25": ov, "under25": un}
                        )

        if match["bookmakers"] or match["totals"]:
            matches.append(match)

    return matches


# ── All-football aggregator ───────────────────────────────────────────────────

def fetch_all_football_odds(
    max_leagues: int = MAX_LEAGUES,
    regions: str    = "uk,eu,us,au",
    markets: str    = "h2h,totals",
    delay: float    = 0.3,          # polite delay between requests
) -> list[dict]:
    """
    Discover all currently active football leagues and fetch odds for each.
    Returns a flat list of all match dicts with a 'league' key.
    """
    active = fetch_active_football_sports()
    keys   = _ordered_sport_keys(active)[:max_leagues]

    print(f"    Active football leagues found: {len(active)}, "
          f"fetching top {len(keys)}")

    all_matches: list[dict] = []
    for i, key in enumerate(keys, 1):
        try:
            matches = fetch_odds(sport=key, regions=regions, markets=markets)
            if matches:
                league_name = next(
                    (s["title"] for s in active if s["key"] == key), key
                )
                print(f"    [{i:>2}/{len(keys)}] {league_name:<40} "
                      f"{len(matches):>3} matches")
                all_matches.extend(matches)
            else:
                print(f"    [{i:>2}/{len(keys)}] {key:<40}   0 matches (no odds yet)")
        except Exception as exc:
            print(f"    [{i:>2}/{len(keys)}] {key}: error — {exc}")
        if delay and i < len(keys):
            time.sleep(delay)

    return all_matches


# ── Derived helpers ───────────────────────────────────────────────────────────

def best_odds(match: dict) -> dict:
    best = {"home": 0.0, "draw": 0.0, "away": 0.0,
            "home_bk": "", "draw_bk": "", "away_bk": ""}
    for bk in match.get("bookmakers", []):
        if bk["home"] > best["home"]:
            best["home"], best["home_bk"] = bk["home"], bk["name"]
        if bk["draw"] > best["draw"]:
            best["draw"], best["draw_bk"] = bk["draw"], bk["name"]
        if bk["away"] > best["away"]:
            best["away"], best["away_bk"] = bk["away"], bk["name"]
    return best


def best_totals_odds(match: dict) -> dict:
    best = {"over25": 0.0, "under25": 0.0,
            "over25_bk": "", "under25_bk": ""}
    for t in match.get("totals", []):
        if t["over25"] > best["over25"]:
            best["over25"],  best["over25_bk"]  = t["over25"],  t["name"]
        if t["under25"] > best["under25"]:
            best["under25"], best["under25_bk"] = t["under25"], t["name"]
    return best


def pinnacle_implied_probs(match: dict) -> dict | None:
    for bk in match.get("bookmakers", []):
        if "pinnacle" in bk["name"].lower():
            total = 1/bk["home"] + 1/bk["draw"] + 1/bk["away"]
            return {
                "H": (1/bk["home"]) / total,
                "D": (1/bk["draw"]) / total,
                "A": (1/bk["away"]) / total,
            }
    return None


def market_consensus_probs(match: dict) -> dict:
    hp, dp, ap = [], [], []
    for bk in match.get("bookmakers", []):
        h, d, a = bk.get("home", 0), bk.get("draw", 0), bk.get("away", 0)
        if h > 0 and d > 0 and a > 0:
            total = 1/h + 1/d + 1/a
            hp.append((1/h)/total)
            dp.append((1/d)/total)
            ap.append((1/a)/total)
    if not hp:
        return {"H": 0.0, "D": 0.0, "A": 0.0}
    return {
        "H": round(sum(hp)/len(hp), 4),
        "D": round(sum(dp)/len(dp), 4),
        "A": round(sum(ap)/len(ap), 4),
    }


def totals_consensus_probs(match: dict) -> dict:
    op, up = [], []
    for t in match.get("totals", []):
        ov, un = t.get("over25", 0), t.get("under25", 0)
        if ov > 0 and un > 0:
            total = 1/ov + 1/un
            op.append((1/ov)/total)
            up.append((1/un)/total)
    if not op:
        return {"O25": 0.0, "U25": 0.0}
    return {
        "O25": round(sum(op)/len(op), 4),
        "U25": round(sum(up)/len(up), 4),
    }


if __name__ == "__main__":
    sports = fetch_active_football_sports()
    print(f"Active football leagues: {len(sports)}")
    for s in sports:
        print(f"  {s['key']:<45} {s['title']}")
    print()
    matches = fetch_all_football_odds(max_leagues=5)
    print(f"\nTotal matches: {len(matches)}")
