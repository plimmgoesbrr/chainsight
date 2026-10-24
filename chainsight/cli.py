"""Command-line entry points.

  python -m chainsight.cli prepare   generate data (if missing), train model, run first analysis
  python -m chainsight.cli serve     start the dashboard on http://127.0.0.1:8000
"""
import os
import sys
import time
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def prepare(force=False):
    from . import generate, pipeline as pl, store, train
    if force or not os.path.exists(os.path.join(DATA, "case_dataset.csv")):
        print("[1/3] Generating synthetic datasets ...")
        generate.generate_all(DATA)
    else:
        print("[1/3] Datasets found")
    if force or train.load_model(ROOT) is None:
        print("[2/3] Training the model ...")
        train.train(ROOT)
    else:
        print("[2/3] Model found")
    db = os.path.join(DATA, "chainsight.duckdb")
    if force or not os.path.exists(db):
        print("[3/3] Running the first analysis ...")
        model = train.load_model(ROOT)
        res = pl.analyse(os.path.join(DATA, "case_dataset.csv"), os.path.join(DATA, "case_dataset_seeds.csv"),
                         os.path.join(DATA, "geo_db.csv"), model)
        ev = pl.evaluate(res, os.path.join(DATA, "ground_truth", "case_dataset_labels.csv"))
        store.save(res, db, "case_dataset.csv", ev)
        print("      done in", res["elapsed"], "s")
    else:
        print("[3/3] Analysis found")


def _free_port(start=8000):
    import socket
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def serve(open_browser=True):
    import uvicorn
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    if open_browser:
        import threading
        threading.Timer(2.5, lambda: webbrowser.open(url)).start()
    print(f"\n  ChainSight is running at  {url}   (keep this window open; press Ctrl+C to stop)\n")
    uvicorn.run("chainsight.api:app", host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "prepare":
        prepare(force="--force" in sys.argv)
    elif cmd == "serve":
        serve(open_browser="--no-browser" not in sys.argv)
    else:
        print(__doc__)
