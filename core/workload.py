"""
Request generators for the simulator: Poisson arrivals with a configurable
(T,P) mix and payload size (WorkloadGenerator), plus WorkflowGenerator, which
emits DAGs of dependent tasks instead of independent requests.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

from .model import SecurityProfile, Transport, Payload


@dataclass
class Request:
    """A single request, or one task of a workflow."""
    req_id: int
    arrival_ms: float
    profile: SecurityProfile
    payload_bytes: int
    use_kem_envelope: bool     # encrypted payload wrapped in an ML-KEM envelope
    tenant: str = "default"
    route: str = "default"
    # Workflow membership (None/empty for independent requests). The simulator
    # releases a task only once all `predecessors` have completed.
    workflow_id: Optional[int] = None
    predecessors: Tuple[int, ...] = ()
    successors: Tuple[int, ...] = ()
    # Set when the parent workflow passed workflow-level admission: dispatchers
    # with per-workflow admission must not re-reject its tasks on projected risk.
    workflow_admitted: bool = False

    @property
    def cls_key(self) -> str:
        """Class key for adaptive learning: route|size-bucket|P|tenant.
        For workflow tasks `route` is the stage name."""
        size_bucket = "small" if self.payload_bytes < 512 * 1024 else (
            "medium" if self.payload_bytes < 4 * 1024 * 1024 else "large")
        return f"{self.route}|{size_bucket}|{self.profile.payload.value}|{self.tenant}"


class WorkloadGenerator:
    """Poisson arrivals with a configurable (T,P) mix, mirroring the five
    configurations benchmarked in the paper plus optional OPAQUE."""

    DEFAULT_MIX = {
        SecurityProfile(Transport.CLASSIC, Payload.PLAIN): 0.20,
        SecurityProfile(Transport.CLASSIC, Payload.DECRYPT): 0.20,
        SecurityProfile(Transport.PQC, Payload.PLAIN): 0.20,
        SecurityProfile(Transport.PQC, Payload.DECRYPT): 0.20,
        SecurityProfile(Transport.CLASSIC, Payload.OPAQUE): 0.20,
    }

    def __init__(self, rate_per_ms: float, payload_bytes: int = 5 * 1024 * 1024,
                 mix: Optional[Dict[SecurityProfile, float]] = None,
                 kem_envelope_fraction: float = 0.5,
                 tenants: Optional[List[str]] = None,
                 seed: int = 42):
        self.rate_per_ms = rate_per_ms
        self.payload_bytes = payload_bytes
        self.mix = mix or self.DEFAULT_MIX
        self.kem_envelope_fraction = kem_envelope_fraction
        self.tenants = tenants or ["default"]
        self.rng = random.Random(seed)
        self._profiles = list(self.mix.keys())
        self._weights = list(self.mix.values())
        self._next_id = 0

    def generate(self, horizon_ms: float) -> List[Request]:
        """Generate all requests arriving within horizon_ms (homogeneous Poisson process)."""
        requests = []
        t = 0.0
        while True:
            t += self.rng.expovariate(self.rate_per_ms)
            if t > horizon_ms:
                break
            profile = self.rng.choices(self._profiles, weights=self._weights, k=1)[0]
            use_kem = (profile.payload == Payload.DECRYPT and
                       self.rng.random() < self.kem_envelope_fraction)
            tenant = self.rng.choice(self.tenants)
            requests.append(Request(
                req_id=self._next_id,
                arrival_ms=t,
                profile=profile,
                payload_bytes=self.payload_bytes,
                use_kem_envelope=use_kem,
                tenant=tenant,
            ))
            self._next_id += 1
        return requests


# ---- Workflows: DAGs of dependent tasks ----
# Tasks are ordinary Request objects (same cost model, same dispatcher) linked
# via predecessors/successors; the simulator gates release on the predecessors.

class WorkflowTopology(str, Enum):
    LINEAR = "linear"          # stage0 -> stage1 -> ... -> stageN-1
    FAN_OUT_IN = "fan_out_in"  # ingest -> W parallel tasks -> aggregate
    MONTAGE_LIKE = "montage_like"  # W project -> concatfit -> bgmodel -> W background -> mosaic


@dataclass
class Workflow:
    workflow_id: int
    arrival_ms: float
    tenant: str
    topology: str
    tasks: List[Request] = field(default_factory=list)  # topological order, sources first

    @property
    def task_by_id(self) -> Dict[int, Request]:
        return {t.req_id: t for t in self.tasks}


class WorkflowGenerator:
    """Poisson-arriving workflows (task DAGs) with the (T,P) profile assigned
    per stage: first stage DECRYPT, last stage PLAIN, intermediate stages
    OPAQUE. profile_mode="random" draws P uniformly per task instead, as the
    control for structured-vs-uniform mix comparisons."""

    def __init__(self, rate_per_ms: float,
                 topology: str = WorkflowTopology.LINEAR,
                 n_stages: int = 3,
                 fan_width: int = 4,
                 payload_bytes: int = 5 * 1024 * 1024,
                 profile_mode: str = "by_stage",
                 transport_mix: Optional[Dict[Transport, float]] = None,
                 kem_envelope_fraction: float = 0.5,
                 tenants: Optional[List[str]] = None,
                 seed: int = 42):
        if profile_mode not in ("by_stage", "random"):
            raise ValueError(f"unknown profile_mode: {profile_mode!r}")
        self.rate_per_ms = rate_per_ms
        self.topology = topology
        self.n_stages = n_stages
        self.fan_width = fan_width
        self.payload_bytes = payload_bytes
        self.profile_mode = profile_mode
        self.transport_mix = transport_mix or {Transport.CLASSIC: 0.5, Transport.PQC: 0.5}
        self.kem_envelope_fraction = kem_envelope_fraction
        self.tenants = tenants or ["default"]
        self.rng = random.Random(seed)
        self._transports = list(self.transport_mix.keys())
        self._transport_weights = list(self.transport_mix.values())
        self._next_wf_id = 0
        self._next_task_id = 0

    def _alloc_task_id(self) -> int:
        tid = self._next_task_id
        self._next_task_id += 1
        return tid

    def _random_transport(self) -> Transport:
        return self.rng.choices(self._transports, weights=self._transport_weights, k=1)[0]

    def _payload_for(self, stage_index: int, n_total_stages: int) -> Payload:
        if self.profile_mode == "random":
            return self.rng.choice([Payload.PLAIN, Payload.DECRYPT, Payload.OPAQUE])
        if stage_index == 0:
            return Payload.DECRYPT
        if stage_index == n_total_stages - 1:
            return Payload.PLAIN
        return Payload.OPAQUE

    def _make_task(self, task_id: int, arrival_ms: float, tenant: str, route: str,
                    stage_index: int, n_total_stages: int, workflow_id: int,
                    predecessors: List[int], successors: List[int]) -> Request:
        payload = self._payload_for(stage_index, n_total_stages)
        transport = self._random_transport()
        profile = SecurityProfile(transport, payload)
        use_kem = (payload == Payload.DECRYPT and self.rng.random() < self.kem_envelope_fraction)
        return Request(
            req_id=task_id, arrival_ms=arrival_ms, profile=profile,
            payload_bytes=self.payload_bytes, use_kem_envelope=use_kem,
            tenant=tenant, route=route, workflow_id=workflow_id,
            predecessors=tuple(predecessors), successors=tuple(successors),
        )

    def _build_linear(self, wf_id: int, arrival_ms: float, tenant: str) -> List[Request]:
        n = self.n_stages
        ids = [self._alloc_task_id() for _ in range(n)]
        tasks = []
        for i in range(n):
            preds = [ids[i - 1]] if i > 0 else []
            succs = [ids[i + 1]] if i < n - 1 else []
            tasks.append(self._make_task(ids[i], arrival_ms, tenant, f"stage{i}",
                                          i, n, wf_id, preds, succs))
        return tasks

    def _build_fan_out_in(self, wf_id: int, arrival_ms: float, tenant: str) -> List[Request]:
        ingest_id = self._alloc_task_id()
        mid_ids = [self._alloc_task_id() for _ in range(self.fan_width)]
        agg_id = self._alloc_task_id()
        tasks = [self._make_task(ingest_id, arrival_ms, tenant, "ingest",
                                  0, 3, wf_id, [], mid_ids)]
        for mid_id in mid_ids:
            tasks.append(self._make_task(mid_id, arrival_ms, tenant, "transfer",
                                          1, 3, wf_id, [ingest_id], [agg_id]))
        tasks.append(self._make_task(agg_id, arrival_ms, tenant, "aggregate",
                                      2, 3, wf_id, mid_ids, []))
        return tasks

    def _build_montage_like(self, wf_id: int, arrival_ms: float, tenant: str) -> List[Request]:
        """Simplified Montage DAG: W project -> concatfit -> bgmodel -> W background
        -> mosaic (5 levels, 2W+3 tasks, peak width W as in fan-out/fan-in)."""
        W = self.fan_width
        n_levels = 5
        project_ids = [self._alloc_task_id() for _ in range(W)]
        concat_id = self._alloc_task_id()
        bgmodel_id = self._alloc_task_id()
        background_ids = [self._alloc_task_id() for _ in range(W)]
        mosaic_id = self._alloc_task_id()
        tasks = []
        for pid in project_ids:
            tasks.append(self._make_task(pid, arrival_ms, tenant, "project",
                                          0, n_levels, wf_id, [], [concat_id]))
        tasks.append(self._make_task(concat_id, arrival_ms, tenant, "concatfit",
                                      1, n_levels, wf_id, project_ids, [bgmodel_id]))
        tasks.append(self._make_task(bgmodel_id, arrival_ms, tenant, "bgmodel",
                                      2, n_levels, wf_id, [concat_id], background_ids))
        for bid in background_ids:
            tasks.append(self._make_task(bid, arrival_ms, tenant, "background",
                                          3, n_levels, wf_id, [bgmodel_id], [mosaic_id]))
        tasks.append(self._make_task(mosaic_id, arrival_ms, tenant, "mosaic",
                                      4, n_levels, wf_id, background_ids, []))
        return tasks

    def generate(self, horizon_ms: float) -> List[Workflow]:
        """Generate all workflows arriving within horizon_ms. Arrivals are Poisson
        per workflow; all its tasks share arrival_ms and dependencies gate release."""
        workflows = []
        t = 0.0
        while True:
            t += self.rng.expovariate(self.rate_per_ms)
            if t > horizon_ms:
                break
            tenant = self.rng.choice(self.tenants)
            wf_id = self._next_wf_id
            self._next_wf_id += 1
            if self.topology == WorkflowTopology.LINEAR:
                tasks = self._build_linear(wf_id, t, tenant)
            elif self.topology == WorkflowTopology.FAN_OUT_IN:
                tasks = self._build_fan_out_in(wf_id, t, tenant)
            elif self.topology == WorkflowTopology.MONTAGE_LIKE:
                tasks = self._build_montage_like(wf_id, t, tenant)
            else:
                raise ValueError(self.topology)
            workflows.append(Workflow(workflow_id=wf_id, arrival_ms=t, tenant=tenant,
                                       topology=self.topology, tasks=tasks))
        return workflows
