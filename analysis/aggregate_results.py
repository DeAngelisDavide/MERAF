#!/usr/bin/env python3
"""
Aggregate per-seed experiment results scattered under a root directory
(e.g. output/seed_1/, output/seed_2/, ...) into per-(scenario, algorithm,
load) statistics: mean, sample std, CI95 (Student t), min, max.

Inputs, in priority order: `summary_<scenario>_raw_perseed.csv` written by
run_experiments.py; otherwise per-request `completed_*.csv` logs, from which
metrics are recomputed and the generated-request count is rebuilt by
re-running the deterministic workload generator. Outputs per scenario:
`<scenario>_raw_all_seeds.csv`, `<scenario>_aggregated.csv`, `<scenario>_table.tex`.
See docs/analysis.md for details.

Usage:
    python3 -m MERAF.analysis.aggregate_results --root output --outdir aggregated
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import statistics as st
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# Shared with run_experiments.py so the two pipelines cannot silently diverge
# on statistics, LaTeX formatting or the offered-percentile penalty rule.
from ..core.stats_utils import mean_std_ci95
from ..reporting.make_latex_tables import write_main_comparison_table
from ..core.metrics import offered_percentile_reachable, CLIENT_TIMEOUT_MS

try:
    from MERAF.core.workload import WorkloadGenerator
    from MERAF.core.metrics import _percentile
    _HAS_MERAF = True
except Exception:
    _HAS_MERAF = False


RAW_PERSEED_RE = re.compile(r"^summary_(?P<scenario>[a-zA-Z0-9]+)_raw_perseed\.csv$")

COMPLETED_RE = re.compile(
    r"^completed_(?P<scenario>[a-zA-Z0-9]+)_(?P<algo>[a-zA-Z0-9\-]+)_"
    r"(?P<load>[0-9.]+)(?:_seed(?P<seed>\d+))?\.csv$"
)

# Fallback when the seed is not in the filename: first integer in the parent dir name.
DIR_SEED_RE = re.compile(r"(\d+)")


def find_files(root: str, pattern: re.Pattern) -> List[Tuple[str, re.Match]]:
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            m = pattern.match(fn)
            if m:
                out.append((os.path.join(dirpath, fn), m))
    return out


def infer_seed_from_path(path: str) -> Optional[int]:
    parent = os.path.basename(os.path.dirname(path))
    m = DIR_SEED_RE.search(parent)
    return int(m.group(1)) if m else None


# Offered percentiles are deliberately excluded: they are aggregated separately
# in aggregate(), over reachable seeds only.
NUMERIC_FIELDS = ["throughput_rps", "goodput_rps", "latency_mean_ms", "latency_p50_ms",
                  "latency_p95_ms", "latency_p99_ms", "reject_rate_pct"]


def load_raw_perseed(root: str, verbose: bool) -> Dict[str, List[dict]]:
    """Return {scenario: [raw rows, one per (algorithm, load, seed)]}."""
    files = find_files(root, RAW_PERSEED_RE)
    if verbose:
        print(f"[primary] found {len(files)} 'summary_*_raw_perseed.csv' file(s)")
    by_scenario: Dict[str, List[dict]] = defaultdict(list)
    for path, m in files:
        scenario = m.group("scenario")
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        if verbose:
            print(f"    {path}: {len(rows)} rows")
        by_scenario[scenario].extend(rows)
    return by_scenario


def load_from_completed(root: str, horizon_ms: float, slo_ms: float, verbose: bool) -> Dict[str, List[dict]]:
    """Rebuild per-seed rows from per-request `completed_*.csv` logs."""
    if not _HAS_MERAF:
        raise RuntimeError(
            "The fallback path requires the MERAF.core library (run as "
            "python3 -m MERAF.analysis.aggregate_results, so that the total number of "
            "generated requests and the percentiles are rebuilt with the same functions)."
        )
    files = find_files(root, COMPLETED_RE)
    if verbose:
        print(f"[fallback] found {len(files)} 'completed_*.csv' file(s)")
    by_scenario: Dict[str, List[dict]] = defaultdict(list)

    for path, m in files:
        scenario = m.group("scenario")
        algo = m.group("algo")
        load = float(m.group("load"))
        seed = m.group("seed")
        seed = int(seed) if seed is not None else infer_seed_from_path(path)
        if seed is None:
            if verbose:
                print(f"    WARNING: cannot determine the seed for {path}, skipped")
            continue

        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            totals = [float(r["total_ms"]) for r in reader]
        n_completed = len(totals)

        # The generator is deterministic in (rate, seed, horizon) and independent
        # of algorithm/scenario, so the generated count can be rebuilt exactly.
        gen = WorkloadGenerator(rate_per_ms=load, seed=seed)
        n_total = len(gen.generate(horizon_ms))
        n_rejected = max(n_total - n_completed, 0)
        reject_rate_pct = 100.0 * n_rejected / n_total if n_total else 0.0

        # Same convention as metrics.summarize(): each reject is charged the
        # client timeout, not a multiple of the SLO (see docs/analysis.md).
        reject_penalty_ms = CLIENT_TIMEOUT_MS
        offered_totals = totals + [reject_penalty_ms] * n_rejected
        n_within_slo = sum(1 for t in totals if t <= slo_ms)

        row = dict(
            algorithm=algo, load=load, seed=seed,
            total_requests=n_total, completed=n_completed,
            rejected=n_rejected,
            reject_rate_pct=reject_rate_pct,
            horizon_ms=horizon_ms,
            reject_penalty_ms=reject_penalty_ms,
            throughput_rps=n_completed / (horizon_ms / 1000.0) if horizon_ms else 0.0,
            goodput_rps=n_within_slo / (horizon_ms / 1000.0) if horizon_ms else 0.0,
            latency_mean_ms=st.mean(totals) if totals else float("nan"),
            latency_p50_ms=_percentile(totals, 0.50),
            latency_p95_ms=_percentile(totals, 0.95),
            latency_p99_ms=_percentile(totals, 0.99),
            latency_p95_offered_ms=_percentile(offered_totals, 0.95),
            latency_p99_offered_ms=_percentile(offered_totals, 0.99),
            latency_p95_offered_reachable=offered_percentile_reachable(reject_rate_pct, 0.95),
            latency_p99_offered_reachable=offered_percentile_reachable(reject_rate_pct, 0.99),
        )
        by_scenario[scenario].append(row)
        if verbose:
            print(f"    {path}: algo={algo} load={load} seed={seed} "
                  f"n_completed={n_completed} n_total={n_total}")
    return by_scenario


def aggregate(rows: List[dict]) -> Dict[Tuple[str, float], dict]:
    """Group rows by (algorithm, load) and compute mean/std(ddof=1)/CI95/min/max across seeds."""
    groups: Dict[Tuple[str, float], List[dict]] = defaultdict(list)
    for r in rows:
        key = (r["algorithm"], float(r["load"]))
        groups[key].append(r)

    out = {}
    for key, group_rows in groups.items():
        seeds = sorted({int(r["seed"]) for r in group_rows})
        n = len(group_rows)
        agg = dict(algorithm=key[0], load=key[1], n_seeds=n, seeds=seeds,
                   reject_penalty_ms=float(group_rows[0].get("reject_penalty_ms", CLIENT_TIMEOUT_MS)))
        for field in NUMERIC_FIELDS:
            vals = [float(r[field]) for r in group_rows]
            r_stats = mean_std_ci95(vals)
            agg[f"{field}_mean"] = r_stats.mean
            agg[f"{field}_std"] = r_stats.std
            agg[f"{field}_ci95"] = r_stats.ci95
            agg[f"{field}_min"] = r_stats.min
            agg[f"{field}_max"] = r_stats.max

        # Offered percentiles: average only the seeds where the percentile is
        # reachable (per-row flag). Mixing reachable seeds with ones saturated at
        # CLIENT_TIMEOUT_MS would produce a meaningless mean.
        for p_name in ("p95", "p99"):
            value_field = f"latency_{p_name}_offered_ms"
            reach_field = f"latency_{p_name}_offered_reachable"
            reachable_vals = [float(r[value_field]) for r in group_rows
                               if r.get(reach_field) in ("True", True)]
            n_reachable = len(reachable_vals)
            agg[f"{value_field}_n_reachable"] = n_reachable
            if n_reachable > 0:
                r_stats = mean_std_ci95(reachable_vals)
                agg[f"{value_field}_mean"] = r_stats.mean
                agg[f"{value_field}_std"] = r_stats.std
                agg[f"{value_field}_ci95"] = r_stats.ci95
                agg[f"{value_field}_min"] = r_stats.min
                agg[f"{value_field}_max"] = r_stats.max
            else:
                agg[f"{value_field}_mean"] = float("nan")
                agg[f"{value_field}_std"] = float("nan")
                agg[f"{value_field}_ci95"] = float("nan")
                agg[f"{value_field}_min"] = float("nan")
                agg[f"{value_field}_max"] = float("nan")
        out[key] = agg
    return out


def write_raw_csv(path: str, rows: List[dict]) -> None:
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def write_aggregated_csv(path: str, agg: Dict[Tuple[str, float], dict]) -> None:
    if not agg:
        return
    rows = sorted(agg.values(), key=lambda r: (r["load"], r["algorithm"]))
    fieldnames = [k for k in rows[0].keys() if k != "seeds"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: v for k, v in r.items() if k != "seeds"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="output",
                     help="Root directory containing the per-seed subdirectories")
    ap.add_argument("--outdir", default="aggregated",
                     help="Directory where the CSVs and LaTeX tables are saved")
    ap.add_argument("--horizon-ms", type=float, default=30000.0,
                     help="Simulated horizon (ms) used to generate the data "
                          "(default 30 s, matching the runs); needed only by the "
                          "fallback path that rebuilds metrics from completed_*.csv")
    ap.add_argument("--slo-ms", type=float, default=2000.0,
                     help="SLO (ms) used only to compute goodput (requests "
                          "completed within the SLO) in the fallback path; the "
                          "rejection penalty of the 'offered' metrics is the client "
                          "timeout (CLIENT_TIMEOUT_MS = 60 s), not the SLO. The "
                          "primary path reads everything from the CSV written by "
                          "run_experiments.py")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    by_scenario = load_raw_perseed(args.root, args.verbose)
    if not by_scenario:
        print("No 'summary_*_raw_perseed.csv' found: rebuilding from "
              "completed_*.csv (fallback path)...")
        by_scenario = load_from_completed(args.root, args.horizon_ms, args.slo_ms, args.verbose)

    if not by_scenario:
        print("ERROR: neither 'summary_*_raw_perseed.csv' nor "
              "'completed_*.csv' found under", args.root)
        print("Check the directory layout with: find", args.root, "-name '*.csv'")
        return 1

    for scenario, rows in by_scenario.items():
        print(f"\n=== Scenario: {scenario} ({len(rows)} raw rows) ===")
        raw_file = os.path.join(args.outdir, f"{scenario}_raw_all_seeds.csv")
        write_raw_csv(raw_file, rows)

        agg = aggregate(rows)
        agg_file = os.path.join(args.outdir, f"{scenario}_aggregated.csv")
        write_aggregated_csv(agg_file, agg)

        tex_file = os.path.join(args.outdir, f"{scenario}_table.tex")
        write_main_comparison_table(tex_file, scenario, agg)

        print(f"{'algorithm':<20}{'load':>8}{'n_seed':>8}{'RPS':>16}{'p50(ms)':>18}"
              f"{'p95(ms)':>18}{'p99(ms)':>18}{'rejects%':>14}")
        for key in sorted(agg.keys(), key=lambda k: (k[1], k[0])):
            a = agg[key]
            print(f"{a['algorithm']:<20}{a['load']:>8.3f}{a['n_seeds']:>8}"
                  f"{a['throughput_rps_mean']:>9.2f}+/-{a['throughput_rps_ci95']:<6.2f}"
                  f"{a['latency_p50_ms_mean']:>10.1f}+/-{a['latency_p50_ms_ci95']:<6.1f}"
                  f"{a['latency_p95_ms_mean']:>10.1f}+/-{a['latency_p95_ms_ci95']:<6.1f}"
                  f"{a['latency_p99_ms_mean']:>10.1f}+/-{a['latency_p99_ms_ci95']:<6.1f}"
                  f"{a['reject_rate_pct_mean']:>7.2f}+/-{a['reject_rate_pct_ci95']:<6.2f}")

        print(f"[saved] {raw_file}")
        print(f"[saved] {agg_file}")

    print("\nDone. Output in", os.path.abspath(args.outdir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
