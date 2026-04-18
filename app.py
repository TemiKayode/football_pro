"""
app.py — Football Pro: 8-Layer Prediction Engine
Run:  python app.py   →   http://localhost:5000
"""
from __future__ import annotations
import json, os, subprocess, sys, threading, time, logging, traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "core"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=False)

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(ROOT/"football_pro.log", encoding="utf-8")])
log = logging.getLogger("app")

app = Flask(__name__, template_folder="templates", static_folder="static")

ODDS_KEY   = os.getenv("ODDS_API_KEY","")
AFL_KEY    = os.getenv("API_FOOTBALL_KEY","")
BANKROLL   = float(os.getenv("BANKROLL","1000"))
MIN_EDGE   = float(os.getenv("MIN_EDGE","0.04"))
MIN_CONF   = int(os.getenv("MIN_CONFIDENCE","65"))
KELLY_FRAC = float(os.getenv("KELLY_FRACTION","0.25"))
DRY_RUN    = os.getenv("DRY_RUN","true").lower()=="true"
FOLD_SIZES = [int(x) for x in os.getenv("ACCA_FOLDS","3,5,7,10").split(",") if x.strip().isdigit()]

STATE = {"matches":[],"picks":[],"accumulators":{},"live":[],"last_run":None,
         "status":"idle","error":None,"credits":"?","api_keys":{"odds":bool(ODDS_KEY),"football":bool(AFL_KEY)}}
_lock = threading.Lock()

def _sse(d): return f"data: {json.dumps(d)}\n\n"
def _mkt(m): return {"H":"Home Win","D":"Draw","A":"Away Win","O25":"Over 2.5","U25":"Under 2.5","BTTS":"BTTS"}.get(m,m)
def _kelly(prob,odds):
    b=odds-1
    if b<=0 or prob<=0: return 0.0
    k=(b*prob-(1-prob))/b
    return round(max(0,BANKROLL*k*KELLY_FRAC),2)

def _demo():
    s=[("Man City","Arsenal",2.05,3.40,3.80,"soccer_epl","Premier League"),
       ("PSG","Lyon",1.55,3.80,5.50,"soccer_france_ligue_one","Ligue 1"),
       ("Bayern Munich","Stuttgart",1.45,4.20,7.00,"soccer_germany_bundesliga","Bundesliga"),
       ("Barcelona","Real Betis",1.35,4.80,8.50,"soccer_spain_la_liga","La Liga"),
       ("Juventus","Bologna",1.80,3.40,4.80,"soccer_italy_serie_a","Serie A"),
       ("Ajax","PSV",2.05,3.30,3.60,"soccer_netherlands_eredivisie","Eredivisie"),
       ("Napoli","Fiorentina",1.90,3.40,4.20,"soccer_italy_serie_a","Serie A"),
       ("Dortmund","Hoffenheim",1.70,3.60,5.20,"soccer_germany_bundesliga","Bundesliga")]
    return [{"home_team":h,"away_team":a,"league":lg,"league_name":ln,
             "commence_time":datetime.now(timezone.utc).isoformat(),
             "bookmakers":[{"name":"Demo","home":ho,"draw":do,"away":ao}],
             "totals":[{"name":"Demo","over25":1.85,"under25":1.95}]}
            for h,a,ho,do,ao,lg,ln in s]

