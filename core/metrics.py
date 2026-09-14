"""
Aggregation of simulation-run metrics (throughput, latency percentiles,
reject rate, offered-load percentiles, workflow goodput) and CSV export.
"""
from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass, asdict
from typing import Dict, List

from .simulator import Simulator, CompletedRecord, WorkflowRecord
from .workload import Request

# Real end-to-end client timeout of the reference HTTP benchmark client.
# Default penalty for rejected requests in the offered-load percentiles.
CLIENT_TIMEOUT_MS = 60_000.0


def _percentile(values: List[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


@dataclass
class RunSummary:
    algorithm: str
    total_requests: int
    completed: int
    rejected: int
    reject_rate_pct: float
    horizon_ms: float
    throughput_rps: float
    latency_mean_ms: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    queueing_p50_ms: float
    queueing_p95_ms: float
    # Offered-load percentiles: rejected requests count as reject_penalty_ms.
    # A declared convention, always report alongside reject_rate_pct.
    # Reachability is evaluated per seed (see offered_percentile_reachable).
    reject_penalty_ms: float
    latency_p95_offered_ms: float
    latency_p99_offered_ms: float
    latency_p95_offered_reachable: bool
    latency_p99_offered_reachable: bool
    goodput_rps: float  # completions within SLO per second (not total RPS)


def summarize(sim: Simulator, total_requests: int, horizon_ms: float,
              algorithm_name: str, slo_ms: float = 2000.0,
              reject_penalty_ms: float = CLIENT_TIMEOUT_MS) -> RunSummary:
    completed = sim.completed
    n_completed = len(completed)
    n_rejected = len(sim.rejected)
    totals = [c.total_ms for c in completed]
    queues = [c.queueing_ms for c in completed]

    throughput_rps = n_completed / (horizon_ms / 1000.0) if horizon_ms > 0 else 0.0
    n_within_slo = sum(1 for t in totals if t <= slo_ms)
    goodput_rps = n_within_slo / (horizon_ms / 1000.0) if horizon_ms > 0 else 0.0

    offered_totals = totals + [reject_penalty_ms] * n_rejected
    reject_rate_pct = 100.0 * n_rejected / total_requests if total_requests else 0.0

    return RunSummary(
        algorithm=algorithm_name,
        total_requests=total_requests,
        completed=n_completed,
        rejected=n_rejected,
        reject_rate_pct=reject_rate_pct,
        horizon_ms=horizon_ms,
        throughput_rps=throughput_rps,
        latency_mean_ms=statistics.mean(totals) if totals else float("nan"),
        latency_p50_ms=_percentile(totals, 0.50),
        latency_p95_ms=_percentile(totals, 0.95),
        latency_p99_ms=_percentile(totals, 0.99),
        queueing_p50_ms=_percentile(queues, 0.50),
        queueing_p95_ms=_percentile(queues, 0.95),
        reject_penalty_ms=reject_penalty_ms,
        latency_p95_offered_ms=_percentile(offered_totals, 0.95),
        latency_p99_offered_ms=_percentile(offered_totals, 0.99),
        latency_p95_offered_reachable=offered_percentile_reachable(reject_rate_pct, 0.95),
        latency_p99_offered_reachable=offered_percentile_reachable(reject_rate_pct, 0.99),
        goodput_rps=goodput_rps,
    )


def offered_percentile_reachable(reject_rate_pct: float, p: float) -> bool:
    """True if percentile p of the offered distribution falls within admitted requests.

    Rejected requests are censored as "worse than anything observed", so the
    percentile is reachable only when reject fraction < (1 - p).
    """
    r = reject_rate_pct / 100.0
    return r < (1.0 - p)


def per_profile_breakdown(sim: Simulator) -> Dict[str, RunSummary]:
    """Latency metrics disaggregated by security profile."""
    by_profile: Dict[str, List[CompletedRecord]] = {}
    for c in sim.completed:
        by_profile.setdefault(c.profile, []).append(c)

    out = {}
    for profile, records in by_profile.items():
        totals = [r.total_ms for r in records]
        out[profile] = dict(
            n=len(records),
            latency_mean_ms=statistics.mean(totals) if totals else float("nan"),
            latency_p50_ms=_percentile(totals, 0.50),
            latency_p95_ms=_percentile(totals, 0.95),
            latency_p99_ms=_percentile(totals, 0.99),
        )
    return out


def write_summaries_csv(path: str, summaries: List[RunSummary]) -> None:
    if not summaries:
        return
    fieldnames = list(asdict(summaries[0]).keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for s in summaries:
            writer.writerow(asdict(s))


def write_completed_csv(path: str, sim: Simulator) -> None:
    fieldnames = ["req_id", "profile", "tenant", "backend", "arrival_ms",
                  "dispatch_ms", "completion_ms", "queueing_ms", "service_ms",
                  "total_ms", "workflow_id"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for c in sim.completed:
            writer.writerow(asdict(c))


@dataclass
class WorkflowSummary:
    """Workflow-level summary: completion rate, makespan, goodput, wasted work.

    Wasted-work fields are a diagnostic of per-task admission, not evidence
    for per-workflow admission (which zeroes waste by construction).
    """
    algorithm: str
    topology: str
    admission_mode: str          # "per-task" or "per-workflow"
    total_workflows: int
    completed_workflows: int
    failed_workflows: int
    rejected_at_admission: int   # subset of failed, per-workflow mode only
    completion_rate_pct: float
    makespan_mean_ms: float
    makespan_p50_ms: float
    makespan_p95_ms: float
    makespan_p99_ms: float
    goodput_workflows_per_sec: float      # completed AND makespan <= depth * slo_ms
    within_deadline_pct: float            # % of completed that also met the deadline
    wasted_cpu_ms_total: float
    wasted_bytes_total: float
    wasted_cpu_ms_mean_per_failed: float  # 0 if no failed workflows


def summarize_workflows(sim: Simulator, algorithm_name: str, topology: str,
                         admission_mode: str, horizon_ms: float,
                         slo_ms: float = 2000.0) -> WorkflowSummary:
    records = sim.completed_workflows
    total = len(records)
    completed = [r for r in records if r.completed]
    failed = [r for r in records if not r.completed]
    rejected_at_admission = sum(1 for r in failed if r.rejected_at_admission)

    makespans = [r.makespan_ms for r in completed]
    wasted_cpu_total = sum(r.wasted_cpu_ms for r in failed)
    wasted_bytes_total = sum(r.wasted_bytes for r in failed)

    # End-to-end deadline scales with DAG depth: each level must meet the per-task SLO.
    within_deadline = [r for r in completed if r.makespan_ms <= r.depth * slo_ms]
    goodput_per_sec = len(within_deadline) / (horizon_ms / 1000.0) if horizon_ms > 0 else 0.0
    within_deadline_pct = 100.0 * len(within_deadline) / len(completed) if completed else 0.0

    return WorkflowSummary(
        algorithm=algorithm_name,
        topology=topology,
        admission_mode=admission_mode,
        total_workflows=total,
        completed_workflows=len(completed),
        failed_workflows=len(failed),
        rejected_at_admission=rejected_at_admission,
        completion_rate_pct=100.0 * len(completed) / total if total else 0.0,
        makespan_mean_ms=statistics.mean(makespans) if makespans else float("nan"),
        makespan_p50_ms=_percentile(makespans, 0.50),
        makespan_p95_ms=_percentile(makespans, 0.95),
        makespan_p99_ms=_percentile(makespans, 0.99),
        goodput_workflows_per_sec=goodput_per_sec,
        within_deadline_pct=within_deadline_pct,
        wasted_cpu_ms_total=wasted_cpu_total,
        wasted_bytes_total=wasted_bytes_total,
        wasted_cpu_ms_mean_per_failed=(wasted_cpu_total / len(failed)) if failed else 0.0,
    )


def write_workflow_summaries_csv(path: str, summaries: List[WorkflowSummary]) -> None:
    if not summaries:
        return
    fieldnames = list(asdict(summaries[0]).keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for s in summaries:
            writer.writerow(asdict(s))


def write_workflow_records_csv(path: str, records: List[WorkflowRecord]) -> None:
    """Per-workflow (non-aggregated) log, for offline recomputation of metrics."""
    if not records:
        return
    fieldnames = list(asdict(records[0]).keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            writer.writerow(asdict(r))


def write_profile_breakdown_csv(path: str, breakdown_by_algo: Dict[str, Dict[str, dict]]) -> None:
    """breakdown_by_algo: {algo_name: {profile_name: metrics_dict}}"""
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["algorithm", "profile", "n", "latency_mean_ms",
                          "latency_p50_ms", "latency_p95_ms", "latency_p99_ms"])
        for algo, profiles in breakdown_by_algo.items():
            for profile, m in profiles.items():
                writer.writerow([algo, profile, m["n"], f"{m['latency_mean_ms']:.3f}",
                                  f"{m['latency_p50_ms']:.3f}", f"{m['latency_p95_ms']:.3f}",
                                  f"{m['latency_p99_ms']:.3f}"])
