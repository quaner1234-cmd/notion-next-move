#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mutation test for the workflow optimizations ported into notion-next-move.

Each defect below is one thing the new tests are supposed to catch. The
script applies the defect, runs the offline suite, expects RED, restores the
file and verifies the restore byte-for-byte.

Run:  python tests/mutation_test.py
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITE = ROOT / "tests" / "run_tests.py"

MUTATIONS = [
    ("dedupe: revert to max()-coverage (short title swallows long candidate)",
     "nextmove/dedupe.py",
     "    return len(a & b) / max(len(a), len(b))",
     "    return max(len(a & b) / len(a), len(a & b) / len(b))"),
    ("dedupe: drop the near tier (similar candidates are created as new)",
     "nextmove/dedupe.py",
     '    if best_score >= limits["hold_ratio"]:',
     "    if False:"),
    ("dedupe: ignore configured thresholds (defaults always win)",
     "nextmove/dedupe.py",
     "            out[key] = float(value)",
     "            out[key] = DEFAULT_THRESHOLDS[key]"),
    ("tasks: skip the hold path (a near duplicate is written anyway)",
     "nextmove/tasks.py",
     '    if verdict["kind"] == "near":',
     '    if False:'),
    ("protection: never see a human edit (rows get overwritten)",
     "nextmove/protection.py",
     "    return page_dt > agent_dt",
     "    return False"),
    ("loop: remove the protection gate before completing a task",
     "nextmove/loop.py",
     '    if guard.get("protected"):',
     '    if False:'),
    ("cli: read an unrecognised packet as an empty one",
     "nextmove/cli.py",
     "    problem = plan_module.packet_shape_problem(packet)",
     "    problem = None"),
]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_source(path):
    """Byte-exact read (no newline translation) so restore is exact."""
    return Path(path).read_text(encoding="utf-8", newline="")


def write_source(path, text):
    Path(path).write_text(text, encoding="utf-8", newline="")


def run_suite():
    for stale in ROOT.rglob("__pycache__"):
        subprocess.run(["rm", "-rf", str(stale)], check=False)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run([sys.executable, str(SUITE)], cwd=str(ROOT),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return proc.returncode, tail


def main():
    print("baseline", run_suite())
    rows = []
    for label, relative, old, new in MUTATIONS:
        path = ROOT / relative
        original = read_source(path)
        before = digest(path)
        if old not in original:
            rows.append((label, "ANCHOR-MISSING", "-", "-"))
            print(f"{label}: ANCHOR MISSING")
            continue
        write_source(path, original.replace(old, new, 1))
        landed = digest(path) != before
        code, tail = run_suite()
        caught = code != 0
        write_source(path, original)
        restored = digest(path) == before
        rows.append((label, "CAUGHT" if caught else "NOT-CAUGHT",
                     "restored" if restored else "RESTORE-FAILED",
                     "mutated" if landed else "MUTATION-NOT-APPLIED"))
        print(f"{label}: {'CAUGHT' if caught else 'NOT-CAUGHT'} | {tail} | "
              f"{'restored' if restored else 'RESTORE-FAILED'} | "
              f"{'mutated' if landed else 'NOT-APPLIED'}")
    print()
    for label, verdict, restore, landed in rows:
        print(f"{verdict:<14} {restore:<16} {landed:<22} {label}")
    ok = all(r[1] == "CAUGHT" and r[2] == "restored" and r[3] == "mutated"
             for r in rows)
    print("\nRESULT:", "ALL CAUGHT" if ok else "SOME NOT CAUGHT")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