def run_pipeline(push=None):
    def p(msg,lvl="info"):
        log.info(msg)
        if push: push({"type":"log","msg":msg,"level":lvl})

    with _lock: STATE["status"]="running"; STATE["error"]=None

    try:
        p("="*52); p("Football Pro — 8-Layer Analysis Engine v2.0")
        p(f"Keys: Odds={'SET' if ODDS_KEY else 'MISSING'} | AFL={'SET' if AFL_KEY else 'MISSING'}")
        p("="*52)

        p("[1/6] Fetching live odds (top-10 leagues)...")
        if ODDS_KEY:
            from odds_fetcher_new import fetch_all_football_odds,best_odds,market_consensus_probs,best_totals_odds,get_credits_remaining
            matches = fetch_all_football_odds()
            cr = get_credits_remaining()
            STATE["credits"]=cr
            p(f"  {len(matches)} matches — {cr} credits remaining")
        else:
            p("  No ODDS_API_KEY — demo mode","warn")
            matches = _demo()

        if not matches:
            p("  No matches — check quota","warn")
            with _lock: STATE["status"]="done"
            return

        if push: push({"type":"match_count","count":len(matches)})

        p("[2/6] Loading prediction models...")
        from goals_analyzer import GoalsAnalyzer
        csv_path = str(ROOT/"data.csv") if (ROOT/"data.csv").exists() else "data.csv"
        gm = GoalsAnalyzer(csv_path)
        p(f"  Poisson goals model loaded")

        p(f"[3/6] 8-layer analysis — {len(matches)} matches...")
        from odds_fetcher_new import best_odds,market_consensus_probs

        match_rows=[]
        picks=[]
        skipped=0

        for i,match in enumerate(matches):
            home=match["home_team"]; away=match["away_team"]; lg=match.get("league","")
            bo=best_odds(match); mc=market_consensus_probs(match)
            h_o=bo.get("home",0); d_o=bo.get("draw",0); a_o=bo.get("away",0)

            try:
                poi=gm.predict_markets(home,away,home_odds=h_o,draw_odds=d_o,away_odds=a_o)
            except:
                poi={"H":0.33,"D":0.33,"A":0.33,"O25":0.5,"U25":0.5,"xG_home":1.4,"xG_away":1.1}

            match_rows.append({
                "home":home,"away":away,"league":lg,
                "league_name":match.get("league_name",lg),
                "time":match.get("commence_time",""),
                "pH":round(poi.get("H",0)*100,1),"pD":round(poi.get("D",0)*100,1),
                "pA":round(poi.get("A",0)*100,1),"pO25":round(poi.get("O25",0)*100,1),
                "xg_home":poi.get("xG_home",0),"xg_away":poi.get("xG_away",0),
                "best_home_odds":h_o,"best_draw_odds":d_o,"best_away_odds":a_o,
                "best_home_bk":bo.get("home_bk",""),"best_draw_bk":bo.get("draw_bk",""),
                "best_away_bk":bo.get("away_bk",""),
            })

            if AFL_KEY:
                try:
                    from intelligence import full_prediction
                    pred=full_prediction(home,away,lg,h_o,d_o,a_o,mc)
                    if pred.get("skip"): skipped+=1; continue
                    conf=pred.get("confidence",0)
                    if conf>=MIN_CONF:
                        for pk in pred.get("top_picks",[]):
                            om={"H":h_o,"D":d_o,"A":a_o}
                            po=pk.get("odds") or om.get(pk["market"],0)
                            picks.append({
                                "home":home,"away":away,"league":lg,
                                "league_name":match.get("league_name",lg),
                                "market":pk["market"],"market_label":_mkt(pk["market"]),
                                "odds":round(po,2),"model_prob":round(pk["prob"]*100,1),
                                "implied_prob":round((1/po)*100,1) if po>0 else 0,
                                "edge":round(pk["edge"]*100,2),"confidence":conf,
                                "kelly_stake":_kelly(pk["prob"],po),
                                "warnings":pred.get("warnings",[]),"flags":pred.get("flags",[]),
                                "xg_home":pred.get("xg_home",0),"xg_away":pred.get("xg_away",0),
                                "h2h":pred["layers"].get("h2h",{}),"reverse_modifier":pred["layers"].get("h2h",{}).get("reverse_modifier",0),
                                "context_home":pred["layers"].get("home_context",{}),"context_away":pred["layers"].get("away_context",{}),
                                "injuries_home":pred["layers"].get("home_injuries",{}),"injuries_away":pred["layers"].get("away_injuries",{}),
                            })
                except Exception as e:
                    log.debug(f"Deep analysis {home} vs {away}: {e}")
            else:
                for mkt,prob,o in [("H",poi["H"],h_o),("D",poi["D"],d_o),("A",poi["A"],a_o)]:
                    if o>1.05:
                        imp=1.0/o; edge=prob-imp
                        if edge>=MIN_EDGE and prob>=0.55:
                            picks.append({"home":home,"away":away,"league":lg,
                                "league_name":match.get("league_name",lg),
                                "market":mkt,"market_label":_mkt(mkt),
                                "odds":round(o,2),"model_prob":round(prob*100,1),
                                "implied_prob":round(imp*100,1),"edge":round(edge*100,2),
                                "confidence":round(prob*100,1),"kelly_stake":_kelly(prob,o),
                                "warnings":[],"flags":[]})
            if i%5==0: p(f"  {i+1}/{len(matches)} processed...")

        p(f"  Skipped {skipped} (low confidence/manager crisis)")
        picks.sort(key=lambda x:x.get("confidence",0)*x.get("edge",0),reverse=True)
        if push: push({"type":"matches","data":match_rows}); push({"type":"picks","data":picks})

        p(f"[4/6] Building accumulators (folds:{FOLD_SIZES})...")
        accas={}
        try:
            from accumulator import build_accumulators,Selection
            sels=[Selection(home_team=pk["home"],away_team=pk["away"],market=pk["market"],
                            odds=pk["odds"],bookmaker="",model_prob=pk["model_prob"]/100,
                            implied_prob=pk["implied_prob"]/100,edge=pk["edge"]/100,league=pk["league"])
                  for pk in picks]
            raw=build_accumulators(sels,fold_sizes=FOLD_SIZES,top_per_fold=3)
            for n,lst in raw.items():
                accas[str(n)]=[{"combined_odds":a.combined_odds,"combined_prob":round(a.combined_prob*100,2),
                    "ev":round(a.ev*100,2),"legs":[{"match":l.match_label,"market":l.market_label,
                    "odds":l.odds,"prob":round(l.model_prob*100,1),"edge":round(l.edge*100,2)} for l in a.legs],
                    "payouts":{str(s):round(a.payout(s),2) for s in [10,20,50,100]},
                    "profits":{str(s):round(a.profit(s),2) for s in [10,20,50,100]}} for a in lst]
            p(f"  {sum(len(v) for v in accas.values())} combos built")
        except Exception as e: p(f"  Acca error: {e}","warn")
        if push: push({"type":"accumulators","data":accas})

        p("[5/6] Logging bets (dry run)...")
        if DRY_RUN:
            try:
                from clv_tracker import log_bet
                for pk in picks[:20]: log_bet(pk["home"],pk["away"],pk["market"],pk["odds"],pk["kelly_stake"],pk["model_prob"]/100)
                p(f"  Logged {min(len(picks),20)} bets")
            except Exception as e: p(f"  CLV log: {e}","warn")

        now=datetime.now(timezone.utc).isoformat()
        p(f"[6/6] Complete — {len(matches)} matches | {len(picks)} picks | {now}")
        with _lock: STATE.update({"matches":match_rows,"picks":picks,"accumulators":accas,"last_run":now,"status":"done","error":None})
        if push: push({"type":"done","picks":len(picks),"matches":len(match_rows)})

    except Exception as e:
        log.error(traceback.format_exc())
        with _lock: STATE["status"]="error"; STATE["error"]=str(e)
        if push: push({"type":"error","msg":str(e)})


