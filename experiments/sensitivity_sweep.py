#!/usr/bin/env python3
"""
One-factor-at-a-time sensitivity sweep of MERAF's hyperparameters
(handshake_sigma_multiplier, sticky_tolerance, beta_relay, lambda, eta,
gamma) around the defaults used in run_experiments.build_dispatcher. For
each grid value it reports MERAF's p99, the best (lowest) p99 among the
security-agnostic baselines at the same scenario/load/seed, and their
ratio (advantage_ratio > 1 means MERAF wins).

Outputs: <outdir>/sensitivity_sweep_<scenario>_load<load>.csv and the
matching .tex table (unless --no-latex).

Usage:
    python3 -m MERAF.experiments.sensitivity_sweep --scenario locale --load 0.16 --seeds 1 2 3
"""
from __future__ import annotations

import argparse
import csv
import os
from typing import Dict, List

from ..core.model import CostCalibration, ScoreWeights
from ..core.dispatchers import MERAFDispatcher, RoundRobinDispatcher, \
    LeastConnectionsDispatcher, PowerOfTwoChoicesDispatcher, EWMALeastResponseTimeDispatcher
from ..core.piano_a import PianoA
from ..core.simulator import Simulator
from ..core.workload import WorkloadGenerator
from ..core.metrics import summarize
from ..core.stats_utils import mean_std_ci95
from .run_experiments import make_backend_fleet
from ..reporting.make_latex_tables import generate_sensitivity_table

BASELINE_NAMES = ["round-robin", "least-connections", "power-of-2", "ewma-lrt"]


def _build_baseline(name: str, calib: CostCalibration, seed: int):
    if name == "round-robin":
        return RoundRobinDispatcher(calibration=calib)
    if name == "least-connections":
        return LeastConnectionsDispatcher(calibration=calib, seed=seed + 3000)
    if name == "power-of-2":
        return PowerOfTwoChoicesDispatcher(calibration=calib, seed=seed + 2000)
    if name == "ewma-lrt":
        return EWMALeastResponseTimeDispatcher(calibration=calib, seed=seed + 4000)
    raise ValueError(name)


def run_meraf(scenario: str, load: float, horizon_ms: float, seed: int, n_tenants: int,
              calib: CostCalibration, weights: ScoreWeights, sticky_tolerance: float,
              handshake_sigma_multiplier: float, slo_ms: float) -> float:
    """Return MERAF's latency_p99_ms for one replica with the given parameters."""
    backends = make_backend_fleet(scenario, slo_ms=slo_ms)
    dispatcher = MERAFDispatcher(
        calibration=calib, weights=weights, candidates_d=6,
        sticky_tolerance=sticky_tolerance,
        handshake_sigma_multiplier=handshake_sigma_multiplier,
        seed=seed + 1000)
    tenants = [f"tenant{i}" for i in range(n_tenants)]
    gen = WorkloadGenerator(rate_per_ms=load, seed=seed, tenants=tenants)
    requests = gen.generate(horizon_ms)
    sim = Simulator(backends, dispatcher, PianoA())
    sim.run(requests)
    return summarize(sim, total_requests=len(requests), horizon_ms=horizon_ms,
                      algorithm_name="meraf", slo_ms=slo_ms).latency_p99_ms


def run_baseline_best_p99(scenario: str, load: float, horizon_ms: float, seed: int,
                           n_tenants: int, calib: CostCalibration, slo_ms: float) -> float:
    """Return the lowest p99 across BASELINE_NAMES for one replica (advantage-ratio denominator)."""
    best = float("inf")
    for name in BASELINE_NAMES:
        backends = make_backend_fleet(scenario, slo_ms=slo_ms)
        dispatcher = _build_baseline(name, calib, seed)
        tenants = [f"tenant{i}" for i in range(n_tenants)]
        gen = WorkloadGenerator(rate_per_ms=load, seed=seed, tenants=tenants)
        requests = gen.generate(horizon_ms)
        sim = Simulator(backends, dispatcher, PianoA())
        sim.run(requests)
        p99 = summarize(sim, total_requests=len(requests), horizon_ms=horizon_ms,
                         algorithm_name=name, slo_ms=slo_ms).latency_p99_ms
        best = min(best, p99)
    return best


