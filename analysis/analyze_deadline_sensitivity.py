#!/usr/bin/env python3
"""
Sensitivity of workflow goodput to the deadline multiplier k.

Re-evaluates the deadline condition makespan <= k * depth * SLO on saved
per-workflow logs (workflows_<scenario>_<topology>_<mode>_<algo>_<load>_seed<i>.csv)
for several k, without re-running any simulation.

Output: <outdir>/deadline_sensitivity.csv, one row per (scenario, topology,
load, k) with goodput mean and CI95 over seeds.

Usage:
    python3 -m MERAF.analysis.analyze_deadline_sensitivity [--outdir MERAF/results/outputs_wf_by_stage]
        [--scenarios rete] [--topologies linear fan_out_in montage_like]
        [--algorithm meraf-workflow-admission] [--ks 0.75 1.0 1.5 2.0]
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import re

from ..core.stats_utils import mean_std_ci95

SLO_MS = 2000.0
HORIZON_S = 30.0  # must match run_workflow_experiments.py


def goodput_for_k(records_file: str, k: float) -> float:
    """Goodput (wf/s) of one run with deadline k * depth * SLO."""
    n = 0
    with open(records_file, newline="") as f:
        for r in csv.DictReader(f):
            if r["completed"] == "True" and \
               float(r["makespan_ms"]) <= k * int(r["depth"]) * SLO_MS:
                n += 1
    return n / HORIZON_S


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outdir", default="MERAF/results/outputs_wf_by_stage")
    ap.add_argument("--profile-mode", default="by_stage")
    ap.add_argument("--scenarios", nargs="+", default=["rete"])
    ap.add_argument("--topologies", nargs="+",
                     default=["linear", "fan_out_in", "montage_like"])
    ap.add_argument("--algorithm", default="meraf-workflow-admission")
    ap.add_argument("--ks", type=float, nargs="+", default=[0.75, 1.0, 1.5, 2.0])
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    args = ap.parse_args()

    out_rows = []
    for scen in args.scenarios:
        for topo in args.topologies:
            pattern = os.path.join(
                args.outdir,
                f"workflows_{scen}_{topo}_{args.profile_mode}_{args.algorithm}_*_seed1.csv")
            loads = sorted(
                float(re.search(r"_(\d+\.\d+)_seed1\.csv$", p).group(1))
                for p in glob.glob(pattern))
            for load in loads:
                for k in args.ks:
                    vals = []
                    for seed in args.seeds:
                        f = os.path.join(
                            args.outdir,
                            f"workflows_{scen}_{topo}_{args.profile_mode}_"
                            f"{args.algorithm}_{load:.3f}_seed{seed}.csv")
                        if os.path.exists(f):
                            vals.append(goodput_for_k(f, k))
                    if not vals:
                        continue
                    s = mean_std_ci95(vals)
                    out_rows.append(dict(scenario=scen, topology=topo, load=load,
                                          k=k, n_seeds=s.n,
                                          goodput_mean=round(s.mean, 3),
                                          goodput_ci95=round(s.ci95, 3)))

    out_file = os.path.join(args.outdir, "deadline_sensitivity.csv")
    with open(out_file, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print(f"[saved] {out_file} ({len(out_rows)} rows)")

    # Plateau summary: goodput range per (topology, k) over loads >= 0.02.
    for topo in args.topologies:
        for k in args.ks:
            vals = [r["goodput_mean"] for r in out_rows
                    if r["topology"] == topo and r["k"] == k and r["load"] >= 0.02]
            if vals:
                print(f"  {topo:14} k={k:<5}: goodput {min(vals):6.2f}--{max(vals):6.2f} wf/s "
                      f"over loads >= 0.02")


if __name__ == "__main__":
    main()
