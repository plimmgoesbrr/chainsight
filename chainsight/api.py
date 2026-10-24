"""ChainSight FastAPI backend: serves the analysis results and the dashboard."""
import json
import math
import os
import shutil
import threading
import time

import numpy as np
import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import pipeline as pl
from . import store, train

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
UPLOADS = os.path.join(DATA, "uploads")
DB = os.path.join(DATA, "chainsight.duckdb")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="ChainSight API", version="1.0")
STATE = {"db": None, "status": {"running": False, "stage": -1, "stages": pl.STAGES, "error": None,
                                "done": False, "started": None, "timings": {}}}
LOCK = threading.Lock()


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return o


def J(o):
    return JSONResponse(_clean(o))


def db():
    if STATE["db"] is None:
        STATE["db"] = store.load(DB)
    if STATE["db"] is None:
        raise HTTPException(404, "No analysis yet. Open the Data page and run an analysis.")
    return STATE["db"]


def _jl(s, default):
    try:
        return json.loads(s) if isinstance(s, str) else default
    except Exception:
        return default


@app.on_event("startup")
def _preload():
    try:
        db()
    except Exception:
        pass


# ------------------------------------------------------------------ summary
@app.get("/api/summary")
def summary():
    d = db()
    w, t, meta = d["wallets"], d["transactions"], d["meta"]
    st = meta["stats"]
    hist, edges = np.histogram(w["score"], bins=10, range=(0, 100))
    flagged = set(w.index[w.band.isin(["HIGH", "MEDIUM"])])
    tt = t.copy()
    tt["day"] = pd.to_datetime(tt["time"]).dt.strftime("%d %b")
    tt["flag"] = [any(a in flagged for a in i + o) for i, o in zip(tt.inputs, tt.outputs)]
    daily = tt.groupby("day", sort=False).agg(all=("flag", "size"), flagged=("flag", "sum"))
    daily = daily.loc[sorted(daily.index, key=lambda x: pd.to_datetime(x + " 2026"))]
    pc = {}
    for p in w.loc[w.band.isin(["HIGH", "MEDIUM"]), "patterns"]:
        for k in _jl(p, {}):
            pc[pl.PATTERN_LABELS[k]] = pc.get(pl.PATTERN_LABELS[k], 0) + 1
    hi = w[w.band == "HIGH"]
    hi_tx = tt[tt.flag & tt.asn_type.notna()]
    asn_types = hi_tx["asn_type"].value_counts().to_dict()
    countries = hi_tx["country"].value_counts().head(6).to_dict()
    return J({
        "dataset": meta["dataset"], "elapsed": meta.get("elapsed"), "timings": meta.get("timings"),
        "time_min": meta["time_min"], "time_max": meta["time_max"],
        "kpi": {"transactions": st["transactions"], "observations": st["rows_clean"], "wallets": st["wallets"],
                "entities": st["multi_address_entities"], "high": int((w.band == "HIGH").sum()),
                "medium": int((w.band == "MEDIUM").sum()), "seeds": st["seed_wallets"],
                "ip_tx_links": st["ip_tx_links"], "ip_wallet_links": st["ip_wallet_links"],
                "coinjoin": st["coinjoin_tx"], "tx_with_origin": st["tx_with_origin"]},
        "quality": {k: st.get(k) for k in ["rows_in", "duplicates_removed", "fees_filled", "invalid_tx_dropped"]},
        "clustering": {k: st.get(k) for k in ["entities", "multi_address_entities", "largest_entity", "h1_merges",
                                               "h2_merges", "h3_rejections", "coinjoin_tx", "service_hubs"]},
        "histogram": {"bins": [f"{int(edges[i])}–{int(edges[i + 1])}" for i in range(10)],
                      "counts": hist.tolist()},
        "daily": {"days": daily.index.tolist(), "all": daily["all"].tolist(), "flagged": daily["flagged"].tolist()},
        "patterns": dict(sorted(pc.items(), key=lambda x: -x[1])),
        "network": {"asn_types": asn_types, "countries": countries},
        "weights": meta["weights"], "bands": meta["bands"], "evaluation": meta.get("evaluation"),
    })


def _wallet_row(a, r):
    reasons = _jl(r.reasons, [])
    return {"address": a, "score": round(float(r.score), 1), "band": r.band, "entity_id": r.entity_id,
            "cluster_size": int(r.cluster_size), "patterns": [pl.PATTERN_LABELS[k] for k in _jl(r.patterns, {})],
            "reason": reasons[0] if reasons else "", "is_seed": bool(r.is_seed),
            "total_recv": float(r.total_recv)}