@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/status")
def api_status():
    with _lock: return jsonify({"status":STATE["status"],"error":STATE["error"],"last_run":STATE["last_run"],"matches":len(STATE["matches"]),"picks":len(STATE["picks"]),"live":len(STATE["live"]),"credits":STATE["credits"],"api_keys":STATE["api_keys"],"config":{"dry_run":DRY_RUN,"bankroll":BANKROLL,"min_edge_pct":MIN_EDGE*100,"min_conf":MIN_CONF,"kelly":KELLY_FRAC,"folds":FOLD_SIZES}})

@app.route("/api/matches")
def api_matches():
    q=(request.args.get("q","") or "").lower(); lg=request.args.get("league","")
    with _lock: data=STATE["matches"]
    if lg: data=[m for m in data if m.get("league")==lg]
    if q:  data=[m for m in data if q in m["home"].lower() or q in m["away"].lower()]
    return jsonify({"matches":data,"total":len(data),"updated":STATE.get("last_run")})

@app.route("/api/picks")
def api_picks():
    mkt=request.args.get("market",""); lg=request.args.get("league","")
    mine=float(request.args.get("min_edge","0") or 0); minc=float(request.args.get("min_conf","0") or 0)
    with _lock: data=STATE["picks"]
    if mkt: data=[p for p in data if p["market"]==mkt]
    if lg:  data=[p for p in data if p.get("league")==lg]
    if mine: data=[p for p in data if p["edge"]>=mine]
    if minc: data=[p for p in data if p.get("confidence",0)>=minc]
    return jsonify({"picks":data,"total":len(data)})

