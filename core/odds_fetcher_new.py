"""odds_fetcher.py — patched with real API key support"""
import os, time, logging, requests
from dotenv import load_dotenv
load_dotenv()
log = logging.getLogger("odds_fetcher")
API_KEY = os.getenv("ODDS_API_KEY","")
BASE_URL = "https://api.the-odds-api.com/v4"
TOP10_LEAGUES = ["soccer_epl","soccer_spain_la_liga","soccer_germany_bundesliga","soccer_italy_serie_a","soccer_france_ligue_one","soccer_netherlands_eredivisie","soccer_portugal_primeira_liga","soccer_champions_league","soccer_europa_league","soccer_brazil_campeonato"]
LEAGUE_NAMES = {"soccer_epl":"Premier League","soccer_spain_la_liga":"La Liga","soccer_germany_bundesliga":"Bundesliga","soccer_italy_serie_a":"Serie A","soccer_france_ligue_one":"Ligue 1","soccer_netherlands_eredivisie":"Eredivisie","soccer_portugal_primeira_liga":"Primeira Liga","soccer_champions_league":"Champions League","soccer_europa_league":"Europa League","soccer_brazil_campeonato":"Brasileirão"}

def get_credits_remaining():
    try:
        r = requests.get(f"{BASE_URL}/sports", params={"apiKey":API_KEY}, timeout=8)
        return r.headers.get("x-requests-remaining","unknown")
    except: return "unknown"

def fetch_active_leagues():
    try:
        r = requests.get(f"{BASE_URL}/sports", params={"apiKey":API_KEY}, timeout=10)
        r.raise_for_status()
        active = {s["key"] for s in r.json() if s.get("active")}
        return [lg for lg in TOP10_LEAGUES if lg in active]
    except Exception as e:
        log.warning(f"Active leagues: {e}")
        return TOP10_LEAGUES

def fetch_odds_league(sport, regions="uk,eu,us,au", markets="h2h,totals"):
    url = f"{BASE_URL}/sports/{sport}/odds"
    params = {"apiKey":API_KEY,"regions":regions,"markets":markets,"oddsFormat":"decimal"}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 422: return []
        r.raise_for_status()
        rem = r.headers.get("x-requests-remaining","?")
        log.info(f"  {LEAGUE_NAMES.get(sport,sport)}: {rem} credits left")
    except Exception as e:
        log.warning(f"  {sport}: {e}")
        return []
    matches = []
    for ev in r.json():
        m = {"home_team":ev["home_team"],"away_team":ev["away_team"],"commence_time":ev.get("commence_time",""),"league":sport,"league_name":LEAGUE_NAMES.get(sport,sport),"bookmakers":[],"totals":[]}
        for bk in ev.get("bookmakers",[]):
            bn = bk.get("title",bk.get("name","?"))
            for mkt in bk.get("markets",[]):
                if mkt["key"]=="h2h":
                    bn2={o["name"]:o["price"] for o in mkt.get("outcomes",[])}
                    h=bn2.get(ev["home_team"],0); d=bn2.get("Draw",0); a=bn2.get(ev["away_team"],0)
                    if h and d and a: m["bookmakers"].append({"name":bn,"home":h,"draw":d,"away":a})
                elif mkt["key"]=="totals":
                    bl={}
                    for o in mkt.get("outcomes",[]):
                        pt=str(o.get("description",o.get("point","")))
                        if "2.5" in pt: bl[o["name"].lower()]=o["price"]
                    ov=bl.get("over",0); un=bl.get("under",0)
                    if ov and un: m["totals"].append({"name":bn,"over25":ov,"under25":un})
        if m["bookmakers"]: matches.append(m)
    return matches

def fetch_all_football_odds(delay=0.35):
    active = fetch_active_leagues()
    all_m = []
    for lg in active:
        ms = fetch_odds_league(lg)
        all_m.extend(ms)
        if delay: time.sleep(delay)
    return all_m

def best_odds(match):
    b={"home":0.0,"draw":0.0,"away":0.0,"home_bk":"","draw_bk":"","away_bk":""}
    for bk in match.get("bookmakers",[]):
        if bk["home"]>b["home"]: b["home"],b["home_bk"]=bk["home"],bk["name"]
        if bk["draw"]>b["draw"]: b["draw"],b["draw_bk"]=bk["draw"],bk["name"]
        if bk["away"]>b["away"]: b["away"],b["away_bk"]=bk["away"],bk["name"]
    return b

def market_consensus_probs(match):
    hp,dp,ap=[],[],[]
    for bk in match.get("bookmakers",[]):
        h,d,a=bk.get("home",0),bk.get("draw",0),bk.get("away",0)
        if h>0 and d>0 and a>0:
            t=1/h+1/d+1/a; hp.append((1/h)/t); dp.append((1/d)/t); ap.append((1/a)/t)
    if not hp: return {"H":0.33,"D":0.33,"A":0.33}
    return {"H":round(sum(hp)/len(hp),4),"D":round(sum(dp)/len(dp),4),"A":round(sum(ap)/len(ap),4)}

def best_totals_odds(match):
    b={"over25":0.0,"under25":0.0,"over25_bk":"","under25_bk":""}
    for t in match.get("totals",[]):
        if t["over25"]>b["over25"]: b["over25"],b["over25_bk"]=t["over25"],t["name"]
        if t["under25"]>b["under25"]: b["under25"],b["under25_bk"]=t["under25"],t["name"]
    return b

def totals_consensus_probs(match):
    op,up=[],[]
    for t in match.get("totals",[]):
        ov,un=t.get("over25",0),t.get("under25",0)
        if ov>0 and un>0:
            tt=1/ov+1/un; op.append((1/ov)/tt); up.append((1/un)/tt)
    if not op: return {"O25":0.5,"U25":0.5}
    return {"O25":round(sum(op)/len(op),4),"U25":round(sum(up)/len(up),4)}

def pinnacle_implied_probs(match):
    for bk in match.get("bookmakers",[]):
        if "pinnacle" in bk["name"].lower():
            t=1/bk["home"]+1/bk["draw"]+1/bk["away"]
            return {"H":(1/bk["home"])/t,"D":(1/bk["draw"])/t,"A":(1/bk["away"])/t}
    return None
