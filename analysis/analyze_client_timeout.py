#!/usr/bin/env python3
"""
Client-side view of saved per-request results under a finite client timeout X.

Reinterprets existing `completed_*.csv` logs (no new simulation): a request is
a success if it completed within X, otherwise a failure (rejected, still in
flight at horizon end, or completed after X). Every failure is charged X in the
client-view latency distribution, so percentiles are censored at X and flagged
as saturated when the failed fraction exceeds (100-p)%.

Output: <outdir>/client_timeout_view.csv, one row per (algorithm, load, X)
with success%, goodput@X and censored p95/p99 (mean and CI95 over seeds).

Usage:
    python3 -m MERAF.analysis.analyze_client_timeout [--loads 0.12 0.16]
        [--thresholds-ms 5000 10000 20000] [--scenario rete]
"""
from __future__ import annotations

import argparse
import csv
import os

from ..core.stats_utils import mean_std_ci95

HORIZON_S = 30.0

# Internal file-name identifiers; the paper labels security-constraint-* as sec-*.
ALGOS = ["meraf", "security-constraint-loadaware", "security-constraint-satisfaction",
         "round-robin", "least-connections", "power-of-2", "ewma-lrt"]


def pctile(sorted_vals, p):
    """Percentile with linear interpolation between order statistics (p in 0..100)."""
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    k = (p / 100.0) * (n - 1)
    lo = int(k)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (k - lo) * (sorted_vals[hi] - sorted_vals[lo])


def load_counts(outdir, scenario):
    """Return {(algo, load, seed): (total_requests, completed, rejected)} from the raw_perseed CSV."""
    out = {}
    with open(os.path.join(outdir, f"summary_{scenario}_raw_perseed.csv"), newline="") as f:
        for r in csv.DictReader(f):
            out[(r["algorithm"], float(r["load"]), int(r["seed"]))] = (
                int(r["total_requests"]), int(r["completed"]), int(r["rejected"]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outdir", default="MERAF/results/outputs")
    ap.add_argument("--scenario", default="rete")
    ap.add_argument("--loads", type=float, nargs="+", default=[0.12, 0.16])
    ap.add_argument("--thresholds-ms", type=float, nargs="+",
                     default=[5000.0, 10000.0, 20000.0])
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    args = ap.parse_args()

    counts = load_counts(args.outdir, args.scenario)
    rows = []
    for load in args.loads:
        for algo in ALGOS:
            for X in args.thresholds_ms:
                succ_pcts, goodputs, p95s, p99s = [], [], [], []
                for seed in args.seeds:
                    f = os.path.join(args.outdir,
                                      f"completed_{args.scenario}_{algo}_{load:.3f}_seed{seed}.csv")
                    if (algo, load, seed) not in counts or not os.path.exists(f):
                        continue
                    total, _completed, _rejected = counts[(algo, load, seed)]
                    with open(f, newline="") as fh:
                        lat = [float(r["total_ms"]) for r in csv.DictReader(fh)]
                    ok = [l for l in lat if l <= X]
                    # Successes at true latency; everything else censored at X.
                    censored = sorted(ok) + [X] * (total - len(ok))
                    succ_pcts.append(100.0 * len(ok) / total)
                    goodputs.append(len(ok) / HORIZON_S)
                    p95s.append(pctile(censored, 95))
                    p99s.append(pctile(censored, 99))
                if not succ_pcts:
                    continue
                s, g = mean_std_ci95(succ_pcts), mean_std_ci95(goodputs)
                p95, p99 = mean_std_ci95(p95s), mean_std_ci95(p99s)
                # Censored percentile equals X once the failed fraction exceeds (100-p)%.
                sat95 = (100.0 - s.mean) > 5.0
                sat99 = (100.0 - s.mean) > 1.0
                rows.append(dict(algorithm=algo, load=load, threshold_ms=X,
                                  success_pct_mean=round(s.mean, 2),
                                  success_pct_ci95=round(s.ci95, 2),
                                  goodput_rps_mean=round(g.mean, 2),
                                  goodput_rps_ci95=round(g.ci95, 2),
                                  p95_client_ms_mean=round(p95.mean, 1),
                                  p95_client_ci95=round(p95.ci95, 1),
                                  p95_saturated_at_threshold=sat95,
                                  p99_client_ms_mean=round(p99.mean, 1),
                                  p99_client_ci95=round(p99.ci95, 1),
                                  p99_saturated_at_threshold=sat99))

    out_file = os.path.join(args.outdir, "client_timeout_view.csv")
    with open(out_file, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"[saved] {out_file} ({len(rows)} rows)\n")

    for load in args.loads:
        for X in args.thresholds_ms:
            print(f"===== load {load} req/ms, client timeout {X/1000:.0f} s =====")
            sel = sorted((r for r in rows if r["load"] == load and r["threshold_ms"] == X),
                          key=lambda r: -r["goodput_rps_mean"])
            for r in sel:
                p95 = (f"={X/1000:4.0f}s sat." if r["p95_saturated_at_threshold"]
                       else f"{r['p95_client_ms_mean']:8.1f}")
                p99 = (f"={X/1000:4.0f}s sat." if r["p99_saturated_at_threshold"]
                       else f"{r['p99_client_ms_mean']:8.1f}")
                print(f"  {r['algorithm']:33} success={r['success_pct_mean']:6.2f}%  "
                      f"goodput={r['goodput_rps_mean']:7.2f}  p95={p95}  p99={p99}")
            print()


if __name__ == "__main__":
    main()
