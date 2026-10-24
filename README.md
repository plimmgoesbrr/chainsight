# ChainSight — Explainable Bitcoin Transaction Intelligence

**Follow the money. Explain the flag.**

An offline investigator tool for Bitcoin forensics. ChainSight reads bulk transaction and network metadata (CSV / JSON / XML), links IP addresses to transactions and wallets, groups wallets by likely owner, scores every wallet's risk from 0–100 with machine learning and graph analysis, and explains every flag in plain English — no LLM, no internet, every number traceable.

Built for **Smart India Hackathon 2026** (problem statement SIH26146, AI-Powered Monitoring & Analysis of Bitcoin Transaction Traffic) by Karthik K, Team Neutralyze.

## At a glance

- **9-stage pipeline:** ingest → geo-enrich → IP linking → owner grouping → 19 wallet features → detection → scoring → explanation → storage
- **Hybrid risk score:** XGBoost (known patterns) + Isolation Forest (unusual behaviour) + personalised PageRank from flagged wallets (follow the money) + laundering-pattern detectors (peeling chains, fan-out, fan-in, mixing)
- **Explainable:** TreeSHAP attributions turned into checkable sentences, e.g. *"90/100 HIGH — 2 hops from a flagged wallet; part of a 9-hop peeling chain; funds moved on within minutes."*
- **Results on a held-out dataset:** 30/30 laundering cases caught, PR-AUC 0.98, 100% precision in the top 50 alerts, <1% false alarms, ~5–7 s per run on a laptop
- **Stack:** Python, XGBoost, scikit-learn, NetworkX, DuckDB, FastAPI, Cytoscape.js, Chart.js — fully offline

---

## How it works

| # | Stage | What happens |
|---|-------|--------------|
| 1 | Ingest & clean | CSV / JSON / XML → validated transactions; duplicates removed, missing fees filled, invalid rows dropped |
| 2 | Geo enrichment | Offline IP → country / ASN / type lookup (`data/geo_db.csv`, GeoLite2-style) |
| 3 | Link IPs | Timing-based matching degree P(tx, IP) — the origin node announces to many peers within ~2 s (P ≥ 1.5); K(wallet, IP) ≥ 1.5 links an IP to a wallet |
| 4 | Group owners | Union-Find with H1 (multi-input), H2 (one-time change, address-type rule), H3 (reused outputs are not change); CoinJoin-like transactions are filtered first |
| 5 | Measure behaviour | 19 features per wallet: fast outflow, holding time, fan-in/out, frequency, life span, PageRank, owner-group size, countries, hosting/VPN share, mixer use … |
| 6 | Detect | **P** XGBoost (known patterns) · **A** Isolation Forest (unusual) · **G** Personalised PageRank from known-flagged seed wallets, following the money downstream · **T** pattern detectors (peeling chain, rapid movement, fan-out, fan-in, repeated splitting, mixing) |
| 7 | Score | `Risk = 100 × (0.5·P + 0.1·A + 0.3·G + 0.1·T)` — weights tuned on the training world; HIGH ≥ 50, MEDIUM ≥ 30 |
| 8 | Explain | XGBoost TreeSHAP splits the model's points across features → real values fetched → sentence templates |
| 9 | Save | DuckDB (`data/chainsight.duckdb`) → FastAPI → dashboard |

**Data.** NTRO provides no dataset, so `chainsight/generate.py` builds realistic synthetic worlds:
~700 people, merchants, exchanges, charities and honest mixer users, plus 30 planted laundering
cases (peeling chains, rapid fan-out, fan-in collection, mixing then cash-out, repeated splitting).
Honest look-alikes are included on purpose (VPN users, quick forwards, payroll fan-out, donation
fan-in, honest CoinJoins). The model is trained on one world (`training_dataset`) and evaluated on a
different one (`case_dataset`) whose answers it never sees.

**Results on the held-out case dataset**

| Metric | Value |
|---|---|
| Top-50 alerts that are truly illicit | 100% |
| Laundering cases caught (≥1 HIGH wallet) | 30 / 30 |
| Illicit wallets flagged (HIGH + MEDIUM) | 96% |
| Precision of flagged wallets | about 90% (90–93% depending on library versions) |
| False-alarm rate on normal wallets | under 1% (0.5–0.7%) |
| PR-AUC | 0.98 |
| Full pipeline run time (6,675 tx, 43k observations) | ~5 s on a laptop |

