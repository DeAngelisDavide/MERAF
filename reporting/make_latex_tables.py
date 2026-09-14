#!/usr/bin/env python3
"""Render LaTeX tables from the CSVs written by the experiment scripts.

  main comparison    summary_<scenario>_aggregated.csv       -> algorithm x load table
  profile-breakdown  profile_breakdown_<scenario>.csv        -> per-profile (T,P) latencies
  equivalence        equivalence_<scenario>.csv              -> TOST verdicts per profile pair
  sensitivity        sensitivity_sweep_<scenario>_load<X>.csv -> MERAF advantage ratio sweep

The generate_*_table() functions are called by run_experiments.py, equivalence_test.py
and sensitivity_sweep.py right after they write their CSV; the CLI regenerates one table.

Usage:
    python3 -m MERAF.reporting.make_latex_tables profile-breakdown \\
        --file MERAF/results/outputs/profile_breakdown_locale.csv --algo meraf --load 0.16
    python3 -m MERAF.reporting.make_latex_tables equivalence --file MERAF/results/outputs/equivalence_locale.csv
    python3 -m MERAF.reporting.make_latex_tables sensitivity \\
        --file MERAF/results/outputs/sensitivity_sweep_locale_load0.160.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from ..core.stats_utils import mean_std_ci95

KEY_RE = re.compile(r"^(?P<algo>.+)@(?P<load>[0-9.]+)@seed(?P<seed>\d+)$")


def _latex_escape(s: str) -> str:
    return s.replace("_", r"\_")


def _write(path: str, lines: List[str]) -> str:
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[saved] {path}")
    return path


def _default_out(in_path: str, suffix: str) -> str:
    base = os.path.splitext(in_path)[0]
    return f"{base}_{suffix}.tex"


def write_main_comparison_table(path: str, scenario: str,
                                 agg: Dict[Tuple[str, float], dict]) -> str:
    """Write the algorithm x load comparison table for one scenario.

    Args:
        agg: (algorithm, load) -> dict with algorithm/load/n_seeds and
            <metric>_mean/_ci95 fields, as built by run_experiments._aggregate
            or aggregate_results.aggregate.
    """
    rows = sorted(agg.values(), key=lambda r: (r["load"], r["algorithm"]))
    n_seeds = rows[0].get("n_seeds", "N")
    penalty = rows[0].get("reject_penalty_ms_mean") or rows[0].get("reject_penalty_ms")
    penalty_s = f"{penalty:.0f}" if penalty is not None else "?"
    lines = []
    lines.append(r"\begin{table*}[htbp]")
    lines.append(r"\centering")
    lines.append(
        r"\caption{MERAF vs. baselines -- scenario " + scenario +
        r", mean $\pm$ 95\% CI over " + str(n_seeds) + r" replications. "
        r"Offered p99: rejected requests are charged " + penalty_s +
        r"ms (client timeout); the mean is computed ONLY over the seeds with a "
        r"reject rate below 1\% (above that threshold the percentile falls in the "
        r"censored tail and is merely a convention, no longer a well-founded "
        r"percentile, see text); ``n/a'' if no seed is below the threshold; "
        r"$k$/$n$ reports how many seeds out of how many were used when "
        r"$k<n$.}")
    lines.append(r"\label{tab:meraf_" + scenario + "}")
    # Offered p99 (completed + rejected) stays a separate column next to admitted p99
    # and reject rate: alone it is not interpretable above the rejection threshold
    # (see core.metrics.offered_percentile_reachable).
    lines.append(r"\begin{tabular}{@{}llcccccc@{}}")
    lines.append(r"\toprule")
    lines.append(r"\textbf{Load} & \textbf{Algorithm} & \textbf{RPS} & "
                 r"\textbf{p50 (ms)} & \textbf{p95 (ms)} & \textbf{p99 (ms)} & "
                 r"\textbf{Offered p99 (ms)} & \textbf{Rejected (\%)} \\")
    lines.append(r"\midrule")
    last_load = None
    for r in rows:
        load_cell = f"{r['load']:.3f}" if r["load"] != last_load else ""
        last_load = r["load"]
        n_reachable = r.get("latency_p99_offered_ms_n_reachable", n_seeds)
        if n_reachable == 0:
            offered_cell = "n/a"
        elif n_reachable < n_seeds:
            offered_cell = (f"{r['latency_p99_offered_ms_mean']:.1f} $\\pm$ "
                            f"{r['latency_p99_offered_ms_ci95']:.1f} "
                            f"({n_reachable}/{n_seeds})")
        else:
            offered_cell = (f"{r['latency_p99_offered_ms_mean']:.1f} $\\pm$ "
                            f"{r['latency_p99_offered_ms_ci95']:.1f}")
        lines.append(
            f"{load_cell} & {_latex_escape(r['algorithm'])} & "
            f"{r['throughput_rps_mean']:.2f} $\\pm$ {r['throughput_rps_ci95']:.2f} & "
            f"{r['latency_p50_ms_mean']:.1f} $\\pm$ {r['latency_p50_ms_ci95']:.1f} & "
            f"{r['latency_p95_ms_mean']:.1f} $\\pm$ {r['latency_p95_ms_ci95']:.1f} & "
            f"{r['latency_p99_ms_mean']:.1f} $\\pm$ {r['latency_p99_ms_ci95']:.1f} & "
            f"{offered_cell} & "
            f"{r['reject_rate_pct_mean']:.2f} $\\pm$ {r['reject_rate_pct_ci95']:.2f} \\\\"
        )
    lines.append(r"\bottomrule")
    lines.append(r"\end{tabular}")
    lines.append(r"\end{table*}")
    return _write(path, lines)


def generate_profile_breakdown_table(file: str, algo: str, load: float,
                                      out: Optional[str] = None) -> Optional[str]:
    """Write the per-profile (T,P) latency table for one (algo, load).

    Returns the written path, or None (message printed, no exception) when no
    rows match, so a caller looping over loads is never interrupted.
    """
    by_profile: Dict[str, List[dict]] = defaultdict(list)
    with open(file, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            m = KEY_RE.match(row["algorithm"])
            if not m:
                continue
            row_algo, row_load = m.group("algo"), float(m.group("load"))
            if row_algo != algo or abs(row_load - load) > 1e-9:
                continue
            by_profile[row["profile"]].append(row)

    if not by_profile:
        print(f"[make_latex_tables] no row for algorithm={algo} load={load} in {file}, "
              f"per-profile table not generated")
        return None

    scenario_guess = os.path.basename(file)
    m = re.match(r"profile_breakdown_(?P<scenario>[a-zA-Z0-9]+)\.csv", scenario_guess)
    scenario = m.group("scenario") if m else "scenario"

    rows = []
    for profile in sorted(by_profile.keys()):
        recs = by_profile[profile]
        n = len(recs)
        p50 = mean_std_ci95([float(r["latency_p50_ms"]) for r in recs])
        p95 = mean_std_ci95([float(r["latency_p95_ms"]) for r in recs])
        p99 = mean_std_ci95([float(r["latency_p99_ms"]) for r in recs])
        rows.append((profile, n, p50, p95, p99))

    lines = []
    lines.append(r"\begin{table*}[htbp]")
    lines.append(r"\centering")
    lines.append(
        r"\caption{Security Profile Latency Isolation (" + _latex_escape(algo) +
        f" at {load:.3f} req/ms, " + _latex_escape(scenario) +
        r" environment, mean $\pm$ 95\% CI over " + str(rows[0][1]) + r" replications)}")
    lines.append(r"\label{tab:profile_" + scenario + "_" + algo.replace("-", "") +
                 f"_{load:.3f}".replace(".", "") + "}")
    lines.append(r"\begin{tabular}{@{}lccc@{}}")
    lines.append(r"\toprule")
    lines.append(r"\textbf{Security Profile (T,P)} & \textbf{p50 (ms)} & "
                 r"\textbf{p95 (ms)} & \textbf{p99 (ms)} \\")
    lines.append(r"\midrule")
    for profile, n, p50, p95, p99 in rows:
        lines.append(
            f"{_latex_escape(profile)} & "
            f"{p50.mean:.1f} $\\pm$ {p50.ci95:.1f} & "
            f"{p95.mean:.1f} $\\pm$ {p95.ci95:.1f} & "
            f"{p99.mean:.1f} $\\pm$ {p99.ci95:.1f} \\\\"
        )
    lines.append(r"\bottomrule")
    lines.append(r"\end{tabular}")
    lines.append(r"\end{table*}")

    out = out or _default_out(file, f"{algo}_{load:.3f}".replace(".", ""))
    return _write(out, lines)


def generate_equivalence_table(file: str, algo: Optional[str] = None,
                                load: Optional[float] = None,
                                out: Optional[str] = None) -> Optional[str]:
    """Write the TOST verdict table, optionally filtered by algo and/or load."""
    with open(file, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        print(f"[make_latex_tables] {file} is empty, equivalence table not generated")
        return None
    if algo:
        rows = [r for r in rows if r["algorithm"] == algo]
    if load is not None:
        rows = [r for r in rows if abs(float(r["load"]) - load) < 1e-9]
    if not rows:
        print(f"[make_latex_tables] no row after the filter algo={algo}/load={load} in {file}, "
              f"equivalence table not generated")
        return None

    scenario = rows[0]["scenario"]
    metric = rows[0]["metric"]
    margin = rows[0]["margin_ms"]

    lines = []
    lines.append(r"\begin{table*}[htbp]")
    lines.append(r"\centering")
    lines.append(
        r"\caption{Equivalence test (TOST) between security profiles -- " +
        _latex_escape(scenario) + ", metric " + _latex_escape(metric) +
        ", margin " + str(margin) + "ms}")
    lines.append(r"\label{tab:equivalence_" + scenario + "}")
    lines.append(r"\begin{tabular}{@{}llccccc@{}}")
    lines.append(r"\toprule")
    lines.append(r"\textbf{Algorithm} & \textbf{Load} & \textbf{Profile A vs B} & "
                 r"\textbf{Diff (ms)} & \textbf{$t_{lower}$} & \textbf{$t_{upper}$} & "
                 r"\textbf{Equivalent?} \\")
    lines.append(r"\midrule")
    for r in sorted(rows, key=lambda r: (r["algorithm"], float(r["load"]))):
        verdict = r"\checkmark" if r["equivalent"] == "True" else r"$\times$"
        lines.append(
            f"{_latex_escape(r['algorithm'])} & {float(r['load']):.3f} & "
            f"{_latex_escape(r['profile_a'])} vs {_latex_escape(r['profile_b'])} & "
            f"{float(r['diff']):.2f} & {r['t_lower']} & {r['t_upper']} & {verdict} \\\\"
        )
    lines.append(r"\bottomrule")
    lines.append(r"\end{tabular}")
    lines.append(r"\end{table*}")

    out = out or _default_out(file, "table")
    return _write(out, lines)


def generate_sensitivity_table(file: str, out: Optional[str] = None) -> Optional[str]:
    """Write the sensitivity-sweep table (MERAF advantage ratio per hyperparameter value)."""
    with open(file, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        print(f"[make_latex_tables] {file} is empty, sensitivity table not generated")
        return None

    lines = []
    lines.append(r"\begin{table*}[htbp]")
    lines.append(r"\centering")
    lines.append(r"\caption{Sensitivity sweep: MERAF advantage ratio over the "
                 r"best baseline (p99), as a function of each hyperparameter, "
                 r"other hyperparameters held at their default value}")
    lines.append(r"\label{tab:sensitivity}")
    lines.append(r"\begin{tabular}{@{}llccc@{}}")
    lines.append(r"\toprule")
    lines.append(r"\textbf{Parameter} & \textbf{Value} & \textbf{MERAF p99 (ms)} & "
                 r"\textbf{Best baseline p99 (ms)} & \textbf{Advantage ratio} \\")
    lines.append(r"\midrule")
    last_param = None
    for r in rows:
        param_cell = _latex_escape(r["param"]) if r["param"] != last_param else ""
        last_param = r["param"]
        lines.append(
            f"{param_cell} & {r['value']} & "
            f"{float(r['meraf_p99_mean']):.2f} $\\pm$ {float(r['meraf_p99_ci95']):.2f} & "
            f"{float(r['best_baseline_p99_mean']):.2f} $\\pm$ {float(r['best_baseline_p99_ci95']):.2f} & "
            f"{float(r['advantage_ratio']):.2f}$\\times$ \\\\"
        )
    lines.append(r"\bottomrule")
    lines.append(r"\end{tabular}")
    lines.append(r"\end{table*}")

    out = out or _default_out(file, "table")
    return _write(out, lines)


def _cli_profile_breakdown(args: argparse.Namespace) -> int:
    return 0 if generate_profile_breakdown_table(args.file, args.algo, args.load, args.out) else 1


def _cli_equivalence(args: argparse.Namespace) -> int:
    return 0 if generate_equivalence_table(args.file, args.algo, args.load, args.out) else 1


def _cli_sensitivity(args: argparse.Namespace) -> int:
    return 0 if generate_sensitivity_table(args.file, args.out) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("profile-breakdown",
                         help="Per-profile (T,P) table from profile_breakdown_<scenario>.csv")
    p1.add_argument("--file", required=True)
    p1.add_argument("--algo", required=True, help="Algorithm name, e.g. meraf")
    p1.add_argument("--load", required=True, type=float, help="Load, e.g. 0.16")
    p1.add_argument("--out", default=None)
    p1.set_defaults(func=_cli_profile_breakdown)

    p2 = sub.add_parser("equivalence",
                         help="TOST verdict table from equivalence_<scenario>.csv")
    p2.add_argument("--file", required=True)
    p2.add_argument("--algo", default=None, help="Filter by algorithm (default: all)")
    p2.add_argument("--load", default=None, type=float, help="Filter by load (default: all)")
    p2.add_argument("--out", default=None)
    p2.set_defaults(func=_cli_equivalence)

    p3 = sub.add_parser("sensitivity",
                         help="Sensitivity-sweep table from sensitivity_sweep_*.csv")
    p3.add_argument("--file", required=True)
    p3.add_argument("--out", default=None)
    p3.set_defaults(func=_cli_sensitivity)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
