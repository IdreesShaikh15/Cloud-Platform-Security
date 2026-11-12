#!/usr/bin/env python3
"""Timing profiles for the live cluster (config/timing-profiles.json, docs/DEMO-PROFILE.md).

Used by scripts/demo-profile.sh; kept in Python so the rules are testable:

    python3 scripts/timing_profile.py show <profile>                    # what the profile changes
    python3 scripts/timing_profile.py apply <profile> < config.json     # config.json with ONLY those keys set, on stdout
    python3 scripts/timing_profile.py detect < config.json              # default | presentation | custom
    python3 scripts/timing_profile.py grace <profile>                   # the pod shutdown wait in seconds, or 'default'

Only the keys named in the profile file are changed; everything else in the configuration (quorum, certificates, thresholds, the other
timers) is passed through untouched. Nothing here reads or changes agent code.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILES = os.path.join(os.path.dirname(HERE), "config", "timing-profiles.json")
GROUPS = ("timers", "trust")


def load(path: str = PROFILES) -> dict:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def names(profiles: dict = None) -> list:
    return sorted((profiles or load()))


def touched_keys(profiles: dict = None) -> dict:
    """{group: set(keys)}: every key ANY profile may change (so switching back resets them all)."""
    out = {g: set() for g in GROUPS}
    for p in (profiles or load()).values():
        for g in GROUPS:
            out[g] |= set(p.get(g) or {})
    return out


def apply(config: dict, name: str, profiles: dict = None) -> dict:
    profiles = profiles or load()
    if name not in profiles:
        raise KeyError(f"unknown profile {name!r}; choose from {', '.join(names(profiles))}")
    new = json.loads(json.dumps(config))
    for g in GROUPS:
        want = profiles[name].get(g) or {}
        if want:
            new.setdefault(g, {}).update(want)
    return new


def detect(config: dict, profiles: dict = None) -> str:
    profiles = profiles or load()
    keys = touched_keys(profiles)
    for name, p in sorted(profiles.items()):
        if all((config.get(g) or {}).get(k) == v for g in GROUPS for k, v in (p.get(g) or {}).items()):
            return name
    return "custom"


def grace(name: str, profiles: dict = None):
    return (profiles or load())[name].get("healthcare_termination_grace_s")


def main(argv) -> int:
    if len(argv) < 2 or argv[1] not in ("show", "apply", "detect", "grace", "names"):
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[1]
    try:
        if cmd == "names":
            print("\n".join(names()))
        elif cmd == "show":
            p = load()[argv[2]]
            print(json.dumps(p, indent=2))
        elif cmd == "apply":
            print(json.dumps(apply(json.load(sys.stdin), argv[2]), indent=2))
        elif cmd == "detect":
            print(detect(json.load(sys.stdin)))
        elif cmd == "grace":
            g = grace(argv[2])
            print("default" if g is None else int(g))
    except (KeyError, IndexError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
