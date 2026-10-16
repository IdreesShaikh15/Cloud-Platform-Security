#!/usr/bin/env python3
"""Turn a Synthea CSV export into the small dataset the demo services serve: apps/src/data/synthea-sample.json.

    # 1. generate patients with Synthea (https://github.com/synthetichealth/synthea, Apache-2.0); this is what produced the committed file:
    java -jar synthea-with-dependencies.jar -p 200 -s 20261016 -cs 20261016 -r 20261016 \
         --exporter.csv.export=true --exporter.fhir.export=false --exporter.baseDirectory=out Massachusetts
    # 2. convert the CSV export:
    python3 scripts/gen-synthea-dataset.py out/csv --seed 20261016

ALL DATA IS SYNTHETIC. Synthea writes invented people; no real patient is involved at any step. This converter additionally keeps only
what the services need and drops everything that looks like an identifier even though it is invented: social security numbers, driver's
licence and passport numbers, street addresses, coordinates, income and costs. Only living patients are kept.

Per patient it keeps: a medical record number (MRN) derived from Synthea's id, name, birth date, gender, city and state; the ACTIVE
conditions; the most recent laboratory results (one per kind of test, newest first); the ACTIVE medications.
The output is deterministic (sorted, fixed field order), so the same Synthea export always gives the same file.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "apps", "src", "data", "synthea-sample.json")
MAX_CONDITIONS, MAX_LABS, MAX_MEDS = 8, 12, 8
KEEP_CONDITION_KINDS = ("(disorder)",)                          # clinical disorders only: "(finding)" rows are social history, "(situation)" rows are administrative


def rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def short(text: str, n: int = 90) -> str:
    text = " ".join(text.replace("  ", " ").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def build(src: str, seed: int, version: str, population: int, state: str) -> dict:
    patients = {}
    for p in rows(os.path.join(src, "patients.csv")):
        if p["DEATHDATE"]:
            continue
        mrn = "SYN-" + p["Id"].replace("-", "")[:8].upper()
        patients[p["Id"]] = {"mrn": mrn, "name": f'{p["FIRST"]} {p["LAST"]}', "dob": p["BIRTHDATE"], "gender": p["GENDER"],
                             "city": p["CITY"], "state": p["STATE"], "conditions": [], "labs": [], "medications": []}
    if len({v["mrn"] for v in patients.values()}) != len(patients):
        raise SystemExit("MRN collision: pick another seed or lengthen the MRN")

    for c in rows(os.path.join(src, "conditions.csv")):
        p = patients.get(c["PATIENT"])
        if p and not c["STOP"] and c["DESCRIPTION"].endswith(KEEP_CONDITION_KINDS) and len(p["conditions"]) < MAX_CONDITIONS:
            p["conditions"].append({"since": c["START"], "code": c["CODE"], "description": short(c["DESCRIPTION"])})

    latest = defaultdict(dict)                                       # patient -> description -> newest lab row
    for o in rows(os.path.join(src, "observations.csv")):
        if o["CATEGORY"] != "laboratory" or o["PATIENT"] not in patients:
            continue
        cur = latest[o["PATIENT"]].get(o["DESCRIPTION"])
        if cur is None or o["DATE"] > cur["DATE"]:
            latest[o["PATIENT"]][o["DESCRIPTION"]] = o
    for pid, byname in latest.items():
        for o in sorted(byname.values(), key=lambda r: (r["DATE"], r["DESCRIPTION"]), reverse=True)[:MAX_LABS]:
            patients[pid]["labs"].append({"date": o["DATE"][:10], "loinc": o["CODE"], "test": short(o["DESCRIPTION"]),
                                          "value": o["VALUE"], "units": o["UNITS"]})

    for m in rows(os.path.join(src, "medications.csv")):
        p = patients.get(m["PATIENT"])
        if p and not m["STOP"] and len(p["medications"]) < MAX_MEDS:
            p["medications"].append({"since": m["START"][:10], "rxnorm": m["CODE"], "description": short(m["DESCRIPTION"]),
                                     "reason": short(m["REASONDESCRIPTION"]) if m["REASONDESCRIPTION"] else ""})

    out = sorted(patients.values(), key=lambda v: v["mrn"])
    return {"meta": {"source": "Synthea", "synthetic": True,
                     "generator": "Synthea (https://github.com/synthetichealth/synthea), Apache-2.0",
                     "generator_build": version, "seed": seed, "population_requested": population, "state_model": state,
                     "patients_in_file": len(out), "note": "Invented patients produced by the Synthea synthetic patient generator. "
                                                           "No real person's data is used anywhere in this project."},
            "patients": out}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_dir", help="the csv/ directory of a Synthea export")
    ap.add_argument("--seed", type=int, default=20261016)
    ap.add_argument("--build", default="d9d07a6", help="Synthea build id (version.txt inside the jar)")
    ap.add_argument("--population", type=int, default=200)
    ap.add_argument("--state", default="Massachusetts")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    data = build(a.csv_dir, a.seed, a.build, a.population, a.state)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
    n = data["meta"]["patients_in_file"]
    tot = {k: sum(len(p[k]) for p in data["patients"]) for k in ("conditions", "labs", "medications")}
    print(f"wrote {os.path.relpath(a.out, ROOT)}: {n} patients, {tot['conditions']} conditions, {tot['labs']} lab results, "
          f"{tot['medications']} medications, {os.path.getsize(a.out) / 1024:.0f} KiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
