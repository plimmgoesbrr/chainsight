"""
Train the supervised model on the TRAINING dataset (different random world from the
case dataset), tune the score weights and band thresholds, and save everything
to models/.
"""
import itertools
import json
import os

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import average_precision_score
from sklearn.model_selection import StratifiedKFold

from . import pipeline as pl

PARAMS = {"objective": "binary:logistic", "eval_metric": "aucpr", "max_depth": 4, "eta": 0.08,
          "subsample": 0.85, "colsample_bytree": 0.85, "min_child_weight": 3, "seed": 0}
ROUNDS = 250


def train(root):
    data = os.path.join(root, "data")
    res = pl.analyse(os.path.join(data, "training_dataset.csv"),
                     os.path.join(data, "training_dataset_seeds.csv"),
                     os.path.join(data, "geo_db.csv"), model=None)
    f, sc = res["features"], res["scores"]
    lab = pd.read_csv(os.path.join(data, "ground_truth", "training_dataset_labels.csv"))
    y = f.index.isin(set(lab.address)).astype(int)
    X = f[pl.FEATURES].astype(float).values
    spw = (len(y) - y.sum()) / max(y.sum(), 1)
    params = dict(PARAMS, scale_pos_weight=float(spw) ** 0.5)

    # out-of-fold probabilities (honest P for weight tuning)
    oof = np.zeros(len(y))
    for tr_i, te_i in StratifiedKFold(5, shuffle=True, random_state=0).split(X, y):
        b = xgb.train(params, xgb.DMatrix(X[tr_i], label=y[tr_i], feature_names=pl.FEATURES), ROUNDS)
        oof[te_i] = b.predict(xgb.DMatrix(X[te_i], feature_names=pl.FEATURES))
    print("OOF PR-AUC (model alone):", round(average_precision_score(y, oof), 3))

    comp = pd.DataFrame({"P": oof, "A": sc.A.values, "G": sc.G.values, "T": sc["T"].values})
    mask = ~sc.is_seed.values
    best, best_w = -1, None
    grid = [round(x, 2) for x in np.arange(0.1, 0.61, 0.05)]
    for wp, wa, wg in itertools.product(grid, grid, grid):
        wt = round(1 - wp - wa - wg, 2)
        if wt < 0.1 - 1e-9:
            continue
        s = wp * comp.P + wa * comp.A + wg * comp.G + wt * comp["T"]
        ap = average_precision_score(y[mask], s[mask])
        if ap > best:
            best, best_w = ap, {"P": float(wp), "A": float(wa), "G": float(wg), "T": float(wt)}
    print("tuned weights:", best_w, "PR-AUC:", round(best, 3))

    score = 100 * sum(best_w[k] * comp[k] for k in "PAGT")
    s_m, y_m = score[mask].values, y[mask]
    # HIGH: smallest threshold with precision >= 0.9 ; MEDIUM: precision >= 0.6
    def thr(prec):
        order = np.argsort(-s_m)
        ys = y_m[order]
        cum = np.cumsum(ys) / np.arange(1, len(ys) + 1)
        ok = np.where(cum >= prec)[0]
        k = ok.max() if len(ok) else 0
        return float(np.floor(s_m[order][k]))
    bands = {"high": max(thr(0.9), 50.0), "medium": max(min(thr(0.6), thr(0.9) - 10), 30.0)}
    print("bands:", bands)

    booster = xgb.train(params, xgb.DMatrix(X, label=y, feature_names=pl.FEATURES), ROUNDS)
    os.makedirs(os.path.join(root, "models"), exist_ok=True)
    booster.save_model(os.path.join(root, "models", "xgb_model.json"))
    meta = {"weights": best_w, "bands": bands, "features": pl.FEATURES,
            "training": {"wallets": int(len(y)), "illicit": int(y.sum()),
                         "oof_pr_auc_model": round(float(average_precision_score(y, oof)), 3),
                         "oof_pr_auc_combined": round(float(best), 3)}}
    with open(os.path.join(root, "models", "meta.json"), "w") as fh:
        json.dump(meta, fh, indent=2)
    return meta


def load_model(root):
    p = os.path.join(root, "models", "xgb_model.json")
    if not os.path.exists(p):
        return None
    b = xgb.Booster()
    try:
        b.load_model(p)
    except Exception:
        print("Saved model could not be loaded with this XGBoost version - retraining ...")
        train(root)
        b = xgb.Booster()
        b.load_model(p)
    with open(os.path.join(root, "models", "meta.json")) as fh:
        meta = json.load(fh)
    return {"booster": b, "meta": meta}


if __name__ == "__main__":
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(train(here))
