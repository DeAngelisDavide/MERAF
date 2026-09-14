#!/usr/bin/env python3
"""
Run MERAF, its ablation variants and the baseline dispatchers on a
heterogeneous backend fleet, over several load levels and two scenarios
("locale"/"rete"), with independent seeds per cell. Writes to --outdir:

  summary_<scenario>_raw_perseed.csv          -- one row per (algorithm, load, seed)
  summary_<scenario>_aggregated.csv           -- one row per (algorithm, load): mean/std/CI95 over seeds
  completed_<scenario>_<algo>_<load>_seed<N>.csv  -- per-request log (unless --no-save-completed)
  profile_breakdown_<scenario>.csv            -- metrics per (T,P) profile
  summary_<scenario>_table.tex + profile tables   -- LaTeX (unless --no-latex)

Usage:
    python3 -m MERAF.experiments.run_experiments
    python3 -m MERAF.experiments.run_experiments --horizon-ms 60000 --loads 50 150 300
"""
from __future__ import annotations

import argparse
import os
import sys

from ..core.backend import Backend, BackendSpec
from ..core.dispatchers import (MERAFDispatcher, RoundRobinDispatcher,
                                   LeastConnectionsDispatcher,
                                   PowerOfTwoChoicesDispatcher,
                                   EWMALeastResponseTimeDispatcher,
                                   SecurityConstraintLeastLoadedDispatcher,
                                   SecurityConstraintFastestDispatcher)
from ..core.model import CostCalibration, ScoreWeights, derive_resource_threshold
from ..core.piano_a import PianoA
from ..core.simulator import Simulator
from ..core.workload import WorkloadGenerator
from ..core.metrics import summarize, per_profile_breakdown, \
    write_summaries_csv, write_completed_csv, write_profile_breakdown_csv
from ..core.stats_utils import mean_std_ci95
from ..reporting.make_latex_tables import write_main_comparison_table, generate_profile_breakdown_table