@app.get("/api/alerts")
def alerts(band: str = "", limit: int = 200, q: str = "", no_seeds: bool = False):
    w = db()["wallets"]
    if no_seeds:
        w = w[~w.is_seed]
    s = w[w.band.isin(["HIGH", "MEDIUM"])] if not band else w[w.band == band.upper()]
    if q:
        s = s[s.index.str.contains(q, case=False) | s.entity_id.str.contains(q, case=False)]
    s = s.sort_values("score", ascending=False).head(limit)
    return J({"total": int(len(s)), "rows": [_wallet_row(a, r) for a, r in s.iterrows()]})


@app.get("/api/wallets")
def wallets(limit: int = 100):
    w = db()["wallets"].sort_values("score", ascending=False).head(limit)
    return J([_wallet_row(a, r) for a, r in w.iterrows()])


@app.get("/api/search")
def search(q: str):
    d = db()
    q = q.strip()
    if q in d["wallets"].index:
        return J({"type": "wallet", "id": q})
    if q.lower() in d["transactions"].index:
        return J({"type": "transaction", "id": q.lower()})
    m = d["wallets"].index[d["wallets"].index.str.startswith(q)]
    if len(m):
        return J({"type": "wallet", "id": m[0]})
    m = d["transactions"].index[d["transactions"].index.str.startswith(q.lower())]
    if len(m):
        return J({"type": "transaction", "id": m[0]})
    raise HTTPException(404, "No wallet or transaction matches that search.")


def _tx_brief(txid, d, focus=None):
    r = d["transactions"].loc[txid]
    direction = None
    amt = None
    if focus:
        if focus in r.inputs:
            direction = "out"
            amt = sum(v for a, v in zip(r.outputs, r.out_amts) if a != focus and a not in r.inputs)
        else:
            direction = "in"
            amt = sum(v for a, v in zip(r.outputs, r.out_amts) if a == focus)
    return {"txid": txid, "time": r.time, "n_in": int(r.n_in), "n_out": int(r.n_out),
            "total": float(r.total_out), "fee": float(r.fee), "coinjoin": bool(r.coinjoin),
            "origin_ip": r.origin_ip if isinstance(r.origin_ip, str) else None,
            "country": r.country if isinstance(r.country, str) else None,
            "asn": r.asn if isinstance(r.asn, str) else None, "direction": direction, "amount": amt}


@app.get("/api/wallet/{address}")
def wallet(address: str):
    d = db()
    w = d["wallets"]
    if address not in w.index:
        raise HTTPException(404, "Wallet not found")
    r = w.loc[address]
    meta = d["meta"]
    wts = meta["weights"]
    comps = [{"key": k, "label": pl.COMPONENT_LABELS[k], "value": float(r[k]),
              "points": round(float(wts[k] * r[k] * 100), 1), "weight": wts[k]} for k in "PAGT"]
    txs = sorted(d["wallet_tx"].get(address, []), key=lambda x: d["transactions"].loc[x[0]].time)
    tx_list = [_tx_brief(tid, d, address) for tid, _ in txs][:60]
    ips = d["address_ip"][d["address_ip"].address == address][["ip", "K"]].to_dict("records")
    origin = {}
    for tid, side in txs:
        tr = d["transactions"].loc[tid]
        if side == "out" and isinstance(tr.origin_ip, str):
            o = origin.setdefault(tr.origin_ip, {"ip": tr.origin_ip, "country": tr.country, "asn": tr.asn,
                                                 "asn_type": tr.asn_type, "tx": 0, "max_P": 0})
            o["tx"] += 1
            o["max_P"] = max(o["max_P"], float(tr.origin_P))
    for o in origin.values():
        o["K"] = next((float(x["K"]) for x in ips if x["ip"] == o["ip"]), float(o["tx"]))
        o["linked_wallet"] = o["K"] >= pl.K_LINK
    ent = w[w.entity_id == r.entity_id]
    related = []
    for k in ("out_edges", "in_edges"):
        g = d[k].get(address)
        if g is not None:
            col = "dst" if k == "out_edges" else "src"
            for a in g[col].unique():
                if a in w.index and w.loc[a].band in ("HIGH", "MEDIUM") and a != address:
                    related.append(_wallet_row(a, w.loc[a]))
    related = sorted({x["address"]: x for x in related}.values(), key=lambda x: -x["score"])[:8]
    feats = {k: float(r[k]) for k in pl.FEATURES}
    return J({
        "address": address, "score": round(float(r.score), 1), "band": r.band, "is_seed": bool(r.is_seed),
        "is_service_hub": bool(r.is_service_hub), "entity_id": r.entity_id,
        "cluster_size": int(r.cluster_size), "cluster_confidence": float(r.cluster_confidence),
        "components": comps, "factors": _jl(r.factors, []), "reasons": _jl(r.reasons, []),
        "patterns": {pl.PATTERN_LABELS[k]: v for k, v in _jl(r.patterns, {}).items()},
        "hops_to_seed": None if pd.isna(r.hops_to_seed) else int(r.hops_to_seed),
        "nearest_seed": r.nearest_seed if isinstance(r.nearest_seed, str) else None,
        "features": feats, "transactions": tx_list, "n_transactions": len(txs),
        "origin_ips": sorted(origin.values(), key=lambda x: -x["tx"]),
        "owner_group": {"size": int(len(ent)), "high": int((ent.band == "HIGH").sum()),
                        "members": ent.sort_values("score", ascending=False).head(6).index.tolist()},
        "related": related, "bands": meta["bands"],
    })