@app.route("/api/accumulators")
def api_accumulators():
    with _lock: return jsonify({"accumulators":STATE["accumulators"]})

@app.route("/api/live")
def api_live():
    if AFL_KEY:
        import requests as req
        try:
            r=req.get("https://v3.football.api-sports.io/fixtures",headers={"x-apisports-key":AFL_KEY},params={"live":"all"},timeout=10)
            live=[{"home":fx["teams"]["home"]["name"],"away":fx["teams"]["away"]["name"],"score_h":fx["goals"].get("home",0),"score_a":fx["goals"].get("away",0),"minute":fx["fixture"]["status"].get("elapsed"),"status":fx["fixture"]["status"].get("long",""),"league":fx["league"]["name"]} for fx in r.json().get("response",[])]
            with _lock: STATE["live"]=live
        except Exception as e: log.warning(f"Live: {e}")
    with _lock: return jsonify({"live":STATE["live"],"count":len(STATE["live"])})

@app.route("/api/kelly")
def api_kelly():
    prob=float(request.args.get("prob",0.55)); odds=float(request.args.get("odds",2.0))
    bank=float(request.args.get("bankroll",BANKROLL)); frac=float(request.args.get("fraction",KELLY_FRAC))
    b=odds-1; fk=(b*prob-(1-prob))/b if b>0 else 0
    return jsonify({"stake":round(bank*max(0,fk)*frac,2),"full_kelly_pct":round(fk*100,2),"frac_kelly_pct":round(fk*frac*100,2),"ev_pct":round((prob*odds-1)*100,2),"breakeven_pct":round((1/odds)*100,2)})

@app.route("/api/clv")
def api_clv():
    try:
        from clv_tracker import _load_bets
        bets=_load_bets(); rows=[]
        for b in bets.values():
            pnl=round(b.stake*b.opening_odds-b.stake,2) if b.result=="W" else (round(-b.stake,2) if b.result=="L" else 0)
            rows.append({"bet_id":b.bet_id,"match":f"{b.home_team} vs {b.away_team}","outcome":b.outcome,"opening_odds":b.opening_odds,"closing_odds":b.closing_odds,"stake":b.stake,"result":b.result,"clv":b.clv,"placed_at":b.placed_at,"pnl":pnl})
        settled=[r for r in rows if r["result"] in ("W","L")]; wins=[r for r in settled if r["result"]=="W"]
        st=sum(r["stake"] for r in settled); ret=sum(r["stake"]*r["opening_odds"] for r in wins); pr=ret-st
        wclv=[r for r in rows if r["closing_odds"]>0]
        return jsonify({"bets":rows,"summary":{"total":len(rows),"settled":len(settled),"wins":len(wins),"total_staked":round(st,2),"profit":round(pr,2),"roi":round(pr/st*100,2) if st else 0,"avg_clv":round(sum(r["clv"] for r in wclv)/len(wclv),4) if wclv else 0,"win_rate":round(len(wins)/len(settled)*100,1) if settled else 0}})
    except Exception as e: return jsonify({"error":str(e),"bets":[],"summary":{}}),200

