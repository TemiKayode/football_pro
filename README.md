# ⚽ Football Value Bot — Live Dashboard

Real-time football analytics dashboard covering the **top 10 leagues worldwide**.
Runs as a static site on **Netlify** with serverless functions, or locally with Python/Flask.

<img width="1889" height="834" alt="image" src="https://github.com/user-attachments/assets/88099c97-6caa-44ac-ae40-08bdca266ab5" />

![Leagues](https://img.shields.io/badge/Leagues-10-blue) ![Auto Refresh](https://img.shields.io/badge/Auto--refresh-60s-green) ![Netlify](https://img.shields.io/badge/Deployed-Netlify-teal)

---

## 🌍 Leagues Covered

| League | Country | Flag |
|--------|---------|------|
| Premier League | England | 🏴󠁧󠁢󠁥󠁮󠁧󠁿 |
| La Liga | Spain | 🇪🇸 |
| Bundesliga | Germany | 🇩🇪 |
| Serie A | Italy | 🇮🇹 |
| Ligue 1 | France | 🇫🇷 |
| Champions League | Europe | 🏆 |
| Eredivisie | Netherlands | 🇳🇱 |
| Primeira Liga | Portugal | 🇵🇹 |
| Brasileirão | Brazil | 🇧🇷 |
| Liga Profesional | Argentina | 🇦🇷 |

---

## 🚀 Deploy to Netlify (5 minutes)

### Option A — GitHub + Netlify (recommended)

1. **Fork or push this repo to GitHub**

2. **Connect to Netlify**
   - Go to [netlify.com](https://netlify.com) → "Add new site" → "Import from Git"
   - Select your repo

3. **Build settings** (auto-detected from `netlify.toml`):
   ```
   Build command:     echo done
   Publish directory: public
   Functions dir:     netlify/functions
   ```

4. **Add environment variables** (Site → Settings → Environment Variables):

   | Variable | Value | Required |
   |----------|-------|----------|
   | `ODDS_API_KEY` | Your key from the-odds-api.com | **Yes** |
   | `FOOTBALL_DATA_KEY` | Your key from football-data.org | Optional |
   | `MIN_EDGE` | `0.03` | Optional |
   | `KELLY_FRACTION` | `0.25` | Optional |
   | `BANKROLL` | `1000` | Optional |
   | `ACCA_FOLDS` | `3,5,7` | Optional |

5. **Deploy!** — Netlify auto-deploys on every push to main.

### Option B — Netlify CLI

```bash
npm install -g netlify-cli
netlify login
netlify init      # link to your site
netlify dev       # run locally at http://localhost:8888
netlify deploy --prod
```

---

## 💻 Local Python Setup

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/football-value-bot.git
cd football-value-bot

# 2. Virtual environment
python3 -m venv venv
source venv/bin/activate      # macOS/Linux
# venv\Scripts\activate       # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env and add your API keys

# 5. Run
python app.py
# Open: http://localhost:5000
```

---

## 🔑 API Keys — Where to Get Them

### The Odds API (REQUIRED for live odds)
- URL: https://the-odds-api.com
- Free tier: **500 requests/month**
- Cost: $0/month to start
- With `MAX_LEAGUES=10` and daily runs: ~300 requests/month (within free tier)

### football-data.org (OPTIONAL for live scores)
- URL: https://www.football-data.org
- Free tier: **10 requests/minute**, competitions PL, PD, BL1, SA, FL1
- Paid tier: All competitions including CL, UCL
- Cost: Free for basic use

### API-Football (OPTIONAL for xG enrichment)
- URL: https://www.api-football.com
- Free tier: **100 requests/day**
- Used for: xG stats, injuries, lineups

---

## 📋 Features

| Feature | Description |
|---------|-------------|
| **Live scores** | Real-time match updates every 60 seconds |
| **Odds fetching** | Best odds across 30+ bookmakers per market |
| **Value picks** | Model probability vs bookmaker implied probability |
| **5 markets** | Home Win, Away Win, Draw, Over/Under 2.5 |
| **xG estimates** | Poisson-based expected goals from odds |
| **Accumulators** | Best 3/5/7-fold combos with stake calculator |
| **Kelly sizing** | Automatic fractional Kelly stake per pick |
| **CLV tracker** | Log bets, enter closing odds, track edge |
| **Demo mode** | Works without API keys using sample data |
| **Auto-refresh** | Updates every 60 seconds automatically |

---

## 🗂️ Project Structure

```
football-value-bot/
├── public/
│   └── index.html              ← Single-page app (works on Netlify + Flask)
├── netlify/
│   └── functions/
│       ├── matches.js          ← Live odds fetcher (Netlify serverless)
│       ├── scores.js           ← Live scores (football-data.org)
│       └── picks.js            ← Value picks + accumulator builder
├── core/                       ← Python ML pipeline (local only)
│   ├── accumulator.py
│   ├── goals_analyzer.py
│   ├── model.py
│   ├── elo.py
│   └── ...
├── app.py                      ← Flask server (local Python backend)
├── requirements.txt            ← Python dependencies
├── package.json                ← Node.js dependencies (Netlify CLI)
├── netlify.toml                ← Netlify configuration
├── .env.example                ← Environment variable template
└── README.md
```

---

## ⚙️ Configuration

All settings via environment variables (Netlify dashboard or `.env` file):

```bash
ODDS_API_KEY=your_key        # The Odds API — live odds
FOOTBALL_DATA_KEY=your_key   # football-data.org — live scores
API_FOOTBALL_KEY=your_key    # api-football.com — xG enrichment

MIN_EDGE=0.03                # Min % edge to flag value pick (3%)
KELLY_FRACTION=0.25          # Quarter Kelly stake sizing
BANKROLL=1000                # Your bankroll in local currency
ACCA_FOLDS=3,5,7             # Accumulator fold sizes to build
DRY_RUN=true                 # true = no real bets placed
```

---

## 📈 How Value Picks Work

1. **Fetch best odds** across all bookmakers (line shopping)
2. **Remove vig** — calculate fair market probabilities
3. **Estimate xG** — Poisson model from odds-implied probabilities
4. **Compare** — model probability vs bookmaker implied probability
5. **Flag edge** — if model prob > implied prob by MIN_EDGE, it's a value pick
6. **Kelly size** — calculate optimal stake as % of bankroll

**Edge formula:** `edge = model_probability - (1 / best_odds)`

---

## 🔄 Daily Updates

The dashboard auto-refreshes every **60 seconds** in the browser.

For automated daily data updates, add a Netlify build webhook triggered daily:
1. Site Settings → Build & deploy → Build hooks → Add hook
2. Use a service like EasyCron: `curl -X POST YOUR_BUILD_HOOK_URL`
3. Schedule: Daily at 08:00 UTC

Or use GitHub Actions (`.github/workflows/daily.yml` — see below).

---

## 🐙 GitHub Actions — Daily Trigger

Create `.github/workflows/daily.yml`:

```yaml
name: Daily data refresh
on:
  schedule:
    - cron: '0 6 * * *'  # 6am UTC daily
  workflow_dispatch:

jobs:
  trigger-build:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger Netlify build
        run: curl -X POST ${{ secrets.NETLIFY_BUILD_HOOK }}
```

Add `NETLIFY_BUILD_HOOK` as a GitHub secret.

---

## ⚠️ Disclaimer

This tool is for **educational and research purposes only**.
Sports betting involves financial risk. Only bet amounts you can afford to lose.
Ensure betting is legal in your jurisdiction before use.
The authors accept no responsibility for financial losses.

---

## 📄 License

MIT License — see LICENSE file.
