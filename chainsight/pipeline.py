"""
ChainSight analysis pipeline.

 1 Ingest & normalise    CSV / JSON / XML -> clean observation + transaction tables
 2 Enrich                offline geo / ASN lookup for every IP
 3 IP linking            timing-based matching degree P(tx, ip) and K(address, ip)
 4 Owner grouping        Union-Find with H1 / H2 / H3 heuristics + CoinJoin filter
 5 Features              behaviour, graph and network features per wallet (address)
 6 Detect                XGBoost (P), Isolation Forest (A), Personalised PageRank (G),
                         laundering-pattern detectors (T)
 7 Score                 Risk = 100 x (w1 P + w2 A + w3 G + w4 T)
 8 Explain               XGBoost TreeSHAP contributions -> real values -> sentence templates
 9 Store                 DuckDB
"""
import json
import math
import os
import time
from collections import defaultdict

import networkx as nx
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

FAST_MIN = 30          # "moved on quickly" window, minutes
P_LINK = 1.5           # IP -> transaction link threshold (origin announces to several peers at once)
K_LINK = 1.5           # IP -> wallet link threshold
TAU_SEC = 2.0          # timing decay for the matching degree
HUB_DEGREE = 60        # service hubs (exchanges) are not used to spread risk

FEATURES = [
    "n_recv", "n_sent", "total_recv", "total_sent", "fast_out_share", "median_hold_min",
    "max_fan_out", "n_senders", "n_receivers", "tx_per_day", "mean_gap_hours", "life_span_hours",
    "avg_fee_rate", "pagerank", "cluster_size", "n_countries", "hosting_share", "coinjoin_count",
    "max_fan_in_48h",
]
FEATURE_LABELS = {
    "n_recv": "Number of incoming payments", "n_sent": "Number of outgoing payments",
    "total_recv": "Amount received", "total_sent": "Amount moved on",
    "fast_out_share": "Fast outflow", "median_hold_min": "Short holding time",
    "max_fan_out": "Fan-out", "n_senders": "Many senders", "n_receivers": "Many receivers",
    "tx_per_day": "Transaction frequency", "mean_gap_hours": "Burst timing",
    "life_span_hours": "Short-lived wallet", "avg_fee_rate": "Fee pattern",
    "pagerank": "Network centrality", "cluster_size": "Owner-group size",
    "n_countries": "Many countries", "hosting_share": "Hosting / VPN IPs",
    "coinjoin_count": "Mixer use", "max_fan_in_48h": "Fan-in",
}
PATTERN_WEIGHTS = {"peeling_chain": 0.6, "rapid_movement": 0.35, "fan_out": 0.35, "fan_in": 0.35,
                   "repeated_splitting": 0.45, "mixing": 0.35}
PATTERN_LABELS = {"peeling_chain": "Peeling chain", "rapid_movement": "Rapid movement",
                  "fan_out": "Fan-out", "fan_in": "Fan-in", "repeated_splitting": "Repeated splitting",
                  "mixing": "Mixing-like"}
DEFAULT_WEIGHTS = {"P": 0.40, "A": 0.20, "G": 0.25, "T": 0.15}
COMPONENT_LABELS = {"P": "Known-crime match", "A": "Unusual behaviour",
                    "G": "Near flagged wallets", "T": "Laundering pattern"}