Caveat: these are synthetic worlds; real-world performance must be measured on real labelled cases.
IP links and owner groups are **evidence with a confidence level, never proof of ownership**.

---

## Quick start (Windows)

1. Install **Python 3.11 or 3.12** from <https://www.python.org/downloads/>
   and tick **"Add python.exe to PATH"** on the first installer screen.
2. Unzip this folder anywhere (e.g. `Desktop\ChainSight`).
3. Double-click **`setup.bat`** — one time only, needs internet for ~2 minutes to install packages.
4. Double-click **`run.bat`** — the dashboard opens at <http://127.0.0.1:8000>.
   Keep the black window open while you use it. After setup, **no internet is needed**.

macOS / Linux: open Terminal in the folder, run `bash setup.sh` once, then `bash run.sh`.

Manual commands (any OS), from inside the folder:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt      (macOS/Linux: .venv/bin/python ...)
.venv\Scripts\python -m chainsight.cli prepare
.venv\Scripts\python -m chainsight.cli serve
```

---

## Demo walkthrough

1. **Wi-Fi off.** Everything runs locally.
2. **Data** tab → pick `case_dataset.csv` (or the JSON / XML samples, or *Upload file…*) →
   **Run analysis**. Watch the 9 pipeline stages tick through (~5 s).
3. **Overview** → transactions analysed, owner groups, IP links, daily activity, detection quality.
4. **Alerts** → every flagged wallet ranked by risk.
5. Click the top alert → **Investigate**: risk gauge, the four score components, the money-trail
   graph (with the origin IP), the score breakdown, and the plain-English reasons.
6. Click any transaction or neighbouring wallet to keep following the money.

**A good wallet to try:** type `bc1qt4h6` in the search box and press Enter →
score 90 (HIGH): 2 hops downstream of a known flagged wallet, part of a 9-hop peeling chain,
funds moved on within minutes, broadcast from a US hosting / VPN IP (dashed cyan link in the graph).

---

## API (FastAPI, http://127.0.0.1:8000)

`/api/summary` · `/api/alerts` · `/api/wallets` · `/api/wallet/{address}` · `/api/risk/{address}` ·
`/api/graph/{address}` · `/api/explanation/{address}` · `/api/transactions?txid=` ·
`/api/entity/{id}` · `/api/search?q=` · `/api/datasets` · `POST /api/upload` · `POST /api/analyze` · `/api/status`

## Project structure

```
chainsight/generate.py   synthetic data generator (normal + laundering + network logs)
chainsight/pipeline.py   the 9-stage analysis pipeline + evaluation
chainsight/train.py      XGBoost training, weight + band tuning
chainsight/store.py      DuckDB storage
chainsight/api.py        FastAPI backend
chainsight/static/       dashboard (HTML/CSS/JS, Chart.js + Cytoscape.js bundled for offline use)
data/                    datasets, seeds, geo DB, ground truth (evaluation only)
models/                  trained model + tuned weights
```

Regenerate everything from scratch: `python -m chainsight.cli prepare --force`

## Troubleshooting

- **"Python was not found"** — install Python from python.org (not the Microsoft Store) and tick
  *Add python.exe to PATH*. Then reopen setup.bat.
- **Windows protected your PC (SmartScreen)** — click *More info → Run anyway*, or run the manual
  commands above in a terminal.
- **pip install fails** — you need internet once for setup. On a college network/proxy, try a phone hotspot.
- **Port 8000 busy** — ChainSight picks the next free port automatically; use the address printed in the window.
- **Mac: error mentioning `libomp.dylib`** — XGBoost needs Apple's OpenMP library: install Homebrew (brew.sh), run `brew install libomp`, then `bash run.sh` again.
- **Windows: error mentioning `vcomp140.dll`** — install the free *Microsoft Visual C++ Redistributable (x64)* from Microsoft's website, then run run.bat again.
- **Mac: Homebrew not installed** — paste the install command from brew.sh, then run the two "Next steps" commands it prints, then `brew install libomp`.
- **Mac: "permission denied" on setup.sh** — run it as `bash setup.sh` instead of `./setup.sh`.
- **Browser didn't open** — open <http://127.0.0.1:8000> yourself.

## Author

Karthik K — B.Tech CSE, Mahindra University · [GitHub](https://github.com/plimmgoesbrr)
