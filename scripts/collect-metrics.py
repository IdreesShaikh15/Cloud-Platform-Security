#!/usr/bin/env python3
"""Fetch per-incident metrics from the dashboard and write results/metrics-<ts>.csv.

    kubectl -n resilience port-forward svc/dashboard 8090:8090 &
    python3 scripts/collect-metrics.py [--url http://localhost:8090]

For each incident the table reports the *earliest* time any agent observed each
event (the executor's timestamp for isolation), which is what the system as a
whole achieved.
"""
import argparse
import csv
import json
import os
import time
import urllib.request

KEYS = ["ttd_s", "tti_s", "ttr_s", "ttv_s", "ttf_s", "trust_recovery_s", "time_to_flag_s",
        "inv_start_s", "inv_end_s"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8090")
    a = ap.parse_args()
    with urllib.request.urlopen(f"{a.url.rstrip('/')}/api/metrics", timeout=5) as r:
        rows = json.loads(r.read())
    by = {}
    for row in rows:
        by.setdefault(row["incident"], []).append(row)
    out = []
    for inc, rs in by.items():
        agg = {"incident": inc, "scenario": rs[0]["scenario"], "target": rs[0]["target"],
               "attacker": rs[0].get("attacker"), "mode": rs[0]["mode"],
               "reporters": len(rs), "false_isolation": any(r["false_isolation"] for r in rs)}
        for k in KEYS:
            vals = [r[k] for r in rs if r.get(k) is not None]
            agg[k] = min(vals) if vals else None
        agg["investigations"] = max((r.get("investigations") or 0 for r in rs), default=0)
        agg["investigation_outcome"] = next((r["investigation_outcome"] for r in rs
                                             if r.get("investigation_outcome")), None)
        agg["human_review"] = any(r.get("human_review") for r in rs)
        av = [r["availability"] for r in rs if r.get("availability") is not None]
        agg["availability"] = av[0] if av else None
        out.append(agg)
    os.makedirs("results", exist_ok=True)
    path = f"results/metrics-{int(time.time())}.csv"
    cols = ["incident", "scenario", "mode", "target", "attacker", "reporters", *KEYS,
            "investigations", "investigation_outcome", "human_review", "false_isolation", "availability"]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(out)
    print(f"{'scenario':18} {'mode':12} {'tgt':3} " + " ".join(f"{k[:-2]:>8}" for k in KEYS) + "  false_iso  avail")
    for r in out:
        print(f"{r['scenario']:18} {r['mode']:12} {r['target']:3} "
              + " ".join(f"{r[k]:8.1f}" if r[k] is not None else f"{'-':>8}" for k in KEYS)
              + f"  {str(r['false_isolation']):9}  {r['availability']}")
    print(f"\nwritten {path}")


if __name__ == "__main__":
    main()
