#!/usr/bin/env python3
"""
TOST equivalence test of latency across security profiles.

Reads `profile_breakdown_<scenario>.csv` (from run_experiments.py, one row per
(algorithm, load, seed, profile)) and, for every (algorithm, load) cell, runs a
Two One-Sided Tests procedure on each pair of profiles over the per-seed values
of --metric. The equivalence margin (--margin-ms, default 100 ms = 5% of a
2000 ms SLO) is a practical-relevance judgement, not a data-derived quantity.
Writes `equivalence_<scenario>.csv` and, unless --no-latex, the matching table.

Usage:
    python3 -m MERAF.experiments.run_experiments                     # produces the input CSVs
    python3 -m MERAF.analysis.equivalence_test --outdir MERAF/results/outputs
    python3 -m MERAF.analysis.equivalence_test --outdir MERAF/results/outputs --margin-ms 50 --metric latency_p99_ms
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import re
from collections import defaultdict
from typing import Dict, List, Tuple

from ..core.stats_utils import tost_equivalence
from ..reporting.make_latex_tables import generate_equivalence_table

PROFILE_BREAKDOWN_RE = re.compile(r"^profile_breakdown_(?P<scenario>[a-zA-Z0-9]+)\.csv$")
# The "algorithm" column holds run_experiments.py's composite key "<algo>@<load>@seed<N>".
KEY_RE = re.compile(r"^(?P<algo>.+)@(?P<load>[0-9.]+)@seed(?P<seed>\d+)$")


def load_profile_breakdown(path: str, metric: str) -> Dict[Tuple[str, float], Dict[str, List[float]]]:
    """Return {(algo, load): {profile: [metric value per seed]}}."""
    out: Dict[Tuple[str, float], Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            m = KEY_RE.match(row["algorithm"])
            if not m:
                continue
            algo = m.group("algo")
            load = float(m.group("load"))
            profile = row["profile"]
            out[(algo, load)][profile].append(float(row[metric]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="MERAF/results/outputs",
                     help="Directory holding the profile_breakdown_<scenario>.csv "
                          "files written by run_experiments.py")
    ap.add_argument("--result-dir", default=None,
                     help="Where to save equivalence_<scenario>.csv (default: --outdir)")
    ap.add_argument("--margin-ms", type=float, default=100.0,
                     help="Practical equivalence margin, same units as --metric "
                          "(default 100ms = 5%% of a 2000ms SLO)")
    ap.add_argument("--metric", default="latency_p95_ms",
                     choices=["latency_mean_ms", "latency_p50_ms",
                              "latency_p95_ms", "latency_p99_ms"])
    ap.add_argument("--no-latex", action="store_true",
                     help="Do not generate equivalence_<scenario>_table.tex automatically "
                          "(by default it is written right after the CSV)")
    args = ap.parse_args()
    result_dir = args.result_dir or args.outdir
    os.makedirs(result_dir, exist_ok=True)

    files = sorted(glob.glob(os.path.join(args.outdir, "profile_breakdown_*.csv")))
    if not files:
        print(f"ERROR: no profile_breakdown_*.csv found in {args.outdir}. "
              f"Run run_experiments.py first.")
        return 1

    any_rows = False
    for path in files:
        m = PROFILE_BREAKDOWN_RE.match(os.path.basename(path))
        scenario = m.group("scenario") if m else os.path.basename(path)
        by_cell = load_profile_breakdown(path, args.metric)

        out_rows = []
        for (algo, load), by_profile in sorted(by_cell.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            profiles = sorted(by_profile.keys())
            for i in range(len(profiles)):
                for j in range(i + 1, len(profiles)):
                    pa, pb = profiles[i], profiles[j]
                    res = tost_equivalence(by_profile[pa], by_profile[pb], margin=args.margin_ms)
                    out_rows.append(dict(
                        scenario=scenario, algorithm=algo, load=load,
                        metric=args.metric, profile_a=pa, profile_b=pb,
                        n_a=len(by_profile[pa]), n_b=len(by_profile[pb]),
                        mean_a=f"{res.mean_a:.3f}", mean_b=f"{res.mean_b:.3f}",
                        diff=f"{res.diff:.3f}" if res.diff == res.diff else "nan",
                        margin_ms=args.margin_ms,
                        t_lower=f"{res.t_lower:.3f}" if res.t_lower == res.t_lower else "nan",
                        t_upper=f"{res.t_upper:.3f}" if res.t_upper == res.t_upper else "nan",
                        t_crit=f"{res.t_crit:.3f}" if res.t_crit == res.t_crit else "nan",
                        df=f"{res.df:.2f}" if res.df == res.df else "nan",
                        equivalent=res.equivalent,
                        note=res.reason or "",
                    ))

        if not out_rows:
            print(f"[{scenario}] no cell with >=2 comparable profiles, skipping")
            continue

        any_rows = True
        out_path = os.path.join(result_dir, f"equivalence_{scenario}.csv")
        with open(out_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
            w.writeheader()
            w.writerows(out_rows)

        n_equiv = sum(1 for r in out_rows if r["equivalent"])
        print(f"[{scenario}] {len(out_rows)} profile-pair comparisons, "
              f"{n_equiv} declared equivalent within {args.margin_ms}ms "
              f"on '{args.metric}' -> {out_path}")

        if not args.no_latex:
            generate_equivalence_table(out_path)

        for r in out_rows:
            if not r["equivalent"]:
                print(f"    NOT equivalent: {r['algorithm']} @ {r['load']} "
                      f"{r['profile_a']} vs {r['profile_b']} "
                      f"(diff={r['diff']}ms, {r['note']})")

    if not any_rows:
        print("No comparison produced (at least 2 security profiles per "
              "algorithm/load cell are required).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
