#!/usr/bin/env python3
"""
Paired t-test of workflow completion rate: by_stage vs random profile assignment.

For each (scenario, topology, load) cell both runs share the same seeds, so the
per-seed difference completion(by_stage) - completion(random) is paired and
t = mean(diff) / (sd(diff) / sqrt(n)).

Output: <bystage>/profile_pairing_ttest.csv, one row per cell with mean diff
(percentage points), sd, t and the |t| > 2 flag, plus a console summary
comparing the number of |t| > 2 cells with the chance expectation.

Usage:
    python3 -m MERAF.analysis.analyze_profile_pairing
        [--bystage MERAF/results/outputs_wf_by_stage] [--random MERAF/results/outputs_wf_random]
        [--algorithm meraf-workflow-admission]
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import statistics as st


def load_cells(outdir: str, mode: str, scen: str, topo: str, algo: str):
    """Return {(load, seed): completion_rate_pct} for one algorithm from the cell's raw_perseed CSV."""
    f = os.path.join(outdir, f"workflow_summary_{scen}_{topo}_{mode}_raw_perseed.csv")
    out = {}
    if not os.path.exists(f):
        return out
    with open(f, newline="") as fh:
        for r in csv.DictReader(fh):
            if r["algorithm"] == algo:
                out[(float(r["load"]), int(r["seed"]))] = float(r["completion_rate_pct"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bystage", default="MERAF/results/outputs_wf_by_stage")
    ap.add_argument("--random", default="MERAF/results/outputs_wf_random")
    ap.add_argument("--scenarios", nargs="+", default=["rete", "locale"])
    ap.add_argument("--topologies", nargs="+",
                     default=["linear", "fan_out_in", "montage_like"])
    ap.add_argument("--algorithm", default="meraf-workflow-admission")
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    args = ap.parse_args()

    rows = []
    for scen in args.scenarios:
        for topo in args.topologies:
            A = load_cells(args.bystage, "by_stage", scen, topo, args.algorithm)
            B = load_cells(args.random, "random", scen, topo, args.algorithm)
            loads = sorted({l for (l, _) in A} & {l for (l, _) in B})
            for load in loads:
                diffs = [A[(load, s)] - B[(load, s)] for s in args.seeds
                         if (load, s) in A and (load, s) in B]
                if len(diffs) < 2:
                    continue
                m = st.mean(diffs)
                sd = st.stdev(diffs)
                if sd > 0:
                    t = m / (sd / math.sqrt(len(diffs)))
                elif m == 0:
                    t = 0.0   # identical cells (unsaturated regime): no evidence
                else:
                    t = math.inf  # constant non-zero difference on every seed
                rows.append(dict(scenario=scen, topology=topo, load=load,
                                  n=len(diffs), diff_pp_mean=round(m, 4),
                                  diff_pp_sd=round(sd, 4),
                                  t=round(t, 3) if math.isfinite(t) else "inf",
                                  significant_t2=abs(t) > 2 if math.isfinite(t) else True))

    out_file = os.path.join(args.bystage, "profile_pairing_ttest.csv")
    with open(out_file, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"[saved] {out_file} ({len(rows)} paired cells)")

    sig = [r for r in rows if r["significant_t2"]]
    print(f"total cells: {len(rows)}; with |t| > 2: {len(sig)} "
          f"(expected by chance: ~{0.116 * len(rows):.1f}, P(|t|>2)~0.116 at 4 df)")
    for r in sorted(sig, key=lambda r: -abs(float(r['t']))):
        print(f"  {r['scenario']:7} {r['topology']:14} load={r['load']:<5} "
              f"diff={r['diff_pp_mean']:+.3f}pp  t={r['t']}")


if __name__ == "__main__":
    main()