@app.route("/api/clv/update",methods=["POST"])
def api_clv_update():
    data=request.json or {}
    try:
        from clv_tracker import update_closing_odds,update_result
        if "closing_odds" in data: update_closing_odds(data["bet_id"],float(data["closing_odds"]))
        if "won" in data: update_result(data["bet_id"],bool(data["won"]))
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"error":str(e)}),400

@app.route("/api/refresh",methods=["POST"])
def api_refresh():
    if STATE.get("status")=="running": return jsonify({"ok":False,"msg":"Already running"}),409
    threading.Thread(target=run_pipeline,daemon=True).start()
    return jsonify({"ok":True})

@app.route("/api/prepare", methods=["POST"])
def api_prepare():
    """Download historical CSV via core/prepare_data.py (local only — needs disk + Python)."""
    data = request.json or {}
    league = str(data.get("league") or "E0").strip()
    seasons_raw = str(data.get("seasons") or "2122,2223,2324,2425")
    seasons = [s.strip() for s in seasons_raw.replace(" ", "").split(",") if s.strip()]
    script = ROOT / "core" / "prepare_data.py"
    cmd = [sys.executable, str(script), "--league", league, "--seasons", *seasons, "--out", str(ROOT / "data.csv")]
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=900)
        out = (proc.stdout or "")[-12000:]
        err = (proc.stderr or "")[-6000:]
        return jsonify({"ok": proc.returncode == 0, "stdout": out, "stderr": err})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "stdout": "", "stderr": "Timed out (15 min). Try fewer seasons."})
    except Exception as e:
        log.exception("prepare")
        return jsonify({"ok": False, "stdout": "", "stderr": str(e)})


@app.route("/api/train", methods=["POST"])
def api_train():
    """Train sklearn ensemble via core/model.py (local only)."""
    csv_path = ROOT / "data.csv"
    if not csv_path.is_file():
        return jsonify({"ok": False, "stdout": "", "stderr": f"Missing {csv_path.name}. Run Prepare data first."})
    script = ROOT / "core" / "model.py"
    cmd = [sys.executable, str(script), str(csv_path)]
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=1800)
        out = (proc.stdout or "")[-12000:]
        err = (proc.stderr or "")[-6000:]
        return jsonify({"ok": proc.returncode == 0, "stdout": out, "stderr": err})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "stdout": "", "stderr": "Training timed out (30 min)."})
    except Exception as e:
        log.exception("train")
        return jsonify({"ok": False, "stdout": "", "stderr": str(e)})


@app.route("/api/refresh/stream")
def api_refresh_stream():
    import queue; q=queue.Queue()
    def push(msg): q.put(msg)
    def gen():
        threading.Thread(target=run_pipeline,args=(push,),daemon=True).start()
        while True:
            try:
                msg=q.get(timeout=180)
                yield _sse(msg)
                if msg.get("type") in ("done","error"): break
            except: break
    return Response(stream_with_context(gen()),mimetype="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

if __name__=="__main__":
    print(f"\n{'='*58}\n  Football Pro — 8-Layer Engine\n  Odds API: {'✓' if ODDS_KEY else '✗ MISSING'}  |  AFL API: {'✓' if AFL_KEY else '✗ MISSING'}\n  Min Confidence: {MIN_CONF}%  |  http://localhost:5000\n{'='*58}\n")
    threading.Thread(target=run_pipeline,daemon=True).start()
    app.run(host="0.0.0.0",port=5000,debug=False,threaded=True)