@app.get("/api/risk/{address}")
def risk(address: str):
    x = wallet(address)
    return x


@app.get("/api/explanation/{address}")
def explanation(address: str):
    d = db()
    if address not in d["wallets"].index:
        raise HTTPException(404, "Wallet not found")
    r = d["wallets"].loc[address]
    return J({"address": address, "score": float(r.score), "band": r.band, "reasons": _jl(r.reasons, []),
              "factors": _jl(r.factors, [])})


@app.get("/api/graph/{address}")
def graph(address: str, depth: int = 2, limit: int = 55):
    d = db()
    w = d["wallets"]
    if address not in w.index:
        raise HTTPException(404, "Wallet not found")
    nodes, edges = {address: 0}, {}
    frontier = [address]
    for lvl in range(1, depth + 1):
        nxt = []
        for a in frontier:
            if w.loc[a].is_service_hub and a != address:
                continue
            for k, col in (("out_edges", "dst"), ("in_edges", "src")):
                g = d[k].get(a)
                if g is None:
                    continue
                g = g.sort_values("amount", ascending=False).head(25)
                for e in g.itertuples():
                    b = getattr(e, col)
                    key = (e.src, e.dst)
                    if b not in nodes:
                        if len(nodes) >= limit:
                            continue
                        nodes[b] = lvl
                        nxt.append(b)
                    edges[key] = edges.get(key, 0) + e.amount
        frontier = nxt
    out_nodes = []
    for a, lvl in nodes.items():
        r = w.loc[a] if a in w.index else None
        out_nodes.append({"id": a, "label": a[:6] + "…" + a[-4:], "type": "wallet", "level": lvl,
                          "score": round(float(r.score), 1) if r is not None else 0,
                          "band": r.band if r is not None else "LOW",
                          "seed": bool(r.is_seed) if r is not None else False,
                          "hub": bool(r.is_service_hub) if r is not None else False,
                          "focus": a == address})
    out_edges = [{"source": s, "target": t, "amount": round(v, 4)} for (s, t), v in edges.items()
                 if s in nodes and t in nodes]
    # IP nodes for the focus wallet
    for tid, side in d["wallet_tx"].get(address, []):
        tr = d["transactions"].loc[tid]
        if side == "out" and isinstance(tr.origin_ip, str):
            ip_id = "ip:" + tr.origin_ip
            if ip_id not in {n["id"] for n in out_nodes}:
                out_nodes.append({"id": ip_id, "label": tr.origin_ip, "type": "ip",
                                  "country": tr.country, "asn": tr.asn, "asn_type": tr.asn_type})
            out_edges.append({"source": ip_id, "target": address, "amount": 0, "ip": True})
    return J({"nodes": out_nodes, "edges": out_edges})


@app.get("/api/transactions")
def transactions(txid: str = "", address: str = "", limit: int = 50):
    d = db()
    if txid:
        if txid.lower() not in d["transactions"].index:
            raise HTTPException(404, "Transaction not found")
        r = d["transactions"].loc[txid.lower()]
        w = d["wallets"]
        def side(addrs, amts):
            return [{"address": a, "amount": float(v), "score": float(w.loc[a].score) if a in w.index else 0,
                     "band": w.loc[a].band if a in w.index else "LOW"} for a, v in zip(addrs, amts)]
        b = _tx_brief(txid.lower(), d)
        b.update({"inputs": side(r.inputs, r.in_amts), "outputs": side(r.outputs, r.out_amts),
                  "origin_P": None if pd.isna(r.origin_P) else float(r.origin_P), "script_type": r.script_type})
        return J(b)
    if address:
        txs = d["wallet_tx"].get(address, [])
        return J([_tx_brief(t, d, address) for t, _ in txs][:limit])
    t = d["transactions"].sort_values("time", ascending=False).head(limit)
    return J([_tx_brief(x, d) for x in t.index])


