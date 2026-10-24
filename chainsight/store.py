"""Persist a pipeline result to DuckDB and load it back for the API."""
import json
import os

import duckdb
import numpy as np
import pandas as pd

from . import pipeline as pl


def _j(x):
    return json.dumps(x, default=lambda o: o.item() if hasattr(o, "item") else str(o))


def save(result, db_path, dataset_name, evaluation=None):
    tx, sc, f = result["tx"], result["scores"], result["features"]
    wallets = f.join(sc).reset_index().rename(columns={"index": "address"})
    hops, nearest = result["hops"], result["nearest"]
    wallets["hops_to_seed"] = wallets["address"].map(hops).astype("float")
    wallets["nearest_seed"] = wallets["address"].map(nearest)
    pats, factors, reasons = result["patterns"], result.get("factors", {}), result.get("reasons", {})
    wallets["patterns"] = wallets["address"].map(lambda a: _j(pats.get(a, {})))
    wallets["factors"] = wallets["address"].map(lambda a: _j(factors.get(a, [])))
    wallets["reasons"] = wallets["address"].map(lambda a: _j(reasons.get(a, [])))
    txt = pd.DataFrame({
        "txid": tx.txid, "time": tx.time.astype(str), "n_in": tx.n_in, "n_out": tx.n_out,
        "total_in": tx.total_in, "total_out": tx.total_out, "fee": tx.fee, "script_type": tx.script_type,
        "coinjoin": tx.coinjoin, "origin_ip": tx.origin_ip, "origin_P": tx.origin_P, "country": tx.country,
        "asn": tx.asn, "asn_type": tx.asn_type,
        "inputs": tx.inputs.map(_j), "outputs": tx.outputs.map(_j),
        "in_amts": tx.in_amts.map(_j), "out_amts": tx.out_amts.map(_j)})
    edges = result["edges"].copy()
    edges["time"] = edges["time"].astype(str)
    entities = wallets.groupby("entity_id").agg(size=("address", "count"), max_score=("score", "max"),
                                                n_high=("band", lambda b: int((b == "HIGH").sum())),
                                                confidence=("cluster_confidence", "first")).reset_index()
    addr_ip = result["addr_ip"].rename(columns={"inputs": "address"})
    meta = {"dataset": dataset_name, "stats": result["stats"], "timings": result["timings"],
            "elapsed": result.get("elapsed"), "weights": result["weights"], "bands": result["bands"],
            "evaluation": evaluation, "time_min": str(tx.time.min()), "time_max": str(tx.time.max())}
    tmp = db_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = duckdb.connect(tmp)
    for name, df in [("wallets", wallets), ("transactions", txt), ("edges", edges),
                     ("entities", entities), ("address_ip", addr_ip)]:
        con.register("df_view", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM df_view")
        con.unregister("df_view")
    con.execute("CREATE TABLE meta (k VARCHAR, v VARCHAR)")
    con.execute("INSERT INTO meta VALUES ('run', ?)", [_j(meta)])
    con.close()
    os.replace(tmp, db_path)
    return meta


def load(db_path):
    if not os.path.exists(db_path):
        return None
    con = duckdb.connect(db_path, read_only=True)
    d = {t: con.execute(f"SELECT * FROM {t}").df() for t in
         ["wallets", "transactions", "edges", "entities", "address_ip"]}
    d["meta"] = json.loads(con.execute("SELECT v FROM meta WHERE k='run'").fetchone()[0])
    con.close()
    w = d["wallets"].set_index("address")
    d["wallets"] = w
    t = d["transactions"].set_index("txid")
    for c in ["inputs", "outputs", "in_amts", "out_amts"]:
        t[c] = t[c].map(json.loads)
    d["transactions"] = t
    e = d["edges"]
    d["out_edges"] = {k: g for k, g in e.groupby("src")}
    d["in_edges"] = {k: g for k, g in e.groupby("dst")}
    # wallet -> txids
    io = {}
    for txid, r in t.iterrows():
        for a in r.inputs:
            io.setdefault(a, []).append((txid, "out"))
        for a in r.outputs:
            io.setdefault(a, []).append((txid, "in"))
    d["wallet_tx"] = io
    return d