# One-at-a-time grids centred on the defaults of run_experiments.build_dispatcher.
SWEEPS = {
    "handshake_sigma_multiplier": [1.0, 1.25, 1.5, 2.0, 3.0],
    "sticky_tolerance": [1.00, 1.05, 1.10, 1.20, 1.50],
    "beta_relay": [0.5, 0.75, 1.0, 1.5, 2.0],
    "lambda": [0.6, 0.9, 1.2, 1.8, 2.4],
    "eta": [0.3, 0.45, 0.6, 0.9, 1.2],
    "gamma": [0.0, 0.125, 0.25, 0.5, 1.0],
}

DEFAULTS = dict(lam=1.2, eta=0.6, gamma=0.25, sticky_tolerance=1.10,
                handshake_sigma_multiplier=1.5, beta_relay=1.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="locale", choices=["locale", "rete"])
    ap.add_argument("--load", type=float, default=0.16,
                     help="Arrival rate (req/ms); default = the most interesting "
                          "overload regime of the paper")
    ap.add_argument("--horizon-ms", type=float, default=30000.0)
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--tenants", type=int, default=50)
    ap.add_argument("--slo-ms", type=float, default=2000.0)
    ap.add_argument("--params", nargs="+", default=list(SWEEPS.keys()),
                     choices=list(SWEEPS.keys()),
                     help="Subset of parameters to sweep (default: all)")
    ap.add_argument("--outdir", default="MERAF/results/outputs")
    ap.add_argument("--no-latex", action="store_true",
                     help="Do not generate the .tex table automatically (by "
                          "default it is written right after the CSV)")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    out_rows: List[dict] = []

    for param in args.params:
        print(f"\n===== SWEEP: {param} =====")
        for value in SWEEPS[param]:
            lam, eta, gamma = DEFAULTS["lam"], DEFAULTS["eta"], DEFAULTS["gamma"]
            sticky_tolerance = DEFAULTS["sticky_tolerance"]
            handshake_sigma_multiplier = DEFAULTS["handshake_sigma_multiplier"]
            beta_relay = DEFAULTS["beta_relay"]

            if param == "lambda":
                lam = value
            elif param == "eta":
                eta = value
            elif param == "gamma":
                gamma = value
            elif param == "sticky_tolerance":
                sticky_tolerance = value
            elif param == "handshake_sigma_multiplier":
                handshake_sigma_multiplier = value
            elif param == "beta_relay":
                beta_relay = value

            weights = ScoreWeights(lam=lam, eta=eta, gamma=gamma)
            calib = CostCalibration(beta_relay=beta_relay)

            meraf_p99s, baseline_p99s = [], []
            for seed in args.seeds:
                meraf_p99s.append(run_meraf(
                    args.scenario, args.load, args.horizon_ms, seed, args.tenants,
                    calib, weights, sticky_tolerance, handshake_sigma_multiplier, args.slo_ms))
                baseline_p99s.append(run_baseline_best_p99(
                    args.scenario, args.load, args.horizon_ms, seed, args.tenants,
                    calib, args.slo_ms))

            meraf_stats = mean_std_ci95(meraf_p99s)
            baseline_stats = mean_std_ci95(baseline_p99s)
            ratio = (baseline_stats.mean / meraf_stats.mean
                     if meraf_stats.mean > 0 else float("nan"))

            out_rows.append(dict(
                param=param, value=value,
                meraf_p99_mean=meraf_stats.mean, meraf_p99_ci95=meraf_stats.ci95,
                best_baseline_p99_mean=baseline_stats.mean,
                best_baseline_p99_ci95=baseline_stats.ci95,
                advantage_ratio=ratio, n_seeds=len(args.seeds),
            ))
            print(f"  {param}={value!s:<8} meraf_p99={meraf_stats.mean:8.2f}+/-{meraf_stats.ci95:.2f}ms  "
                  f"best_baseline_p99={baseline_stats.mean:8.2f}+/-{baseline_stats.ci95:.2f}ms  "
                  f"advantage_ratio={ratio:.2f}x")

    out_path = os.path.join(args.outdir, f"sensitivity_sweep_{args.scenario}_load{args.load:.3f}.csv")
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print(f"\n[saved] {out_path}")

    if not args.no_latex:
        generate_sensitivity_table(out_path)

    print("\nIf 'advantage_ratio' stays > 1 (MERAF wins) across the whole "
          "grid of each parameter, the main result does not depend fragilely "
          "on the default value -- otherwise the paper must report the "
          "threshold beyond which the advantage reverses.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
