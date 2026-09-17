#!/usr/bin/env python3
"""
Workflow (task-DAG) extension of run_experiments.py. Compares MERAF's
per-task admission ("meraf") with per-workflow admission
("meraf-workflow-admission": the whole DAG is accepted or rejected at
submission and its capacity is reserved for its lifetime), the
"meraf-workflow-deadline" ablation, and the baselines (run per-task by the
simulator). Reports completion rate, makespan, goodput (workflows within
deadline per second) and wasted work per (scenario, topology, profile
mode, load, seed). Cells are flushed to raw_perseed.csv as soon as they
are computed and skipped on rerun (--restart recomputes). The run fails if
"meraf" and "meraf-workflow-admission" coincide on more than one
non-trivial cell (stats_utils.assert_variants_differ).

Outputs (in --outdir):
  workflow_summary_<scenario>_<topology>_<profile_mode>_{raw_perseed,aggregated}.csv
  workflows_<scenario>_<topology>_<profile_mode>_<algo>_<load>_seed<N>.csv  (unless --no-save-records)

Usage:
    python3 -m MERAF.experiments.run_workflow_experiments \\
        --topologies linear fan_out_in --loads 0.02 0.06 0.12 0.16 \\
        --scenarios locale rete --seeds 1 2 3 4 5
    python3 -m MERAF.experiments.run_workflow_experiments --profile-mode random ...
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import time
from typing import Dict, List, Tuple

from ..core.dispatchers import (MERAFDispatcher, RoundRobinDispatcher,
                                   LeastConnectionsDispatcher,
                                   PowerOfTwoChoicesDispatcher,
                                   EWMALeastResponseTimeDispatcher,
                                   SecurityConstraintLeastLoadedDispatcher,
                                   SecurityConstraintFastestDispatcher)
from ..core.model import CostCalibration, ScoreWeights
from ..core.piano_a import PianoA
from ..core.simulator import Simulator
from ..core.workload import WorkflowGenerator, WorkflowTopology
from ..core.metrics import (summarize_workflows, write_workflow_records_csv)
from ..core.stats_utils import mean_std_ci95, assert_variants_differ, SuspiciousIdenticalResultsError
from .run_experiments import make_backend_fleet


def build_dispatcher(name: str, calib: CostCalibration, seed: int = 0, candidates_d: int = 6):
    """Instantiate the dispatcher called `name` (see ALGORITHMS); seed offsets match run_experiments.py."""
    if name == "meraf":
        return MERAFDispatcher(calibration=calib, weights=ScoreWeights(lam=1.2, eta=0.6, gamma=0.25),
                                  candidates_d=candidates_d, seed=seed + 1000)
    if name == "meraf-workflow-admission":
        # Whole-DAG admission on resource risk; admitted capacity stays reserved until release.
        return MERAFDispatcher(calibration=calib, weights=ScoreWeights(lam=1.2, eta=0.6, gamma=0.25),
                                  candidates_d=candidates_d, workflow_admission=True, seed=seed + 1000)
    if name == "meraf-workflow-deadline":
        # Ablation: same per-DAG decision point, estimated-makespan-vs-deadline criterion, no reservation.
        return MERAFDispatcher(calibration=calib, weights=ScoreWeights(lam=1.2, eta=0.6, gamma=0.25),
                                  candidates_d=candidates_d, workflow_admission=True,
                                  admission_criterion="deadline", seed=seed + 1000)
    if name == "meraf-no-cost-diff":
        # P axis off: every task is costed as PLAIN.
        return MERAFDispatcher(calibration=calib, weights=ScoreWeights(lam=1.2, eta=0.6, gamma=0.25),
                                  candidates_d=candidates_d, differentiate_cost=False, seed=seed + 1000)
    if name == "round-robin":
        return RoundRobinDispatcher(calibration=calib)
    if name == "least-connections":
        return LeastConnectionsDispatcher(calibration=calib, seed=seed + 3000)
    if name == "power-of-2":
        return PowerOfTwoChoicesDispatcher(calibration=calib, seed=seed + 2000)
    if name == "ewma-lrt":
        return EWMALeastResponseTimeDispatcher(calibration=calib, seed=seed + 4000)
    # Security-aware baselines have no workflow notion, so the simulator runs
    # them per-task; their per-task rejections fail the workflow like MERAF's.
    if name == "security-constraint-loadaware":
        return SecurityConstraintLeastLoadedDispatcher(calibration=calib, seed=seed + 5000)
    if name == "security-constraint-satisfaction":
        return SecurityConstraintFastestDispatcher(calibration=calib, seed=seed + 6000)
    raise ValueError(name)


ALGORITHMS = ["meraf", "meraf-workflow-admission", "meraf-workflow-deadline",
              "meraf-no-cost-diff",
              "round-robin", "least-connections", "power-of-2", "ewma-lrt",
              "security-constraint-loadaware", "security-constraint-satisfaction"]


def run_one(scenario: str, algo_name: str, topology: str, rate_per_ms: float,
            horizon_ms: float, calib: CostCalibration, seed: int, outdir: str,
            n_tenants: int, profile_mode: str, n_stages: int, fan_width: int,
            save_records: bool):
    """Run one replica and return its WorkflowSummary."""
    backends = make_backend_fleet(scenario)
    dispatcher = build_dispatcher(algo_name, calib, seed=seed)
    piano_a = PianoA()
    tenants = [f"tenant{i}" for i in range(n_tenants)]
    gen = WorkflowGenerator(rate_per_ms=rate_per_ms, topology=topology, n_stages=n_stages,
                             fan_width=fan_width, profile_mode=profile_mode,
                             tenants=tenants, seed=seed)
    workflows = gen.generate(horizon_ms)

    sim = Simulator(backends, dispatcher, piano_a)
    sim.run_workflows(workflows)

    if getattr(dispatcher, "workflow_admission", False):
        admission_mode = ("per-workflow-deadline"
                           if getattr(dispatcher, "admission_criterion", "risk") == "deadline"
                           else "per-workflow")
    else:
        admission_mode = "per-task"
    summary = summarize_workflows(sim, algo_name, topology, admission_mode, horizon_ms=horizon_ms)

    if save_records:
        fname = os.path.join(
            outdir, f"workflows_{scenario}_{topology}_{profile_mode}_{algo_name}"
                    f"_{rate_per_ms:.3f}_seed{seed}.csv")
        write_workflow_records_csv(fname, sim.completed_workflows)

    return summary


def _aggregate(rows: List[dict]) -> dict:
    """Aggregate the per-seed rows (dicts, fresh or reloaded from CSV) of one cell.

    Same mean/sample-std/CI95 as run_experiments._aggregate. NaN values (e.g.
    makespan for a seed that completed no workflow under overload) are
    dropped per field and the count is kept as `<field>_n_valid`;
    statistics.stdev would otherwise raise on them.
    """
    fields = ["completion_rate_pct", "makespan_mean_ms", "makespan_p50_ms",
              "makespan_p95_ms", "makespan_p99_ms", "goodput_workflows_per_sec",
              "within_deadline_pct", "wasted_cpu_ms_total", "wasted_bytes_total"]
    out = {"algorithm": rows[0]["algorithm"], "topology": rows[0]["topology"],
           "admission_mode": rows[0]["admission_mode"], "n_seeds": len(rows),
           "total_workflows_mean": sum(r["total_workflows"] for r in rows) / len(rows)}
    for f in fields:
        vals = [r[f] for r in rows]
        valid_vals = [v for v in vals if not math.isnan(v)]
        out[f"{f}_n_valid"] = len(valid_vals)
        if valid_vals:
            r = mean_std_ci95(valid_vals)
            out[f"{f}_mean"] = r.mean
            out[f"{f}_std"] = r.std
            out[f"{f}_ci95"] = r.ci95
        else:
            out[f"{f}_mean"] = float("nan")
            out[f"{f}_std"] = float("nan")
            out[f"{f}_ci95"] = float("nan")
    return out


# Columns kept as strings when reloading raw_perseed.csv; all others are numeric.
_STR_FIELDS = {"algorithm", "topology", "admission_mode"}


def _cast_row(row: Dict[str, str]) -> dict:
    """Cast a raw_perseed.csv row (all strings) back to the types produced by run_one."""
    out = {}
    for k, v in row.items():
        if k in _STR_FIELDS:
            out[k] = v
        elif k == "seed":
            out[k] = int(float(v))
        else:
            out[k] = float(v)
    return out


def _load_existing_cells(raw_file: str) -> Dict[Tuple[str, float, int], dict]:
    """Return the (algorithm, load, seed) cells already in `raw_file`, or {} if absent/empty."""
    if not os.path.exists(raw_file) or os.path.getsize(raw_file) == 0:
        return {}
    cells = {}
    with open(raw_file, newline="") as f:
        for row in csv.DictReader(f):
            row = _cast_row(row)
            cells[(row["algorithm"], row["load"], row["seed"])] = row
    return cells


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horizon-ms", type=float, default=30000.0,
                     help="Simulated duration in ms (default: 30s, as in run_experiments.py)")
    ap.add_argument("--loads", type=float, nargs="+", default=[0.02, 0.06, 0.12, 0.16],
                     help="WORKFLOW arrival rates in workflows/ms (the same 4 "
                          "load intensities already used for independent requests)")
    ap.add_argument("--scenarios", nargs="+", default=["locale", "rete"])
    ap.add_argument("--topologies", nargs="+", default=[WorkflowTopology.LINEAR,
                                                          WorkflowTopology.FAN_OUT_IN],
                     choices=[WorkflowTopology.LINEAR, WorkflowTopology.FAN_OUT_IN,
                              WorkflowTopology.MONTAGE_LIKE])
    ap.add_argument("--n-stages", type=int, default=5,
                     help="Number of stages for the 'linear' topology (default 5)")
    ap.add_argument("--fan-width", type=int, default=4,
                     help="Number of parallel tasks for the 'fan_out_in' topology (default 4)")
    ap.add_argument("--profile-mode", default="by_stage", choices=["by_stage", "random"],
                     help="'by_stage' (default): (T,P) profile correlated with the stage "
                          "(ingest=DECRYPT, intermediate=OPAQUE, final=PLAIN) -- the hypothesis "
                          "under test. 'random': uniformly random profile per task, as in "
                          "WorkloadGenerator -- the control that isolates its effect.")
    ap.add_argument("--algorithms", nargs="+", default=ALGORITHMS, choices=ALGORITHMS)
    ap.add_argument("--outdir", default="MERAF/results/outputs_wf")
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    ap.add_argument("--tenants", type=int, default=50)
    ap.add_argument("--no-save-records", action="store_true",
                     help="Do not save the per-workflow log of each replica")
    ap.add_argument("--restart", action="store_true",
                     help="Ignore any raw_perseed.csv already present in --outdir and "
                          "recompute everything from scratch (default: resume, skipping the "
                          "scenario/topology/algorithm/load/seed cells already present)")
    ap.add_argument("--duty-cycle", type=float, default=1.0,
                     help="Fraction of time the process actively works "
                          "(0<d<=1, default 1.0 = no limit). E.g. 0.5: after each "
                          "cell it sleeps as long as it just worked, keeping average CPU "
                          "at 50%% -- useful to keep the machine from overheating on "
                          "long runs, without depending on cpulimit/cgroups.")
    args = ap.parse_args()
    if not (0.0 < args.duty_cycle <= 1.0):
        ap.error("--duty-cycle must be in (0, 1]")

    os.makedirs(args.outdir, exist_ok=True)
    calib = CostCalibration()

    # Mean completion rate per (scenario, topology, load) for the end-of-run
    per_task_cells = {}
    per_workflow_cells = {}
    per_deadline_cells = {}
    check_admission_modes = ("meraf" in args.algorithms
                              and "meraf-workflow-admission" in args.algorithms)
    # The deadline variant is an ablation ("no difference" is a legitimate
    # outcome), so it is collected for logging only, not asserted.
    collect_deadline = "meraf-workflow-deadline" in args.algorithms

    for scenario in args.scenarios:
        for topology in args.topologies:
            tag = f"{scenario}_{topology}_{args.profile_mode}"
            raw_file = os.path.join(args.outdir, f"workflow_summary_{tag}_raw_perseed.csv")

            # Resume: skip cells already present in raw_perseed.csv unless --restart.
            existing_cells = {} if args.restart else _load_existing_cells(raw_file)
            if existing_cells:
                print(f"\n[resume] {len(existing_cells)} cells already present in "
                      f"{raw_file}, not recomputed")

            need_header = not existing_cells  # absent/empty file or --restart
            raw_mode = "w" if (args.restart or need_header) else "a"
            raw_f = open(raw_file, raw_mode, newline="")
            raw_writer = None  # created on the first new row (needs the column names)

            rows_by_cell: Dict[Tuple[str, float, int], dict] = dict(existing_cells)
            aggregated_rows = []

            print(f"\n===== SCENARIO: {scenario}  TOPOLOGY: {topology}  "
                  f"PROFILE: {args.profile_mode} =====")
            for load in args.loads:
                for algo in args.algorithms:
                    per_seed_rows = []
                    for seed in args.seeds:
                        cell_key = (algo, load, seed)
                        if cell_key in existing_cells:
                            per_seed_rows.append(existing_cells[cell_key])
                            continue

                        t0 = time.perf_counter()
                        summary = run_one(
                            scenario, algo, topology, load, args.horizon_ms, calib, seed,
                            args.outdir, args.tenants, args.profile_mode, args.n_stages,
                            args.fan_width, save_records=not args.no_save_records)
                        if args.duty_cycle < 1.0:
                            # Sleep in proportion to the work just done so average
                            # CPU stays at duty_cycle (no cpulimit/cgroups needed).
                            elapsed = time.perf_counter() - t0
                            time.sleep(elapsed * (1.0 - args.duty_cycle) / args.duty_cycle)
                        row = dict(algorithm=algo, load=load, seed=seed,
                                   **{k: v for k, v in summary.__dict__.items()
                                      if k not in ("algorithm",)})
                        per_seed_rows.append(row)
                        rows_by_cell[cell_key] = row

                        # Flush per cell so an interruption mid-block loses no finished work.
                        if raw_writer is None:
                            raw_writer = csv.DictWriter(raw_f, fieldnames=list(row.keys()))
                            if need_header:
                                raw_writer.writeheader()
                        raw_writer.writerow(row)
                        raw_f.flush()

                    agg = _aggregate(per_seed_rows)
                    agg["load"] = load
                    aggregated_rows.append(agg)
                    print(f"{algo:<28} load={load:<7.3f} "
                          f"completion={agg['completion_rate_pct_mean']:6.2f}%"
                          f"+/-{agg['completion_rate_pct_ci95']:5.2f}  "
                          f"goodput={agg['goodput_workflows_per_sec_mean']:7.3f}wf/s  "
                          f"makespan_p95={agg['makespan_p95_ms_mean']:9.1f}ms  "
                          f"wasted_cpu={agg['wasted_cpu_ms_total_mean']:9.1f}ms")

                    if check_admission_modes:
                        key = (scenario, topology, load)
                        if algo == "meraf":
                            per_task_cells[key] = agg["completion_rate_pct_mean"]
                        elif algo == "meraf-workflow-admission":
                            per_workflow_cells[key] = agg["completion_rate_pct_mean"]
                    if collect_deadline and algo == "meraf-workflow-deadline":
                        per_deadline_cells[(scenario, topology, load)] = agg["completion_rate_pct_mean"]

            raw_f.close()

            agg_file = os.path.join(args.outdir, f"workflow_summary_{tag}_aggregated.csv")
            if aggregated_rows:
                with open(agg_file, "w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=list(aggregated_rows[0].keys()))
                    writer.writeheader()
                    writer.writerows(aggregated_rows)

            print(f"\n[saved] {raw_file}")
            print(f"[saved] {agg_file}")

    if check_admission_modes:
        # Fail instead of letting identical-admission results be analysed.
        assert_variants_differ(per_task_cells, per_workflow_cells,
                                "meraf (per-task)", "meraf-workflow-admission (per-workflow)")
        print("\n[check] meraf and meraf-workflow-admission diverge "
              "as expected on all non-trivial cells.")

    print("\nDone. All CSVs are in", os.path.abspath(args.outdir))


if __name__ == "__main__":
    sys.exit(main())