def make_backend_fleet(scenario: str, n_backends: int = 4, slo_ms: float = 2000.0) -> list:
    """Build a heterogeneous fleet of `n_backends` for `scenario` ("locale" or "rete").

    Bandwidth and RTT are calibrated on the empirical measurements; the last
    node is deliberately weaker. Resource thresholds h_* are derived per node
    from its real capacity and `slo_ms` (see model.derive_resource_threshold)
    instead of being shared constants. Details in docs/experiments.md.
    """
    ref_cost = CostCalibration().cost_decrypt(5 * 1024 * 1024, use_kem_envelope=True)
    ref_bytes = ref_cost["bytes"]
    ref_cpu_ms = max(ref_cost["cpu_ms"] - ref_cost["decrypt_ms"], 1e-6)
    ref_decrypt_ms = ref_cost["decrypt_ms"]

    # Security tiers (2=highly trusted, 1=semi-trusted, 0=untrusted) are used
    # only by the security-constraint baselines; MERAF ignores them. Trusted
    # nodes are a 25% minority per tier (at least one node each).
    n_lht = max(1, n_backends // 4)
    n_lst = max(1, n_backends // 4)

    backends = []
    for i in range(n_backends):
        weak = (i == n_backends - 1)
        security_tier = 2 if i < n_lht else (1 if i < n_lht + n_lst else 0)
        if scenario == "locale":
            net_cap_mbps = (900.0 if weak else 2200.0)  # MB/s, from report_locali.md
            cpu_cap = (15.0 if weak else 40.0)
            decrypt_cap = (8.0 if weak else 20.0)
            rtt = 0.0
        elif scenario == "rete":
            net_cap_mbps = (60.0 if weak else 180.0)    # MB/s, from report_rete.md
            cpu_cap = (15.0 if weak else 40.0)
            decrypt_cap = (8.0 if weak else 20.0)
            rtt = (400.0 if weak else 120.0)
        else:
            raise ValueError(scenario)

        # 1 MB/s ~= 1024 byte/ms
        net_cap_bytes_per_ms = net_cap_mbps * 1024

        h_net = derive_resource_threshold(net_cap_bytes_per_ms, slo_ms, ref_bytes)
        h_cpu = derive_resource_threshold(cpu_cap, slo_ms, ref_cpu_ms)
        h_decrypt = derive_resource_threshold(decrypt_cap, slo_ms, ref_decrypt_ms)

        spec = BackendSpec(
            name=f"{scenario}-be{i}",
            cpu_capacity_per_ms=cpu_cap,
            net_capacity_bytes_per_ms=net_cap_bytes_per_ms,
            decrypt_capacity_per_ms=decrypt_cap,
            mem_capacity_mb=4096.0 if not weak else 2048.0,
            h_cpu=h_cpu, h_net=h_net, h_decrypt=h_decrypt, h_mem=0.9,
            base_rtt_ms=rtt, security_tier=security_tier,
        )
        backends.append(Backend(spec))
    return backends


def build_dispatcher(name: str, calib: CostCalibration, seed: int = 0, candidates_d: int = 6):
    """Instantiate the dispatcher called `name` (see ALGORITHMS).

    Each dispatcher family gets its own seed offset so replicas use
    independent tie-break streams. With the default 4-node fleet,
    candidates_d=6 >= n_backends means MERAF scans the whole fleet on every
    request; power-of-d sampling is exercised only with --n-backends > --candidates-d.
    """
    if name == "meraf":
        return MERAFDispatcher(calibration=calib, weights=ScoreWeights(lam=1.2, eta=0.6, gamma=0.25),
                                  candidates_d=candidates_d, seed=seed + 1000)
    if name == "round-robin":
        return RoundRobinDispatcher(calibration=calib)
    if name == "least-connections":
        return LeastConnectionsDispatcher(calibration=calib, seed=seed + 3000)
    if name == "power-of-2":
        return PowerOfTwoChoicesDispatcher(calibration=calib, seed=seed + 2000)
    if name == "ewma-lrt":
        return EWMALeastResponseTimeDispatcher(calibration=calib, seed=seed + 4000)
    if name == "meraf-no-cost-diff":
        # P axis off: every request is costed as PLAIN; admission/weights unchanged.
        return MERAFDispatcher(calibration=calib, weights=ScoreWeights(lam=1.2, eta=0.6, gamma=0.25),
                                  candidates_d=candidates_d, differentiate_cost=False, seed=seed + 1000)
    if name == "meraf-prob-slo":
        # SLO term = P(latency > SLO) under a Gaussian model instead of the unbounded L95/SLO.
        return MERAFDispatcher(calibration=calib, weights=ScoreWeights(lam=1.2, eta=0.6, gamma=0.25),
                                  candidates_d=candidates_d, slo_penalty_mode="probability", seed=seed + 1000)
    if name in ("meraf-eta0", "meraf-prob-slo-eta0", "meraf-security-blind"):
        # eta=0 drops the additive sigma term (the only place the T axis enters
        # linear scoring); "-prob-slo-eta0" does the same on the probability
        # variant; "-security-blind" also disables P. Together with "meraf" and
        # "meraf-no-cost-diff" these complete the 2x2 T x P factorial.
        weights = ScoreWeights(lam=1.2, eta=0.0, gamma=0.25)
        mode = "probability" if name == "meraf-prob-slo-eta0" else "linear"
        differentiate_cost = name != "meraf-security-blind"
        return MERAFDispatcher(calibration=calib, weights=weights,
                                  candidates_d=candidates_d, slo_penalty_mode=mode,
                                  differentiate_cost=differentiate_cost, seed=seed + 1000)
    if name == "security-constraint-loadaware":
        # Static security-tier eligibility filter, then least-loaded (Singh & Auluck 2020 style).
        return SecurityConstraintLeastLoadedDispatcher(calibration=calib, seed=seed + 5000)
    if name == "security-constraint-satisfaction":
        # Static eligibility filter, then lowest predicted p95 (Wang et al. 2026 style).
        return SecurityConstraintFastestDispatcher(calibration=calib, seed=seed + 6000)
    raise ValueError(name)


ALGORITHMS = ["meraf", "meraf-no-cost-diff", "meraf-prob-slo",
              "meraf-eta0", "meraf-prob-slo-eta0", "meraf-security-blind",
              "round-robin", "least-connections", "power-of-2", "ewma-lrt",
              "security-constraint-loadaware", "security-constraint-satisfaction"]


def run_one(scenario: str, algo_name: str, rate_per_ms: float, horizon_ms: float,
            calib: CostCalibration, seed: int, outdir: str, save_completed: bool = True,
            n_tenants: int = 50, slo_ms: float = 2000.0, n_backends: int = 4,
            candidates_d: int = 6):
    """Run one replica; returns (RunSummary, per-profile breakdown, dispatcher)."""
    backends = make_backend_fleet(scenario, n_backends=n_backends, slo_ms=slo_ms)
    dispatcher = build_dispatcher(algo_name, calib, seed=seed, candidates_d=candidates_d)
    piano_a = PianoA()
    # Independent tenants: with a single shared tenant, MERAF's session-affinity
    # bonus pins the whole simulation to one backend.
    tenants = [f"tenant{i}" for i in range(n_tenants)]
    gen = WorkloadGenerator(rate_per_ms=rate_per_ms, seed=seed, tenants=tenants)
    requests = gen.generate(horizon_ms)

    sim = Simulator(backends, dispatcher, piano_a)
    sim.run(requests)

    summary = summarize(sim, total_requests=len(requests), horizon_ms=horizon_ms,
                         algorithm_name=algo_name, slo_ms=slo_ms)
    breakdown = per_profile_breakdown(sim)

    if save_completed:
        fname = os.path.join(
            outdir, f"completed_{scenario}_{algo_name}_{rate_per_ms:.3f}_seed{seed}.csv")
        write_completed_csv(fname, sim)

    return summary, breakdown, dispatcher


def _aggregate(summaries):
    """Aggregate the per-seed RunSummary objects of one (algorithm, load) cell.

    Mean, sample std (ddof=1), Student-t CI95, min and max come from
    stats_utils.mean_std_ci95, shared with the offline pipeline. Offered
    percentiles are averaged only over seeds where they are reachable and
    the count is reported as `<field>_n_reachable`.
    """
    fields = ["throughput_rps", "goodput_rps", "latency_mean_ms", "latency_p50_ms",
              "latency_p95_ms", "latency_p99_ms", "reject_rate_pct"]
    out = {"algorithm": summaries[0].algorithm, "n_seeds": len(summaries),
           "total_requests": summaries[0].total_requests,
           "horizon_ms": summaries[0].horizon_ms,
           "reject_penalty_ms": summaries[0].reject_penalty_ms}
    for f in fields:
        vals = [getattr(s, f) for s in summaries]
        r = mean_std_ci95(vals)
        out[f"{f}_mean"] = r.mean
        out[f"{f}_std"] = r.std
        out[f"{f}_ci95"] = r.ci95
        out[f"{f}_min"] = r.min
        out[f"{f}_max"] = r.max

    # Reachability is decided per seed: averaging saturated (penalty) values
    # together with real ones would describe no actual system state.
    for p_name in ("p95", "p99"):
        value_field = f"latency_{p_name}_offered_ms"
        reach_field = f"latency_{p_name}_offered_reachable"
        reachable_vals = [getattr(s, value_field) for s in summaries if getattr(s, reach_field)]
        n_reachable = len(reachable_vals)
        out[f"{value_field}_n_reachable"] = n_reachable
        if n_reachable > 0:
            r = mean_std_ci95(reachable_vals)
            out[f"{value_field}_mean"] = r.mean
            out[f"{value_field}_std"] = r.std
            out[f"{value_field}_ci95"] = r.ci95
            out[f"{value_field}_min"] = r.min
            out[f"{value_field}_max"] = r.max
        else:
            out[f"{value_field}_mean"] = float("nan")
            out[f"{value_field}_std"] = float("nan")
            out[f"{value_field}_ci95"] = float("nan")
            out[f"{value_field}_min"] = float("nan")
            out[f"{value_field}_max"] = float("nan")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon-ms", type=float, default=30000.0,
                     help="Simulated duration in ms (default: 30s)")
    ap.add_argument("--loads", type=float, nargs="+", default=[0.02, 0.06, 0.12, 0.16],
                     help="Arrival rates in requests/ms (defaults calibrated "
                          "to stay around/above the network bandwidth ceiling)")
    ap.add_argument("--scenarios", nargs="+", default=["locale", "rete"])
    ap.add_argument("--outdir", default="MERAF/results/outputs")
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5],
                     help="Independent seeds for the replicas (default: 5 replicas)")
    ap.add_argument("--no-save-completed", action="store_true",
                     help="Do not save the per-request log of each replica "
                          "(reduces disk usage for long runs)")
    ap.add_argument("--tenants", type=int, default=50,
                     help="Number of independent simulated tenants/clients "
                          "(default 50; with a single tenant MERAF's session "
                          "affinity pins the whole simulation to one "
                          "backend, see README)")
    ap.add_argument("--n-backends", type=int, default=4,
                     help="Size of the simulated fleet (default 4, as in the "
                          "paper). With --candidates-d < --n-backends, MERAF "
                          "actually samples a subset of the nodes (power-of-d-"
                          "choices exercised) instead of scanning the whole fleet "
                          "on every request -- see Sec. 3.3.3 of the review, "
                          "which notes that d=6 on a fleet of 4 makes the "
                          "sampling inert. Example: --n-backends 50 "
                          "--candidates-d 6.")
    ap.add_argument("--candidates-d", type=int, default=6,
                     help="Number of candidate nodes sampled by MERAF on "
                          "every request (default 6, unchanged from the "
                          "previous version so as not to alter the results already "
                          "reported with the default 4-node fleet)")
    ap.add_argument("--no-latex", action="store_true",
                     help="Do not generate the .tex tables automatically (by "
                          "default they are written right after the CSVs, see "
                          "make_latex_tables.py)")
    ap.add_argument("--profile-table-algo", default="meraf",
                     help="Algorithm for which to generate the automatic per-profile "
                          "(T,P) table, one per load (default: meraf, "
                          "consistent with Table 6/7 of the paper)")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    calib = CostCalibration()

    for scenario in args.scenarios:
        raw_rows = []          # one row per (algo, load, seed)
        aggregated_rows = []   # one row per (algo, load), aggregated over seeds
        breakdown_by_algo = {}

        print(f"\n===== SCENARIO: {scenario} =====")
        for load in args.loads:
            for algo in ALGORITHMS:
                per_seed_summaries = []
                for seed in args.seeds:
                    summary, breakdown, dispatcher = run_one(
                        scenario, algo, load, args.horizon_ms, calib, seed,
                        args.outdir, save_completed=not args.no_save_completed,
                        n_tenants=args.tenants, n_backends=args.n_backends,
                        candidates_d=args.candidates_d)
                    per_seed_summaries.append(summary)
                    raw_rows.append(dict(algorithm=algo, load=load, seed=seed,
                                          **{k: v for k, v in summary.__dict__.items()
                                             if k != "algorithm"}))
                    breakdown_by_algo[f"{algo}@{load}@seed{seed}"] = breakdown

                agg = _aggregate(per_seed_summaries)
                agg["load"] = load
                aggregated_rows.append(agg)
                print(f"{algo:<20} load={load:<7.3f} "
                      f"RPS={agg['throughput_rps_mean']:.2f}+/-{agg['throughput_rps_std']:.2f}  "
                      f"p50={agg['latency_p50_ms_mean']:.1f}+/-{agg['latency_p50_ms_std']:.1f}  "
                      f"p99={agg['latency_p99_ms_mean']:.1f}+/-{agg['latency_p99_ms_std']:.1f}  "
                      f"rejects%={agg['reject_rate_pct_mean']:.2f}+/-{agg['reject_rate_pct_std']:.2f}")

        raw_file = os.path.join(args.outdir, f"summary_{scenario}_raw_perseed.csv")
        if raw_rows:
            with open(raw_file, "w", newline="") as f:
                import csv
                writer = csv.DictWriter(f, fieldnames=list(raw_rows[0].keys()))
                writer.writeheader()
                writer.writerows(raw_rows)

        agg_file = os.path.join(args.outdir, f"summary_{scenario}_aggregated.csv")
        if aggregated_rows:
            with open(agg_file, "w", newline="") as f:
                import csv
                writer = csv.DictWriter(f, fieldnames=list(aggregated_rows[0].keys()))
                writer.writeheader()
                writer.writerows(aggregated_rows)

        pfile = os.path.join(args.outdir, f"profile_breakdown_{scenario}.csv")
        write_profile_breakdown_csv(pfile, breakdown_by_algo)

        print(f"\n[saved] {raw_file}")
        print(f"[saved] {agg_file}")
        print(f"[saved] {pfile}")

        if not args.no_latex:
            # make_latex_tables.py can still regenerate these with other algo/load.
            agg_by_key = {(row["algorithm"], row["load"]): row for row in aggregated_rows}
            tex_file = os.path.join(args.outdir, f"summary_{scenario}_table.tex")
            write_main_comparison_table(tex_file, scenario, agg_by_key)

            for load in args.loads:
                generate_profile_breakdown_table(pfile, args.profile_table_algo, load)

    print("\nDone. All CSVs (and the .tex tables, unless --no-latex) are in",
          os.path.abspath(args.outdir))


if __name__ == "__main__":
    sys.exit(main())