@app.get("/api/entity/{entity_id}")
def entity(entity_id: str):
    w = db()["wallets"]
    e = w[w.entity_id == entity_id]
    if e.empty:
        raise HTTPException(404, "Owner group not found")
    return J({"entity_id": entity_id, "size": int(len(e)), "max_score": float(e.score.max()),
              "wallets": [_wallet_row(a, r) for a, r in e.sort_values("score", ascending=False).head(50).iterrows()]})


# ------------------------------------------------------------- datasets / run
@app.get("/api/datasets")
def datasets():
    out = []
    for folder in (DATA, UPLOADS):
        if not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            if fn.lower().endswith((".csv", ".json", ".xml")) and "seeds" not in fn and fn != "geo_db.csv" \
                    and not fn.startswith("training"):
                p = os.path.join(folder, fn)
                out.append({"name": fn, "path": os.path.relpath(p, DATA), "size_mb": round(os.path.getsize(p) / 1e6, 2),
                            "uploaded": folder == UPLOADS})
    seeds = [fn for fn in sorted(os.listdir(DATA)) if fn.endswith("_seeds.csv") and not fn.startswith("training")]
    if os.path.isdir(UPLOADS):
        seeds += ["uploads/" + fn for fn in sorted(os.listdir(UPLOADS)) if "seeds" in fn]
    return J({"datasets": out, "seeds": seeds})


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    os.makedirs(UPLOADS, exist_ok=True)
    name = os.path.basename(file.filename or "upload.csv")
    if not name.lower().endswith((".csv", ".json", ".xml")):
        raise HTTPException(400, "Please upload a CSV, JSON or XML file.")
    dest = os.path.join(UPLOADS, name)
    with open(dest, "wb") as fh:
        shutil.copyfileobj(file.file, fh)
    return J({"ok": True, "path": os.path.relpath(dest, DATA)})


class RunReq(BaseModel):
    dataset: str = "case_dataset.csv"
    seeds: str = "case_dataset_seeds.csv"


def run_analysis(dataset_rel, seeds_rel):
    st = STATE["status"]
    try:
        model = train.load_model(ROOT)
        if model is None:
            raise RuntimeError("Model not found. Run setup again (it trains the model).")
        path = os.path.join(DATA, dataset_rel)
        seeds = os.path.join(DATA, seeds_rel) if seeds_rel else None

        def progress(i, name):
            st["stage"] = i
            st["timings"][name] = None

        res = pl.analyse(path, seeds, os.path.join(DATA, "geo_db.csv"), model, progress=progress)
        st["stage"] = len(pl.STAGES) - 1
        stem = os.path.splitext(os.path.basename(dataset_rel))[0]
        labels = os.path.join(DATA, "ground_truth", f"{stem}_labels.csv")
        ev = pl.evaluate(res, labels)
        t0 = time.time()
        store.save(res, DB, os.path.basename(dataset_rel), ev)
        res["timings"]["Save"] = round(time.time() - t0, 2)
        STATE["db"] = None
        db()
        st["timings"] = {k: v for k, v in res["timings"].items()}
        st["elapsed"] = round(time.time() - st["started"], 1)
        st["done"] = True
    except Exception as ex:          # surfaced in the UI
        st["error"] = str(ex)
    finally:
        st["running"] = False


@app.post("/api/analyze")
def analyze(req: RunReq):
    with LOCK:
        if STATE["status"]["running"]:
            raise HTTPException(409, "An analysis is already running.")
        if not os.path.exists(os.path.join(DATA, req.dataset)):
            raise HTTPException(404, "Dataset not found")
        STATE["status"] = {"running": True, "stage": 0, "stages": pl.STAGES, "error": None, "done": False,
                           "started": time.time(), "timings": {}, "dataset": req.dataset}
    threading.Thread(target=run_analysis, args=(req.dataset, req.seeds), daemon=True).start()
    return J({"ok": True})


@app.get("/api/status")
def status():
    return J(STATE["status"])


# ------------------------------------------------------------------- static
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
