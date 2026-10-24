"""
Synthetic Bitcoin transaction + P2P network metadata generator.

Produces a dataset shaped like the SIH26146 spec: one row per network
observation of a transaction (timestamp, src/dst IP + port, TXID, input and
output addresses and amounts, fee, script type, block time).

Normal activity (individuals, merchants, exchanges, privacy users of a mixer)
is mixed with planted laundering cases:
  peeling chains, rapid fan-out, fan-in collection, mixing then cash-out,
  repeated splitting.

Ground truth (labels.csv) is written separately and is ONLY used to train the
supervised model and to evaluate results - never as an input feature.
seeds.csv lists a few addresses "already known to be illicit" (e.g. from
public ransomware reports) - the starting points for risk propagation.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
SCRIPT_TYPES = ["P2WPKH", "P2PKH", "P2SH", "P2TR"]

# --- small offline geo database (prefix -> country, ASN, type) -------------
GEO_ROWS = [
    # residential / mobile (mostly India, some abroad)
    ("49.36", "IN", "AS45609 Bharti Airtel", "residential"),
    ("117.96", "IN", "AS9829 BSNL", "residential"),
    ("106.51", "IN", "AS24560 Airtel Broadband", "residential"),
    ("157.32", "IN", "AS55836 Reliance Jio", "mobile"),
    ("103.21", "IN", "AS17488 Hathway", "residential"),
    ("81.2", "GB", "AS2856 BT", "residential"),
    ("73.15", "US", "AS7922 Comcast", "residential"),
    ("92.40", "DE", "AS3320 Deutsche Telekom", "residential"),
    ("110.33", "SG", "AS9506 Singtel", "residential"),
    # hosting / VPN ranges
    ("185.220", "DE", "AS208294 Hosting-VPN", "hosting"),
    ("45.153", "NL", "AS206264 Amarutu Hosting", "hosting"),
    ("194.26", "RU", "AS48282 Hosting-VDS", "hosting"),
    ("91.219", "RO", "AS9009 M247 Hosting", "hosting"),
    ("104.244", "US", "AS53667 FranTech Hosting", "hosting"),
    ("5.255", "SC", "AS213035 Offshore Hosting", "hosting"),
    # bitcoin relay nodes (data-centres)
    ("88.99", "FI", "AS24940 Hetzner", "datacenter"),
    ("51.15", "FR", "AS16276 OVH", "datacenter"),
    ("34.89", "US", "AS15169 Google Cloud", "datacenter"),
]
RESIDENTIAL = [r for r in GEO_ROWS if r[3] in ("residential", "mobile")]
RES_IN = [r for r in RESIDENTIAL if r[1] == "IN"]
HOSTING = [r for r in GEO_ROWS if r[3] == "hosting"]
NODES = [r for r in GEO_ROWS if r[3] == "datacenter"]


def write_geo_db(path):
    pd.DataFrame(GEO_ROWS, columns=["prefix", "country", "asn", "asn_type"]).to_csv(path, index=False)


class Generator:
    def __init__(self, seed=42, n_people=700, n_merchants=40, n_exchanges=5,
                 days=21, start="2026-08-01", n_cases=30):
        self.rng = np.random.default_rng(seed)
        self.t0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
        self.days = days
        self.n_people, self.n_merchants, self.n_exchanges = n_people, n_merchants, n_exchanges
        self.n_cases = n_cases
        self.res_share = float(self.rng.uniform(0.3, 0.45))     # criminals on home connections
        self.hop_max = float(self.rng.uniform(25, 90))           # how fast launderers move funds
        self.entities = []          # dicts: id, kind, addrs, ips
        self.txs = []               # dicts
        self.labels = {}            # address -> (typology, case_id)
        self.seeds = []
        self.relay_ips = [self._ip(NODES[self.rng.integers(len(NODES))]) for _ in range(60)]

    # ---------------------------------------------------------------- ids
    def _kind(self):
        return str(self.rng.choice(["bech32", "p2pkh", "p2sh"], p=[0.6, 0.25, 0.15]))

    def _addr(self, kind=None):
        r = self.rng
        if kind is not None and r.random() < 0.08:      # wallets occasionally mix types
            kind = None
        k = kind or self._kind()
        if k == "bech32":
            return "bc1q" + "".join(r.choice(list(B32), 38))
        if k == "p2pkh":
            return "1" + "".join(r.choice(list(B58), 33))
        return "3" + "".join(r.choice(list(B58), 33))

    def _txid(self):
        return "".join(self.rng.choice(list("0123456789abcdef"), 64))

    def _ip(self, geo_row):
        a = geo_row[0]
        parts = a.split(".")
        while len(parts) < 4:
            parts.append(str(self.rng.integers(1, 254)))
        return ".".join(parts)

    def _time(self, lo_day=0.0, hi_day=None):
        hi_day = self.days if hi_day is None else hi_day
        return self.t0 + timedelta(days=float(self.rng.uniform(lo_day, hi_day)))

    # ------------------------------------------------------------ entities
    def _entity(self, kind, n_addr=1, ips=None):
        ak = self._kind()
        e = {"id": len(self.entities), "kind": kind, "akind": ak,
             "addrs": [self._addr(ak) for _ in range(n_addr)],
             "ips": ips or [self._ip(RES_IN[self.rng.integers(len(RES_IN))])
                            if self.rng.random() < 0.85 else
                            self._ip(RESIDENTIAL[self.rng.integers(len(RESIDENTIAL))])]}
        self.entities.append(e)
        return e

    def _build_population(self):
        self.people = [self._entity("person", int(self.rng.integers(1, 5))) for _ in range(self.n_people)]
        for p in self.people:
            if self.rng.random() < 0.10:            # privacy-minded honest users on VPNs
                p["ips"].append(self._ip(HOSTING[self.rng.integers(len(HOSTING))]))
        self.merchants = [self._entity("merchant", int(self.rng.integers(2, 6))) for _ in range(self.n_merchants)]
        self.exchanges = []
        for _ in range(self.n_exchanges):
            ips = [self._ip(NODES[self.rng.integers(len(NODES))]) for _ in range(2)]
            ex = self._entity("exchange", 1, ips=ips)
            ex["hot"] = ex["addrs"][0]
            self.exchanges.append(ex)

    # ------------------------------------------------------------ tx emit
    def _emit(self, t, inputs, outputs, ip, fee=None, kind="normal", script=None):
        """inputs/outputs: list of (address, amount_btc). Returns txid."""
        if fee is None or kind == "illicit":
            fee = round(float(self.rng.uniform(0.00002, 0.0002)), 8)
        tot_out = sum(a for _, a in outputs)
        tot_in = sum(a for _, a in inputs)
        if abs(tot_in - (tot_out + fee)) > 1e-7:          # rebalance inputs
            scale = (tot_out + fee) / max(tot_in, 1e-12)
            inputs = [(ad, round(a * scale, 8)) for ad, a in inputs]
            diff = round(tot_out + fee - sum(a for _, a in inputs), 8)
            ad, a = inputs[0]
            inputs[0] = (ad, round(a + diff, 8))
        txid = self._txid()
        self.txs.append({
            "txid": txid, "time": t, "ip": ip, "fee": fee, "kind": kind,
            "script_type": script or self.rng.choice(SCRIPT_TYPES, p=[0.55, 0.2, 0.15, 0.1]),
            "inputs": [(ad, round(a, 8)) for ad, a in inputs],
            "outputs": [(ad, round(a, 8)) for ad, a in outputs],
        })
        return txid

    def _amt(self, mu=-3.0, sigma=1.1):
        return round(float(np.exp(self.rng.normal(mu, sigma))), 8)

    def _pick_inputs(self, ent, amount):
        k = int(min(len(ent["addrs"]), self.rng.integers(1, 4)))
        idx = self.rng.choice(len(ent["addrs"]), k, replace=False)
        parts = self.rng.dirichlet(np.ones(k)) * amount
        return [(ent["addrs"][i], float(p)) for i, p in zip(idx, parts)]

    def _pay(self, sender, recip_addr, amount, t, kind="normal"):
        change = round(amount * float(self.rng.uniform(0.05, 0.9)), 8)
        if self.rng.random() < 0.65:
            ch_addr = self._addr(sender["akind"])
            sender["addrs"].append(ch_addr)            # one-time change address
        else:
            ch_addr = sender["addrs"][self.rng.integers(len(sender["addrs"]))]
        inputs = self._pick_inputs(sender, amount + change)
        ip = sender["ips"][self.rng.integers(len(sender["ips"]))]
        return self._emit(t, inputs, [(recip_addr, amount), (ch_addr, change)], ip, kind=kind)

    def _recv_addr(self, ent, new_p=0.4):
        if self.rng.random() < new_p or not ent["addrs"]:
            a = self._addr(ent["akind"])
            ent["addrs"].append(a)
            return a
        return ent["addrs"][self.rng.integers(len(ent["addrs"]))]

    # ------------------------------------------------------- normal flows
    def _normal_activity(self):
        r = self.rng
        for p in self.people:
            for _ in range(int(r.poisson(7))):
                t = self._time()
                u = r.random()
                if u < 0.45:
                    other = self.people[r.integers(len(self.people))]
                    self._pay(p, self._recv_addr(other), self._amt(-3.3), t)
                elif u < 0.75:
                    m = self.merchants[r.integers(len(self.merchants))]
                    self._pay(p, self._recv_addr(m, 0.7), self._amt(-3.8, 0.8), t)
                else:   # deposit to exchange (fresh deposit address)
                    ex = self.exchanges[r.integers(len(self.exchanges))]
                    dep = self._addr(ex["akind"])
                    ex.setdefault("deposits", []).append((dep, t))
                    self._pay(p, dep, self._amt(-2.6), t)
        # honest quick forwards (receive, then pay on within minutes)
        for _ in range(int(self.n_people * 0.35)):
            a, b = self.people[r.integers(len(self.people))], self.people[r.integers(len(self.people))]
            c = self.people[r.integers(len(self.people))]
            t = self._time()
            amt = self._amt(-2.8)
            fresh = self._addr(b["akind"])
            b["addrs"].append(fresh)
            self._pay(a, fresh, amt, t)
            t2 = t + timedelta(minutes=float(r.uniform(2, 25)))
            ch = self._addr(b["akind"])
            b["addrs"].append(ch)
            pay = round(amt * float(r.uniform(0.3, 0.97)), 8)
            self._emit(t2, [(fresh, amt)], [(self._recv_addr(c), pay), (ch, round(amt - pay - 0.0001, 8))],
                       b["ips"][0])
        # honest payroll / supplier batches (fan-out)
        for m in self.merchants[: max(3, self.n_merchants // 4)]:
            for w in range(3):
                n = int(r.integers(10, 22))
                outs = [(self._recv_addr(self.people[r.integers(len(self.people))], 0.5), self._amt(-3.0, 0.5))
                        for _ in range(n)]
                tot = sum(v for _, v in outs)
                ch = self._addr(m["akind"])
                m["addrs"].append(ch)
                outs.append((ch, round(tot * 0.5, 8)))
                self._emit(self._time(w * 7, w * 7 + 7), self._pick_inputs(m, tot * 1.5), outs, m["ips"][0])
        # honest donation / crowdfunding drives (fan-in)
        for _ in range(6):
            org = self._entity("charity", 1)
            box = org["addrs"][0]
            t = self._time(1, self.days - 3)
            for _ in range(int(r.integers(12, 35))):
                d = self.people[r.integers(len(self.people))]
                self._pay(d, box, self._amt(-4.0, 0.7), t + timedelta(hours=float(r.uniform(0, 40))))
            ex = self.exchanges[r.integers(len(self.exchanges))]
            dep = self._addr(ex["akind"])
            ex.setdefault("deposits", []).append((dep, t))
            self._emit(t + timedelta(days=float(r.uniform(2, 4))), [(box, 0.5)], [(dep, 0.4999)], org["ips"][0])
        # merchants occasionally move takings to an exchange
        for m in self.merchants:
            for _ in range(int(r.poisson(4))):
                ex = self.exchanges[r.integers(len(self.exchanges))]
                dep = self._addr(ex["akind"])
                ex.setdefault("deposits", []).append((dep, self._time()))
                self._pay(m, dep, self._amt(-2.0), self._time())
        # exchanges: sweep deposits to hot wallet (hours later), daily batch payouts
        for ex in self.exchanges:
            deps = ex.get("deposits", [])
            for i in range(0, len(deps), 6):
                chunk = deps[i:i + 6]
                t = max(d[1] for d in chunk) + timedelta(hours=float(r.uniform(2, 20)))
                amt = sum(self._amt(-2.6) for _ in chunk)
                ins = [(d[0], amt / len(chunk)) for d in chunk]
                self._emit(t, ins, [(ex["hot"], amt)], ex["ips"][0], kind="exchange_sweep")
            for d in range(self.days):
                n_out = int(r.integers(8, 28))
                outs = [(self._recv_addr(self.people[r.integers(len(self.people))], 0.5),
                         self._amt(-2.8)) for _ in range(n_out)]
                tot = sum(a for _, a in outs)
                outs.append((ex["hot"], round(tot * 3, 8)))
                t = self.t0 + timedelta(days=d, hours=float(r.uniform(9, 18)))
                self._emit(t, [(ex["hot"], tot * 4)], outs, ex["ips"][1], kind="exchange_batch")

    # ------------------------------------------------- mixer (CoinJoin-like)
    def _coinjoin(self, t, illicit_inputs=(), denom=0.1, n_honest=None):
        """Equal-value outputs. Honest privacy users take part too."""
        r = self.rng
        n_honest = n_honest if n_honest is not None else int(r.integers(4, 9))
        inputs, outputs = [], []
        for _ in range(n_honest):
            p = self.people[r.integers(len(self.people))]
            a = p["addrs"][r.integers(len(p["addrs"]))]
            inputs.append((a, denom + 0.003))
            fresh = self._addr(p["akind"])
            p["addrs"].append(fresh)
            outputs.append((fresh, denom))
        mixed_out = []
        for a in illicit_inputs:
            inputs.append((a, denom + 0.003))
            fresh = self._addr()
            outputs.append((fresh, denom))
            mixed_out.append(fresh)
        r.shuffle(outputs)
        ip = self._ip(HOSTING[r.integers(len(HOSTING))])
        self._emit(t, inputs, outputs, ip, fee=0.0006, kind="coinjoin")
        return mixed_out

    # --------------------------------------------------- laundering cases
    def _illicit_ip(self):
        if self.rng.random() < self.res_share:
            return self._ip(RESIDENTIAL[self.rng.integers(len(RESIDENTIAL))])
        return self._ip(HOSTING[self.rng.integers(len(HOSTING))])

    def _label(self, addr, typ, case):
        self.labels.setdefault(addr, (typ, case))

    def _victim_payments(self, ransom_addr, t, n, case):
        for _ in range(n):
            v = self.people[self.rng.integers(len(self.people))]
            self._pay(v, ransom_addr, self._amt(-0.6, 0.5), t - timedelta(hours=float(self.rng.uniform(1, 48))),
                      kind="victim")

    def _cashout(self, t):
        ex = self.exchanges[self.rng.integers(len(self.exchanges))]
        dep = self._addr(ex["akind"])
        ex.setdefault("deposits", []).append((dep, t))
        return dep

    def _case_peeling(self, case):
        r, t = self.rng, self._time(2, self.days - 3)
        start = self._addr(self.ck)
        self._label(start, "peeling_chain", case)
        self._victim_payments(start, t, int(r.integers(2, 5)), case)
        bal = float(r.uniform(3, 9))
        cur = start
        for hop in range(int(r.integers(7, 14))):
            peel = round(bal * float(r.uniform(0.04, 0.12)), 8)
            nxt = self._addr(self.ck)
            self._label(nxt, "peeling_chain", case)
            t += timedelta(minutes=float(r.uniform(2, self.hop_max)))
            self._emit(t, [(cur, bal)], [(self._cashout(t), peel), (nxt, bal - peel - 0.0001)],
                       self._illicit_ip(), fee=0.0001, kind="illicit")
            bal, cur = bal - peel - 0.0001, nxt
        return start

    def _case_fanout(self, case):
        r, t = self.rng, self._time(2, self.days - 3)
        hub = self._addr(self.ck)
        self._label(hub, "rapid_fan_out", case)
        self._victim_payments(hub, t, int(r.integers(3, 7)), case)
        amt = float(r.uniform(4, 12))
        n = int(r.integers(14, 30))
        outs = []
        for _ in range(n):
            a = self._addr(self.ck)
            self._label(a, "rapid_fan_out", case)
            outs.append((a, round(amt / n * float(r.uniform(0.8, 1.2)), 8)))
        t += timedelta(minutes=float(r.uniform(0.5, 3)))
        self._emit(t, [(hub, amt)], outs, self._illicit_ip(), kind="illicit")
        # each leaf cashes out quickly
        for a, v in outs:
            if r.random() < 0.7:
                t2 = t + timedelta(minutes=float(r.uniform(3, 60)))
                self._emit(t2, [(a, v)], [(self._cashout(t2), v - 0.00005)], self._illicit_ip(),
                           fee=0.00005, kind="illicit")
        return hub

    def _case_fanin(self, case):
        r, t = self.rng, self._time(2, self.days - 3)
        collector = self._addr(self.ck)
        self._label(collector, "fan_in_collection", case)
        n = int(r.integers(14, 32))
        total = 0.0
        for _ in range(n):
            mule = self._addr(self.ck)
            self._label(mule, "fan_in_collection", case)
            v = self.people[r.integers(len(self.people))]           # victim -> mule
            amt = self._amt(-1.6, 0.6)
            tm = t - timedelta(hours=float(r.uniform(0.5, 30)))
            self._pay(v, mule, amt, tm, kind="victim")
            tm2 = tm + timedelta(minutes=float(r.uniform(5, 90)))
            self._emit(tm2, [(mule, amt)], [(collector, amt - 0.00005)], self._illicit_ip(), fee=0.00005,
                       kind="illicit")
            total += amt
        t += timedelta(hours=float(r.uniform(0.2, 2)))
        nxt = self._addr(self.ck)
        self._label(nxt, "fan_in_collection", case)
        self._emit(t, [(collector, total * 0.99)], [(nxt, total * 0.99 - 0.0002)], self._illicit_ip(),
                   fee=0.0002, kind="illicit")
        t += timedelta(minutes=float(r.uniform(5, 40)))
        self._emit(t, [(nxt, total * 0.99 - 0.0002)], [(self._cashout(t), total * 0.99 - 0.0004)],
                   self._illicit_ip(), fee=0.0002, kind="illicit")
        return collector

    def _case_mixing(self, case):
        r, t = self.rng, self._time(2, self.days - 3)
        src = self._addr(self.ck)
        self._label(src, "mixing_cashout", case)
        self._victim_payments(src, t, int(r.integers(2, 5)), case)
        k = int(r.integers(3, 6))
        pre = []
        outs = []
        for _ in range(k):
            a = self._addr(self.ck)
            self._label(a, "mixing_cashout", case)
            pre.append(a)
            outs.append((a, 0.103))
        t += timedelta(minutes=float(r.uniform(1, 10)))
        self._emit(t, [(src, 0.103 * k)], outs, self._illicit_ip(), kind="illicit")
        t += timedelta(minutes=float(r.uniform(5, 30)))
        mixed = self._coinjoin(t, pre)
        for m in mixed:
            self._label(m, "mixing_cashout", case)
            t2 = t + timedelta(minutes=float(r.uniform(10, 120)))
            self._emit(t2, [(m, 0.1)], [(self._cashout(t2), 0.0999)], self._illicit_ip(), fee=0.0001,
                       kind="illicit")
        return src

    def _case_splitting(self, case):
        r, t = self.rng, self._time(2, self.days - 3)
        root = self._addr(self.ck)
        self._label(root, "repeated_splitting", case)
        self._victim_payments(root, t, int(r.integers(2, 5)), case)
        frontier = [(root, float(r.uniform(4, 10)))]
        for depth in range(3):
            nxt = []
            for a, v in frontier:
                t += timedelta(minutes=float(r.uniform(2, 15)))
                parts = int(r.integers(3, 5))
                outs = []
                for _ in range(parts):
                    b = self._addr(self.ck)
                    self._label(b, "repeated_splitting", case)
                    outs.append((b, round(v / parts - 0.00005, 8)))
                self._emit(t, [(a, v)], outs, self._illicit_ip(), fee=0.0001, kind="illicit")
                nxt.extend(outs)
            frontier = nxt[:12]
        for a, v in frontier:
            t2 = t + timedelta(minutes=float(r.uniform(5, 60)))
            self._emit(t2, [(a, v)], [(self._cashout(t2), v - 0.00005)], self._illicit_ip(), fee=0.00005,
                       kind="illicit")
        return root

    def _privacy_users(self):
        # honest CoinJoins (mixing alone is not a crime)
        for _ in range(12):
            self._coinjoin(self._time(), n_honest=int(self.rng.integers(5, 10)))

    # ---------------------------------------------------------------- run
    def build(self):
        self._build_population()
        self._normal_activity()
        self._privacy_users()
        makers = [self._case_peeling, self._case_fanout, self._case_fanin, self._case_mixing,
                  self._case_splitting]
        for c in range(self.n_cases):
            case = f"CASE-{c + 1:02d}"
            self.ck = self._kind()
            start = makers[c % len(makers)](case)
            if self.rng.random() < 0.5:     # half the cases have a publicly reported address
                self.seeds.append({"address": start, "source": "public ransomware / fraud report",
                                   "case_id": case})
        return self

    # ------------------------------------------------------------ outputs
    def observations(self):
        """One row per network observation (the SIH dataset shape)."""
        r = self.rng
        rows = []
        for tx in sorted(self.txs, key=lambda x: x["time"]):
            first_seen = tx["time"]
            block_time = first_seen + timedelta(minutes=float(r.uniform(2, 20)))
            ins = tx["inputs"]
            outs = tx["outputs"]
            common = {
                "txid": tx["txid"],
                "input_addresses": json.dumps([a for a, _ in ins]),
                "input_amounts": json.dumps([v for _, v in ins]),
                "output_addresses": json.dumps([a for a, _ in outs]),
                "output_amounts": json.dumps([v for _, v in outs]),
                "fee": tx["fee"], "script_type": tx["script_type"],
                "block_time": block_time.isoformat().replace("+00:00", "Z"),
            }
            # origin broadcasts to several peers almost immediately
            # (~12% of the time our sensors miss the originating node entirely)
            n_peers = 0 if r.random() < 0.12 else int(r.integers(2, 7))
            for _ in range(n_peers):
                ts = first_seen + timedelta(seconds=float(r.uniform(0, 1.5)))
                rows.append({**common, "timestamp": ts.isoformat().replace("+00:00", "Z"),
                             "src_ip": tx["ip"], "src_port": int(r.integers(49152, 65535)),
                             "dst_ip": self.relay_ips[r.integers(len(self.relay_ips))], "dst_port": 8333})
            # relays forward it later
            for _ in range(int(r.integers(1, 4))):
                relay = self.relay_ips[r.integers(len(self.relay_ips))]
                for _ in range(int(r.integers(1, 3))):
                    ts = first_seen + timedelta(seconds=float(r.uniform(3, 40)))
                    rows.append({**common, "timestamp": ts.isoformat().replace("+00:00", "Z"),
                                 "src_ip": relay, "src_port": 8333,
                                 "dst_ip": self.relay_ips[r.integers(len(self.relay_ips))], "dst_port": 8333})
        df = pd.DataFrame(rows)
        # a little real-world messiness: duplicates and a few missing fees
        dup = df.sample(frac=0.01, random_state=1)
        df = pd.concat([df, dup], ignore_index=True)
        miss = df.sample(frac=0.005, random_state=2).index
        df.loc[miss, "fee"] = np.nan
        cols = ["timestamp", "src_ip", "src_port", "dst_ip", "dst_port", "txid", "input_addresses",
                "output_addresses", "input_amounts", "output_amounts", "fee", "script_type", "block_time"]
        return df[cols].sort_values("timestamp").reset_index(drop=True)

    def save(self, folder, name):
        os.makedirs(folder, exist_ok=True)
        obs = self.observations()
        obs.to_csv(os.path.join(folder, f"{name}.csv"), index=False)
        gt = os.path.join(folder, "ground_truth")
        os.makedirs(gt, exist_ok=True)
        pd.DataFrame([{"address": a, "label": 1, "typology": t, "case_id": c}
                      for a, (t, c) in self.labels.items()]).to_csv(
            os.path.join(gt, f"{name}_labels.csv"), index=False)
        pd.DataFrame(self.seeds).to_csv(os.path.join(folder, f"{name}_seeds.csv"), index=False)
        return obs


def generate_all(data_dir):
    write_geo_db(os.path.join(data_dir, "geo_db.csv"))
    tr = Generator(seed=7, n_cases=40).build()
    tr.save(data_dir, "training_dataset")
    case = Generator(seed=42, n_cases=30).build()
    obs = case.save(data_dir, "case_dataset")
    # same case data in JSON and XML (multi-format ingestion demo)
    small = obs.head(2000)
    small.to_json(os.path.join(data_dir, "case_sample.json"), orient="records", indent=1)
    small.to_xml(os.path.join(data_dir, "case_sample.xml"), index=False, root_name="observations",
                 row_name="observation", parser="etree")
    return obs


if __name__ == "__main__":
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    o = generate_all(os.path.join(here, "data"))
    print("observations:", len(o), "transactions:", o.txid.nunique())