# =========================================================== 1 ingest
def load_observations(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        df = pd.read_csv(path)
    elif ext == ".json":
        df = pd.read_json(path)
    elif ext == ".xml":
        df = pd.read_xml(path, parser="etree")
    else:
        raise ValueError(f"Unsupported file type: {ext} (use CSV, JSON or XML)")
    return df


def _as_list(v):
    if isinstance(v, list):
        return v
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return []
    s = str(v).strip()
    if s.startswith("["):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            s = s.strip("[]")
    return [x.strip().strip("'\"") for x in s.replace(";", ",").split(",") if x.strip()]


def normalise(raw):
    stats = {"rows_in": int(len(raw))}
    df = raw.copy()
    df.columns = [c.strip().lower() for c in df.columns]
    required = ["timestamp", "src_ip", "txid", "input_addresses", "output_addresses",
                "input_amounts", "output_amounts"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Dataset is missing required columns: {missing}")
    for c in ["dst_ip", "src_port", "dst_port", "fee", "script_type", "block_time"]:
        if c not in df.columns:
            df[c] = np.nan
    df = df.dropna(subset=["txid", "timestamp"])
    df["txid"] = df["txid"].astype(str).str.strip().str.lower()
    df["src_ip"] = df["src_ip"].astype(str).str.strip()
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce", format="ISO8601")
    df["block_ts"] = pd.to_datetime(df["block_time"], utc=True, errors="coerce", format="ISO8601")
    df = df.dropna(subset=["ts"])
    before = len(df)
    df = df.drop_duplicates(subset=["txid", "src_ip", "dst_ip", "timestamp"])
    stats["duplicates_removed"] = int(before - len(df))
    stats["rows_clean"] = int(len(df))

    # one row per transaction
    first = df.sort_values("ts").groupby("txid", as_index=False).first()
    tx = pd.DataFrame({
        "txid": first["txid"],
        "inputs": first["input_addresses"].map(_as_list),
        "outputs": first["output_addresses"].map(_as_list),
        "in_amts": first["input_amounts"].map(lambda v: [float(x) for x in _as_list(v)]),
        "out_amts": first["output_amounts"].map(lambda v: [float(x) for x in _as_list(v)]),
        "fee": pd.to_numeric(first["fee"], errors="coerce"),
        "script_type": first["script_type"].fillna("unknown"),
        "first_seen": first["ts"],
        "block_ts": first["block_ts"],
    })
    tx["inputs"] = tx["inputs"].map(lambda l: [str(a).strip() for a in l])
    tx["outputs"] = tx["outputs"].map(lambda l: [str(a).strip() for a in l])
    tx["total_in"] = tx["in_amts"].map(sum)
    tx["total_out"] = tx["out_amts"].map(sum)
    fee_missing = tx["fee"].isna()
    tx.loc[fee_missing, "fee"] = (tx["total_in"] - tx["total_out"]).clip(lower=0)
    stats["fees_filled"] = int(fee_missing.sum())
    valid = (tx["total_in"] + 1e-6 >= tx["total_out"]) & (tx["inputs"].map(len) > 0) & (tx["outputs"].map(len) > 0)
    stats["invalid_tx_dropped"] = int((~valid).sum())
    tx = tx[valid].reset_index(drop=True)
    tx["n_in"] = tx["inputs"].map(len)
    tx["n_out"] = tx["outputs"].map(len)
    tx["time"] = tx["first_seen"]
    obs = df[df["txid"].isin(set(tx["txid"]))][["txid", "ts", "src_ip", "dst_ip", "src_port", "dst_port"]]
    stats["transactions"] = int(len(tx))
    return obs.reset_index(drop=True), tx, stats


# =========================================================== 2 enrich
def load_geo(path):
    if path and os.path.exists(path):
        g = pd.read_csv(path, dtype=str)
        return {r.prefix: (r.country, r.asn, r.asn_type) for r in g.itertuples()}
    return {}


def geo_lookup(ip, geo):
    parts = str(ip).split(".")
    for n in (3, 2, 1):
        key = ".".join(parts[:n])
        if key in geo:
            return geo[key]
    return ("??", "unknown", "unknown")


# =========================================================== 3 IP linking
def link_ips(obs, tx, geo):
    """Matching degree P(tx, ip) = sum_j exp(-(TR_j - TR_min)/tau) over the IP's
    announcements of tx. The broadcasting node announces to several peers almost
    instantly (P >= 1); relays forward it seconds later (P < 1)."""
    o = obs.copy()
    tmin = o.groupby("txid")["ts"].transform("min")
    o["w"] = np.exp(-((o["ts"] - tmin).dt.total_seconds()) / TAU_SEC)
    p = o.groupby(["txid", "src_ip"], as_index=False)["w"].sum().rename(columns={"w": "P"})
    links = p[p["P"] >= P_LINK].copy()
    best = links.sort_values("P", ascending=False).drop_duplicates("txid")
    g = best["src_ip"].map(lambda ip: geo_lookup(ip, geo))
    best = best.assign(country=[x[0] for x in g], asn=[x[1] for x in g], asn_type=[x[2] for x in g])
    tx = tx.merge(best[["txid", "src_ip", "P", "country", "asn", "asn_type"]].rename(
        columns={"src_ip": "origin_ip", "P": "origin_P"}), on="txid", how="left")
    # K(address, ip): cumulative association over linked transactions spending from the address
    io = tx[["txid", "inputs", "origin_ip"]].dropna(subset=["origin_ip"]).explode("inputs")
    k = io.groupby(["inputs", "origin_ip"]).size().reset_index(name="K").rename(
        columns={"inputs": "address", "origin_ip": "ip"})
    k = k[k["K"] >= K_LINK]
    stats = {"ip_tx_links": int(len(best)), "ip_wallet_links": int(len(k)),
             "tx_with_origin": float(round(len(best) / max(len(tx), 1), 3))}
    return tx, links, k, stats


# =========================================================== 4 owner grouping
class UnionFind:
    def __init__(self):
        self.parent, self.h1, self.h2 = {}, defaultdict(int), defaultdict(int)

    def find(self, a):
        p = self.parent.setdefault(a, a)
        while p != self.parent[p]:
            self.parent[p] = self.parent[self.parent[p]]
            p = self.parent[p]
        self.parent[a] = p
        return p

    def union(self, a, b, rule):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra
            (self.h1 if rule == "H1" else self.h2)[ra] += 1 + self.h1.pop(rb, 0)
            self.h2[ra] += self.h2.pop(rb, 0)


def addr_type(a):
    return "bech32" if a.startswith("bc1") else ("p2sh" if a.startswith("3") else "p2pkh")


def detect_coinjoin(tx):
    def is_cj(r):
        if r.n_in < 3 or r.n_out < 3:
            return False
        vals = pd.Series(np.round(r.out_amts, 6)).value_counts()
        return vals.iloc[0] >= 3 and vals.iloc[0] >= 0.5 * r.n_out and len(set(r.inputs)) >= 3
    return tx.apply(is_cj, axis=1)


def group_owners(tx):
    uf = UnionFind()
    for a in set(a for l in tx["inputs"] for a in l) | set(a for l in tx["outputs"] for a in l):
        uf.find(a)
    t = tx.sort_values("time")
    # first appearance of every address as an output, and how often it is reused as output
    seen_out_count = defaultdict(int)
    for outs in t["outputs"]:
        for a in outs:
            seen_out_count[a] += 1
    seen_before = set()
    n_h1 = n_h2 = n_h3 = 0
    for r in t.itertuples():
        if r.coinjoin:
            seen_before.update(r.outputs)
            seen_before.update(r.inputs)
            continue
        ins = list(dict.fromkeys(r.inputs))
        for a in ins[1:]:                      # H1: multi-input -> same owner
            uf.union(ins[0], a, "H1")
            n_h1 += 1
        if r.n_out == 2 and ins:
            in_types = {addr_type(a) for a in ins}
            change = None
            if len(in_types) == 1:
                same = [a for a in r.outputs if addr_type(a) in in_types and a not in ins]
                if len(same) == 1:
                    c = same[0]
                    if c not in seen_before:                 # H2: one-time change address
                        change, rule = c, "H2"
                        if seen_out_count[c] > 1:            # H3: reused later -> not change
                            change = None
                            n_h3 += 1
            if change:
                uf.union(ins[0], change, rule)
                n_h2 += 1
        seen_before.update(r.outputs)
        seen_before.update(r.inputs)
    owner = {a: uf.find(a) for a in list(uf.parent)}
    sizes = pd.Series(owner).value_counts()
    conf = {}
    for root in sizes.index:
        h1, h2 = uf.h1.get(root, 0), uf.h2.get(root, 0)
        conf[root] = 1.0 if h1 + h2 == 0 else round(0.6 + 0.4 * h1 / (h1 + h2), 2)
    roots = {r: i for i, r in enumerate(sizes.index)}
    owners = pd.DataFrame({"address": list(owner.keys()),
                           "entity_id": [f"E{roots[r]:05d}" for r in owner.values()],
                           "cluster_size": [int(sizes[r]) for r in owner.values()],
                           "cluster_confidence": [conf[r] for r in owner.values()]})
    stats = {"entities": int(len(sizes)), "multi_address_entities": int((sizes > 1).sum()),
             "largest_entity": int(sizes.iloc[0]) if len(sizes) else 0,
             "h1_merges": n_h1, "h2_merges": n_h2, "h3_rejections": n_h3}
    return owners, stats


# =========================================================== 5 features
def io_tables(tx):
    rin = tx[["txid", "time", "inputs", "in_amts", "n_out", "fee", "total_in", "coinjoin",
              "asn_type", "country"]].copy()
    rin = rin.explode(["inputs", "in_amts"]).rename(columns={"inputs": "address", "in_amts": "amount"})
    rout = tx[["txid", "time", "outputs", "out_amts", "n_in", "coinjoin", "inputs"]].copy()
    rout = rout.explode(["outputs", "out_amts"]).rename(columns={"outputs": "address", "out_amts": "amount"})
    rin["amount"] = rin["amount"].astype(float)
    rout["amount"] = rout["amount"].astype(float)
    return rin, rout


def build_edges(tx):
    rows = []
    for r in tx.itertuples():
        tot = max(r.total_in, 1e-12)
        ins = list(zip(r.inputs, r.in_amts))
        for o, ov in zip(r.outputs, r.out_amts):
            if o in r.inputs:
                continue
            for i, iv in ins:
                rows.append((i, o, ov * iv / tot, r.txid, r.time))
    e = pd.DataFrame(rows, columns=["src", "dst", "amount", "txid", "time"])
    return e


def build_features(tx, edges, owners):
    rin, rout = io_tables(tx)
    f = pd.DataFrame(index=pd.Index(sorted(set(rin.address) | set(rout.address)), name="address"))
    f["n_recv"] = rout.groupby("address").size()
    f["n_sent"] = rin.groupby("address").size()
    f["total_recv"] = rout.groupby("address")["amount"].sum()
    f["total_sent"] = rin.groupby("address")["amount"].sum()
    # holding time: spend vs latest receipt before it
    recv = rout[["address", "time", "amount"]].sort_values("time")
    spend = rin[["address", "time", "amount"]].sort_values("time")
    m = pd.merge_asof(spend, recv.rename(columns={"time": "rt", "amount": "ramt"}),
                      left_on="time", right_on="rt", by="address", direction="backward")
    m["hold_min"] = (m["time"] - m["rt"]).dt.total_seconds() / 60
    m["fast"] = (m["hold_min"] <= FAST_MIN).astype(float) * m["amount"]
    f["fast_out_share"] = (m.groupby("address")["fast"].sum() / f["total_recv"]).clip(0, 1)
    f["median_hold_min"] = m.groupby("address")["hold_min"].median()
    f["max_fan_out"] = rin[~rin.coinjoin].groupby("address")["n_out"].max()
    f["n_senders"] = edges.groupby("dst")["src"].nunique()
    f["n_receivers"] = edges.groupby("src")["dst"].nunique()
    ev = pd.concat([rin[["address", "time"]], rout[["address", "time"]]]).sort_values("time")
    g = ev.groupby("address")["time"]
    span_h = (g.max() - g.min()).dt.total_seconds() / 3600
    f["life_span_hours"] = span_h
    f["tx_per_day"] = g.size() / np.maximum(span_h / 24, 1.0)
    ev["gap"] = g.diff().dt.total_seconds() / 3600
    f["mean_gap_hours"] = ev.groupby("address")["gap"].mean()
    rin["fee_rate"] = rin["fee"] / rin["total_in"].clip(lower=1e-9)
    f["avg_fee_rate"] = rin.groupby("address")["fee_rate"].mean()
    G = nx.DiGraph()
    G.add_weighted_edges_from(edges.groupby(["src", "dst"])["amount"].sum().reset_index().itertuples(index=False))
    pr = nx.pagerank(G, alpha=0.85, max_iter=60, tol=1e-6, weight=None)
    f["pagerank"] = pd.Series(pr) * len(pr)
    f = f.join(owners.set_index("address")[["entity_id", "cluster_size", "cluster_confidence"]])
    f["n_countries"] = rin.dropna(subset=["country"]).groupby("address")["country"].nunique()
    rin["hosting"] = (rin["asn_type"] == "hosting").astype(float)
    f["hosting_share"] = rin.dropna(subset=["asn_type"]).groupby("address")["hosting"].mean()
    cj = pd.concat([rin[rin.coinjoin]["address"], rout[rout.coinjoin]["address"]])
    f["coinjoin_count"] = cj.value_counts()
    # fan-in within 48h windows
    rr = edges[["dst", "src", "time"]].drop_duplicates(["dst", "src"]).sort_values("time")
    fan = {}
    for dst, grp in rr.groupby("dst"):
        if len(grp) < 5:
            fan[dst] = len(grp)
            continue
        t = grp["time"].values
        j, best = 0, 0
        for i in range(len(t)):
            while t[i] - t[j] > np.timedelta64(48, "h"):
                j += 1
            best = max(best, i - j + 1)
        fan[dst] = best
    f["max_fan_in_48h"] = pd.Series(fan)
    num = [c for c in FEATURES]
    f[num] = f[num].fillna(0)
    f["median_hold_min"] = f["median_hold_min"].where(f["n_sent"] > 0, 60 * 24 * 30)
    f["mean_gap_hours"] = f["mean_gap_hours"].where(f["mean_gap_hours"] > 0, 24 * 30)
    return f, G


# =========================================================== 6 detect
def graph_risk(G, seeds, degree):
    """Personalised PageRank that follows the money downstream from seed wallets.
    High-degree service hubs (exchanges) are removed so risk does not leak to everyone."""
    hubs = {n for n, d in degree.items() if d > HUB_DEGREE}
    H = G.subgraph([n for n in G.nodes if n not in hubs]).copy()
    seeds = [s for s in seeds if s in H]
    if not seeds:
        return pd.Series(0.0, index=list(G.nodes)), pd.Series(dtype=float), {}, hubs
    pers = {n: 0.0 for n in H.nodes}
    for s in seeds:
        pers[s] = 1.0 / len(seeds)
    ppr = nx.pagerank(H, alpha=0.85, personalization=pers, max_iter=100, tol=1e-8, weight=None,
                      dangling=pers)
    s = pd.Series(ppr)
    base = 1.0 / max(len(H), 1)
    s = s.clip(lower=base * 1e-6)
    g = (np.log10(s / base).clip(lower=0) / max(np.log10(s.max() / base), 1e-9)).clip(0, 1)
    # hop distance downstream from the nearest seed (for explanations)
    hops, nearest = {}, {}
    for sd in seeds:
        for n, d in nx.single_source_shortest_path_length(H, sd, cutoff=6).items():
            if n not in hops or d < hops[n]:
                hops[n], nearest[n] = d, sd
    g = g.reindex(list(G.nodes)).fillna(0.0)
    return g, pd.Series(hops), nearest, hubs


def detect_patterns(tx, f, edges):
    pats = defaultdict(dict)
    t = tx[~tx.coinjoin]
    # peeling chain: 1 input -> 2 outputs (small peel + large remainder), remainder repeats quickly
    nxt = {}
    for r in t[(t.n_in == 1) & (t.n_out == 2)].itertuples():
        tot = sum(r.out_amts)
        if tot <= 0:
            continue
        small, large = sorted(zip(r.out_amts, r.outputs))
        if small[0] / tot <= 0.15 and large[0] / tot >= 0.8:
            nxt[r.inputs[0]] = (large[1], r.time)
    chains = {}
    for start in nxt:
        if start in chains:
            continue
        path, cur = [start], start
        while cur in nxt and len(path) < 50:
            nb, tm = nxt[cur]
            if nb in nxt and (nxt[nb][1] - tm).total_seconds() > 3600:
                path.append(nb)
                break
            path.append(nb)
            cur = nb
        for a in path:
            chains[a] = max(chains.get(a, 0), len(path) - 1)
    for a, n in chains.items():
        if n >= 4:
            pats[a]["peeling_chain"] = {"hops": int(n)}
    # rapid movement
    rm = f[(f.fast_out_share >= 0.8) & (f.total_recv >= 0.01) & (f.n_sent >= 1) & (f.median_hold_min <= FAST_MIN)]
    for a, r in rm.iterrows():
        pats[a]["rapid_movement"] = {"pct": int(round(r.fast_out_share * 100)),
                                     "minutes": int(max(1, round(r.median_hold_min)))}
    for a, r in f[f.max_fan_out >= 10].iterrows():
        pats[a]["fan_out"] = {"n": int(r.max_fan_out)}
    for a, r in f[f.max_fan_in_48h >= 10].iterrows():
        pats[a]["fan_in"] = {"n": int(r.max_fan_in_48h)}
    # repeated splitting: spends in a split (>=3 outputs) after receiving from a split within 60 min
    split_tx = t[(t.n_out >= 3) & (t.n_out < 10)]
    split_out = {}
    for r in split_tx.itertuples():
        for o in r.outputs:
            split_out[o] = r.time
    for r in split_tx.itertuples():
        for a in r.inputs:
            if a in split_out and 0 <= (r.time - split_out[a]).total_seconds() <= 3600:
                pats[a]["repeated_splitting"] = {"n": int(r.n_out)}
                for o in r.outputs:
                    pats[o].setdefault("repeated_splitting", {"n": int(r.n_out)})
    for a, r in f[f.coinjoin_count > 0].iterrows():
        pats[a]["mixing"] = {"n": int(r.coinjoin_count)}
    T = pd.Series({a: min(1.0, sum(PATTERN_WEIGHTS[k] for k in p)) for a, p in pats.items()})
    return dict(pats), T.reindex(f.index).fillna(0.0)


def anomaly_score(X):
    iso = IsolationForest(n_estimators=200, contamination="auto", random_state=0)
    Xl = np.log1p(X.clip(lower=0))
    iso.fit(Xl)
    raw = -iso.score_samples(Xl)
    return pd.Series(raw, index=X.index).rank(pct=True)


# =========================================================== 8 explain
def _fmt(x, nd=2):
    return f"{x:,.{nd}f}"


def feature_sentence(feat, row):
    v = row[feat]
    if feat == "fast_out_share":
        if v < 0.5:
            return None
        return f"{int(round(v * 100))}% of the funds it received were moved on within {FAST_MIN} minutes."
    if feat == "median_hold_min":
        if v > 120:
            return None
        return f"It typically held funds for only {max(1, int(round(v)))} minutes before moving them."
    if feat == "max_fan_out":
        if v < 3:
            return None
        return f"It split funds across {int(v)} destination wallets in a single transaction."
    if feat == "n_senders":
        if v < 5:
            return None
        return f"It received funds from {int(v)} different wallets."
    if feat == "max_fan_in_48h":
        if v < 5:
            return None
        return f"{int(v)} different wallets paid into it within 48 hours."
    if feat == "n_receivers":
        if v < 5:
            return None
        return f"It sent funds to {int(v)} different wallets."
    if feat == "hosting_share":
        if v < 0.5:
            return None
        return f"{int(round(v * 100))}% of its transactions were broadcast from hosting / VPN IP ranges."
    if feat == "n_countries":
        if v < 2:
            return None
        return f"Its transactions were broadcast from {int(v)} different countries."
    if feat == "life_span_hours":
        if v > 72:
            return None
        return f"All of its activity happened within {max(1, int(round(v)))} hours."
    if feat == "tx_per_day":
        if v < 5:
            return None
        return f"It made about {_fmt(v, 1)} transactions per day while active."
    if feat == "coinjoin_count":
        return f"It took part in {int(v)} mixing (CoinJoin-like) transaction(s)."
    if feat == "mean_gap_hours" and v <= 6:
        return f"Its transactions came in quick bursts, on average {_fmt(v, 1)} hours apart."
    if feat == "cluster_size" and v >= 3:
        return f"It belongs to an owner group of {int(v)} wallets."
    return None      # amounts, counts and centrality add points but are not shown as reasons


def pattern_sentence(k, d):
    if k == "peeling_chain":
        return f"It is part of a peeling chain of {d['hops']} hops that shaves off small amounts at each step."
    if k == "rapid_movement":
        return f"{d['pct']}% of received funds left within {FAST_MIN} minutes (typical hold: {d['minutes']} min)."
    if k == "fan_out":
        return f"Funds were split across {d['n']} destination wallets."
    if k == "fan_in":
        return f"{d['n']} wallets paid into it within 48 hours."
    if k == "repeated_splitting":
        return f"Funds were repeatedly split into {d['n']}-way pieces, hop after hop."
    if k == "mixing":
        return "It took part in a mixing (CoinJoin-like) transaction with equal-value outputs."
    return None


def explain(scores, f, contribs, patterns, hops, nearest, seeds_set, weights):
    """Points per factor + plain-English reasons, built only from stored values."""
    reasons, factors = {}, {}
    for a, r in scores.iterrows():
        fr = []
        # model points split by TreeSHAP contributions
        p_pts = weights["P"] * r.P * 100
        c = contribs.loc[a] if a in contribs.index else None
        if c is not None and p_pts > 0:
            pos = c[c > 0]
            if pos.sum() > 0:
                share = pos / pos.sum()
                for feat, sh in share.sort_values(ascending=False).items():
                    pts = p_pts * sh
                    if pts >= 0.5:
                        fr.append({"factor": FEATURE_LABELS.get(feat, feat), "points": round(pts, 1),
                                   "component": "P", "feature": feat,
                                   "sentence": feature_sentence(feat, f.loc[a])})
            else:
                fr.append({"factor": "Model baseline", "points": round(p_pts, 1), "component": "P"})
        a_pts = weights["A"] * r.A * 100
        if a_pts >= 0.5:
            fr.append({"factor": "Unusual behaviour", "points": round(a_pts, 1), "component": "A",
                       "sentence": f"Its overall behaviour is more unusual than {min(99, int(r.A * 100))}% of wallets in this dataset."})
        g_pts = weights["G"] * r.G * 100
        if g_pts >= 0.5:
            if a in seeds_set:
                s = "It is a known flagged wallet (listed in a public ransomware / fraud report)."
            elif a in nearest:
                h = int(hops[a])
                s = (f"It receives money {h} hop{'s' if h != 1 else ''} downstream of flagged wallet "
                     f"{nearest[a][:12]}…")
            else:
                s = "It is closely connected to flagged wallets."
            fr.append({"factor": "Near flagged wallets", "points": round(g_pts, 1), "component": "G",
                       "sentence": s})
        t_pts = weights["T"] * r["T"] * 100
        pp = patterns.get(a, {})
        if t_pts >= 0.5 and pp:
            tot_w = sum(PATTERN_WEIGHTS[k] for k in pp)
            for k, d in pp.items():
                fr.append({"factor": PATTERN_LABELS[k] + " pattern", "points": round(t_pts * PATTERN_WEIGHTS[k] / tot_w, 1),
                           "component": "T", "sentence": pattern_sentence(k, d)})
        fr.sort(key=lambda x: -x["points"])
        factors[a] = fr
        sents, used = [], set()
        for x in fr:
            s = x.get("sentence")
            if s and x["points"] >= 2 and s not in used:
                sents.append(s)
                used.add(s)
            if len(sents) == 5:
                break
        reasons[a] = sents
    return factors, reasons


# =========================================================== orchestration
STAGES = ["Ingest & clean", "Geo enrichment", "Link IPs to transactions", "Group wallets by owner",
          "Measure behaviour", "Detect (AI + graph + patterns)", "Score 0-100", "Explain", "Save"]


def analyse(dataset_path, seeds_path, geo_path, model, progress=None, fit_iforest=True):
    """Runs the full pipeline. `model` is a dict from train.load_model() or None
    (feature-only run used during training)."""
    t_start = time.time()
    timings = {}

    def step(i, name):
        if progress:
            progress(i, name)
        timings[name] = time.time()

    def done(name):
        timings[name] = round(time.time() - timings[name], 2)

    step(0, STAGES[0])
    raw = load_observations(dataset_path)
    obs, tx, stats = normalise(raw)
    done(STAGES[0])

    step(1, STAGES[1])
    geo = load_geo(geo_path)
    done(STAGES[1])

    step(2, STAGES[2])
    tx, ip_links, addr_ip, s_ip = link_ips(obs, tx, geo)
    stats.update(s_ip)
    done(STAGES[2])

    step(3, STAGES[3])
    tx["coinjoin"] = detect_coinjoin(tx)
    owners, s_own = group_owners(tx)
    stats.update(s_own)
    stats["coinjoin_tx"] = int(tx["coinjoin"].sum())
    done(STAGES[3])

    step(4, STAGES[4])
    edges = build_edges(tx)
    f, G = build_features(tx, edges, owners)
    stats["wallets"] = int(len(f))
    done(STAGES[4])

    step(5, STAGES[5])
    seeds = []
    if seeds_path and os.path.exists(seeds_path):
        sd = pd.read_csv(seeds_path)
        seeds = [s for s in sd["address"].astype(str).str.strip() if s in f.index]
    stats["seed_wallets"] = len(seeds)
    degree = dict(G.degree())
    Gs, hops, nearest, hubs = graph_risk(G, seeds, degree)
    stats["service_hubs"] = len(hubs)
    X = f[FEATURES].astype(float)
    A = anomaly_score(X) if fit_iforest else pd.Series(0.0, index=f.index)
    patterns, T = detect_patterns(tx, f, edges)
    if model is not None:
        import xgboost as xgb
        dm = xgb.DMatrix(X.values, feature_names=FEATURES)
        P = pd.Series(model["booster"].predict(dm), index=f.index)
        contrib = model["booster"].predict(dm, pred_contribs=True)
        contribs = pd.DataFrame(contrib[:, :-1], index=f.index, columns=FEATURES)
        weights = model["meta"]["weights"]
        bands = model["meta"]["bands"]
    else:
        P = pd.Series(0.0, index=f.index)
        contribs = pd.DataFrame(0.0, index=f.index, columns=FEATURES)
        weights, bands = DEFAULT_WEIGHTS, {"high": 70, "medium": 45}
    done(STAGES[5])

    step(6, STAGES[6])
    sc = pd.DataFrame({"P": P, "A": A, "G": Gs.reindex(f.index).fillna(0.0), "T": T})
    sc["score"] = 100 * (weights["P"] * sc.P + weights["A"] * sc.A + weights["G"] * sc.G + weights["T"] * sc["T"])
    sc["band"] = np.where(sc.score >= bands["high"], "HIGH", np.where(sc.score >= bands["medium"], "MEDIUM", "LOW"))
    sc["is_seed"] = sc.index.isin(seeds)
    sc["is_service_hub"] = sc.index.isin(hubs)
    done(STAGES[6])

    result = {"obs": obs, "tx": tx, "edges": edges, "features": f, "scores": sc, "patterns": patterns,
              "hops": hops, "nearest": nearest, "seeds": seeds, "ip_links": ip_links, "addr_ip": addr_ip,
              "contribs": contribs, "weights": weights, "bands": bands, "stats": stats}
    if model is None:
        result["timings"] = timings
        return result

    step(7, STAGES[7])
    flagged = sc[sc.score >= bands["medium"] * 0.6]
    factors, reasons = explain(flagged, f, contribs, patterns, hops, nearest, set(seeds), weights)
    result["factors"], result["reasons"] = factors, reasons
    done(STAGES[7])
    result["timings"] = timings
    result["elapsed"] = round(time.time() - t_start, 1)
    return result


def evaluate(result, labels_path):
    """Compare against planted ground truth (never used by the pipeline itself)."""
    if not labels_path or not os.path.exists(labels_path):
        return None
    lab = pd.read_csv(labels_path)
    sc = result["scores"].copy()
    y = sc.index.isin(set(lab.address)).astype(int)
    sc["y"] = y
    ev = sc[~sc.is_seed]                                  # seeds are given, not discovered
    from sklearn.metrics import average_precision_score
    out = {"wallets_evaluated": int(len(ev)), "illicit_wallets": int(ev.y.sum())}
    top = ev.sort_values("score", ascending=False)
    for k in (50, 100):
        out[f"precision_at_{k}"] = round(float(top.head(k).y.mean()), 3)
    flagged = ev.band.isin(["HIGH", "MEDIUM"])
    high = ev.band == "HIGH"
    out["recall_flagged"] = round(float((flagged & (ev.y == 1)).sum() / max(ev.y.sum(), 1)), 3)
    out["precision_flagged"] = round(float((flagged & (ev.y == 1)).sum() / max(flagged.sum(), 1)), 3)
    out["recall_high"] = round(float((high & (ev.y == 1)).sum() / max(ev.y.sum(), 1)), 3)
    out["precision_high"] = round(float((high & (ev.y == 1)).sum() / max(high.sum(), 1)), 3)
    out["false_alarm_rate"] = round(float((flagged & (ev.y == 0)).sum() / max((ev.y == 0).sum(), 1)), 4)
    out["pr_auc"] = round(float(average_precision_score(ev.y, ev.score)), 3)
    lab_i = lab.set_index("address")
    per = {}
    for typ, grp in lab_i.groupby("typology"):
        idx = [a for a in grp.index if a in ev.index]
        if idx:
            per[typ] = round(float(ev.loc[idx].band.isin(["HIGH", "MEDIUM"]).mean()), 3)
    out["recall_by_pattern"] = per
    cases = lab.groupby("case_id")["address"].apply(list)
    caught = sum(1 for addrs in cases if sc.reindex(addrs).band.isin(["HIGH"]).any())
    out["cases_total"] = int(len(cases))
    out["cases_caught"] = int(caught)
    return out
