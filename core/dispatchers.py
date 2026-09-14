"""
Request dispatchers: the MERAF cost-aware dispatcher (Algorithm 1, OnRequest)
and the baseline dispatchers used for comparison.

All dispatchers share one interface so the simulator can use them interchangeably:
select_backend(req, backends, now_ms, piano_a) -> DispatchResult and
on_completion(req, backend, latency_ms, now_ms) -> None.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .backend import Backend
from .model import (SecurityProfile, Transport, Payload, CostCalibration,
                     resource_set, dominant_risk, score, ScoreWeights,
                     required_security_tier, required_trust_label_singh_auluck,
                     TRUST_HIGHLY_TRUSTED, TRUST_SEMI_TRUSTED, prob_exceed_slo)
from .piano_a import PianoA
from .workload import Request


# The paper's "cpu(io)" (OPAQUE relay I/O) is modelled as work on the shared cpu resource.
def _sim_resource_name(r: str) -> str:
    return "cpu" if r == "cpu(io)" else r


@dataclass
class DispatchResult:
    backend: Optional[Backend]
    cost: Optional[Dict[str, float]] = None
    resources: Tuple[str, ...] = ()


class MERAFDispatcher:
    """MERAF Plan B: cost-aware selection, dominant-resource-risk admission, conditional affinity bonus."""

    def __init__(self, calibration: Optional[CostCalibration] = None,
                 weights: Optional[ScoreWeights] = None,
                 candidates_d: int = 3,
                 sticky_tolerance: float = 1.10,
                 slo_ms: float = 2000.0,
                 stale_after_ms: float = 10_000.0,
                 handshake_sigma_multiplier: float = 1.5,
                 differentiate_cost: bool = True,
                 slo_penalty_mode: str = "linear",
                 workflow_admission: bool = False,
                 admission_criterion: str = "risk",
                 seed: int = 7):
        if admission_criterion not in ("risk", "deadline"):
            raise ValueError(f"unknown admission_criterion: {admission_criterion!r}")
        # "deadline" is an ablation: per-DAG decision on estimated makespan, no capacity reserve.
        self.admission_criterion = admission_criterion
        self.calib = calibration or CostCalibration()
        self.weights = weights or ScoreWeights()
        self.d = candidates_d
        self.sticky_tolerance = sticky_tolerance
        self.slo_ms = slo_ms
        # Max age (sim ms) of a per-class EWMA before falling back to the structural estimate.
        self.stale_after_ms = stale_after_ms
        # Sigma inflation during a full PQC handshake; exposed for sensitivity sweeps.
        self.handshake_sigma_multiplier = handshake_sigma_multiplier
        # Ablation: False prices every request as PLAIN, leaving admission/scoring untouched.
        self.differentiate_cost = differentiate_cost
        # "probability" replaces the unbounded L95/SLO term with P(latency > SLO) in [0, 1].
        if slo_penalty_mode not in ("linear", "probability"):
            raise ValueError(f"unknown slo_penalty_mode: {slo_penalty_mode!r} "
                              f"(expected: 'linear' or 'probability')")
        self.slo_penalty_mode = slo_penalty_mode
        # If True the simulator calls admit_workflow() per DAG before dispatching any task.
        self.workflow_admission = workflow_admission
        self.rng = random.Random(seed)
        self._sticky: Dict[str, str] = {}  # client_id -> backend name
        self.rejections = 0
        self.dispatched = 0
        self.workflows_rejected_at_admission = 0
        # Capacity reserved by admitted, still-running workflows (keyed by backend name), so
        # back-to-back admissions cannot both be granted against the same free capacity.
        self._reserved_width: Dict[str, int] = {}
        self._reserved_mem_mb: Dict[str, float] = {}
        self._workflow_reservation: Dict[int, Tuple[str, int, float]] = {}

    def admit_workflow(self, workflow, backends: List[Backend], now_ms: float) -> bool:
        """Admit or reject a whole DAG at submission time (always True unless workflow_admission).

        Tasks are grouped by topological level; the workflow is admitted if one healthy
        backend can absorb the widest level (plus capacity already reserved for other
        running workflows) with rho < 1 and enough memory. That capacity stays reserved
        until release_workflow(). Rationale and limits: docs/core-dispatchers.md.
        """
        if not self.workflow_admission:
            return True

        healthy = [b for b in backends if b.healthy]
        if not healthy:
            self.workflows_rejected_at_admission += 1
            return False

        levels: Dict[int, List[Request]] = {}
        level_of: Dict[int, int] = {}
        for task in workflow.tasks:  # already in topological order
            lvl = 0 if not task.predecessors else 1 + max(level_of[p] for p in task.predecessors)
            level_of[task.req_id] = lvl
            levels.setdefault(lvl, []).append(task)

        if self.admission_criterion == "deadline":
            # Deadline-gate ablation: estimated makespan (sum over levels of the best
            # per-task L95) vs. depth*SLO; no capacity is reserved.
            est_makespan = 0.0
            for lvl in sorted(levels):
                level_est = 0.0
                for task in levels[lvl]:
                    cost, _resources = self._cost_profile(task)
                    best = float("inf")
                    for b in healthy:
                        default_l95 = cost["cpu_ms"] + cost["bytes"] / max(
                            b.spec.net_capacity_bytes_per_ms, 1e-9)
                        if b.is_stale(task.cls_key, now_ms, self.stale_after_ms):
                            est = default_l95
                        else:
                            est = b.predicted_l95(task.cls_key, default_l95)
                        best = min(best, est)
                    level_est = max(level_est, best)
                est_makespan += level_est
            if est_makespan <= len(levels) * self.slo_ms:
                return True
            self.workflows_rejected_at_admission += 1
            return False

        width = max(len(ts) for ts in levels.values())
        resources_union: set = set()
        peak_mem_mb = 0.0
        for ts in levels.values():
            level_mem_mb = 0.0
            for task in ts:
                cost, resources = self._cost_profile(task)
                level_mem_mb += cost["mem_mb"]
                resources_union.update(resources)
            peak_mem_mb = max(peak_mem_mb, level_mem_mb)

        for b in healthy:
            reserved_mem = self._reserved_mem_mb.get(b.spec.name, 0.0)
            if b.mem_used_mb + reserved_mem + peak_mem_mb > b.spec.mem_capacity_mb:
                continue  # not enough memory, reservations included
            reserved_w = self._reserved_width.get(b.spec.name, 0)
            proj = b.projected_utilization(tuple(resources_union), extra=width + reserved_w)
            rho = dominant_risk(proj, b.thresholds(), tuple(resources_union))
            if rho < 1.0:
                self._reserved_width[b.spec.name] = reserved_w + width
                self._reserved_mem_mb[b.spec.name] = reserved_mem + peak_mem_mb
                self._workflow_reservation[workflow.workflow_id] = (b.spec.name, width, peak_mem_mb)
                return True

        self.workflows_rejected_at_admission += 1
        return False

    def release_workflow(self, workflow_id: int) -> None:
        """Release the capacity reserved by admit_workflow() once a workflow completes or fails."""
        entry = self._workflow_reservation.pop(workflow_id, None)
        if entry is None:
            return
        backend_name, width, mem_mb = entry
        self._reserved_width[backend_name] = max(0, self._reserved_width.get(backend_name, 0) - width)
        self._reserved_mem_mb[backend_name] = max(
            0.0, self._reserved_mem_mb.get(backend_name, 0.0) - mem_mb)

    def _cost_profile(self, req: Request) -> Tuple[Dict[str, float], Tuple[str, ...]]:
        p = req.profile.payload
        if not self.differentiate_cost:
            # Ablation: price and resource set as if the request were PLAIN.
            return self.calib.cost_plain(req.payload_bytes), resource_set(Payload.PLAIN)
        resources = resource_set(p)
        if p == Payload.PLAIN:
            cost = self.calib.cost_plain(req.payload_bytes)
        elif p == Payload.DECRYPT:
            cost = self.calib.cost_decrypt(req.payload_bytes, req.use_kem_envelope)
        elif p == Payload.OPAQUE:
            cost = self.calib.cost_opaque(req.payload_bytes)
        else:
            raise ValueError(p)
        return cost, resources

    def _score_candidates(self, candidates, req, cost, resources, is_full_handshake,
                           sticky_name, now_ms):
        best_backend = None
        best_score = float("inf")
        scored: Dict[str, float] = {}

        # An admitted workflow already assumed the projected risk; only the hard
        # memory limit still applies to its tasks.
        skip_risk_gate = self.workflow_admission and req.workflow_admitted

        for b in candidates:
            if not b.has_mem_headroom(cost["mem_mb"]):
                continue  # hard memory limit, never bypassed
            proj = b.projected_utilization(resources, extra=1)
            rho = dominant_risk(proj, b.thresholds(), resources)
            if rho >= 1.0 and not skip_risk_gate:
                continue  # rho_i(r,P) >= 1

            cls = req.cls_key
            default_l95 = cost["cpu_ms"] + cost["bytes"] / max(b.spec.net_capacity_bytes_per_ms, 1e-9)
            if b.is_stale(cls, now_ms, self.stale_after_ms):
                # Stale or absent telemetry: structural estimate, as on first use of the class.
                mean = default_l95
                l95 = default_l95
                sigma = default_l95 * 0.5
            else:
                mean = b.predicted_mean(cls, default_l95)
                l95 = b.predicted_l95(cls, default_l95)
                sigma = b.predicted_sigma(cls, default_l95)
            if is_full_handshake and req.profile.transport == Transport.PQC:
                sigma *= self.handshake_sigma_multiplier

            affinity = 1.0 if b.spec.name == sticky_name else 0.0
            if self.slo_penalty_mode == "probability":
                latency_term = prob_exceed_slo(mean, sigma, self.slo_ms)
            else:
                latency_term = None  # score() defaults to L95/SLO
            s = score(l95, self.slo_ms, rho, sigma, affinity, self.weights,
                      latency_term=latency_term)
            scored[b.spec.name] = s
            if s < best_score:
                best_score = s
                best_backend = b
        return best_backend, best_score, scored

    def select_backend(self, req: Request, backends: List[Backend], now_ms: float,
                        piano_a: PianoA) -> DispatchResult:
        cost, resources = self._cost_profile(req)
        is_full_handshake = piano_a.on_connection(req.tenant, req.profile.transport, now_ms)

        healthy = [b for b in backends if b.healthy]
        if not healthy:
            self.rejections += 1
            return DispatchResult(None)

        candidates = self.rng.sample(healthy, k=min(self.d, len(healthy)))

        client_id = req.tenant
        sticky_name = self._sticky.get(client_id)

        best_backend, best_score, scored = self._score_candidates(
            candidates, req, cost, resources, is_full_handshake, sticky_name, now_ms)

        if best_backend is None and len(candidates) < len(healthy):
            # Before rejecting, also score the healthy nodes left out of the d-sample.
            remaining = [b for b in healthy if b not in candidates]
            best_backend, best_score, scored2 = self._score_candidates(
                remaining, req, cost, resources, is_full_handshake, sticky_name, now_ms)
            scored.update(scored2)
            candidates = candidates + remaining

        if best_backend is None:
            self.rejections += 1
            return DispatchResult(None)

        # Affinity bonus: keep the sticky backend if within tolerance of the best score.
        if sticky_name is not None:
            sticky_backend = next((b for b in candidates if b.spec.name == sticky_name), None)
            if sticky_backend is not None and sticky_name in scored:
                if scored[sticky_name] <= best_score * self.sticky_tolerance:
                    best_backend = sticky_backend

        self._sticky[client_id] = best_backend.spec.name
        best_backend.mem_used_mb += cost["mem_mb"]
        self.dispatched += 1
        return DispatchResult(best_backend, cost, resources)

    def on_completion(self, req: Request, backend: Backend, latency_ms: float,
                       now_ms: float) -> None:
        backend.observe_latency(req.cls_key, latency_ms, now_ms)


# --------------------------------------------------------------------------
# Baseline dispatchers used for comparison
# --------------------------------------------------------------------------

class RoundRobinDispatcher:
    """Round robin over healthy backends; ignores T and P."""

    def __init__(self, calibration: Optional[CostCalibration] = None):
        self.calib = calibration or CostCalibration()
        self._idx = 0
        self.rejections = 0
        self.dispatched = 0

    def _cost_profile(self, req: Request):
        p = req.profile.payload
        resources = resource_set(p)
        if p == Payload.PLAIN:
            cost = self.calib.cost_plain(req.payload_bytes)
        elif p == Payload.DECRYPT:
            cost = self.calib.cost_decrypt(req.payload_bytes, req.use_kem_envelope)
        else:
            cost = self.calib.cost_opaque(req.payload_bytes)
        return cost, resources

    def select_backend(self, req, backends, now_ms, piano_a) -> DispatchResult:
        piano_a.on_connection(req.tenant, req.profile.transport, now_ms)
        healthy = [b for b in backends if b.healthy]
        if not healthy:
            self.rejections += 1
            return DispatchResult(None)
        b = healthy[self._idx % len(healthy)]
        self._idx += 1
        cost, resources = self._cost_profile(req)
        if not b.has_mem_headroom(cost["mem_mb"]):
            self.rejections += 1
            return DispatchResult(None)
        b.mem_used_mb += cost["mem_mb"]
        self.dispatched += 1
        return DispatchResult(b, cost, resources)

    def on_completion(self, req, backend, latency_ms, now_ms) -> None:
        pass


class LeastConnectionsDispatcher(RoundRobinDispatcher):
    """Backend with the fewest active flows (cpu+net+decrypt); the cost profile only sizes memory/bytes."""

    def __init__(self, calibration: Optional[CostCalibration] = None, seed: int = 13):
        super().__init__(calibration)
        # Random tie-break: min() alone would pin all traffic to the first backend.
        self.rng = random.Random(seed)

    def select_backend(self, req, backends, now_ms, piano_a) -> DispatchResult:
        piano_a.on_connection(req.tenant, req.profile.transport, now_ms)
        healthy = [b for b in backends if b.healthy]
        if not healthy:
            self.rejections += 1
            return DispatchResult(None)

        def total_load(b: Backend) -> int:
            return b.cpu.active_count + b.net.active_count + b.decrypt.active_count

        min_load = min(total_load(b) for b in healthy)
        tied = [b for b in healthy if total_load(b) == min_load]
        b = self.rng.choice(tied)
        cost, resources = self._cost_profile(req)
        if not b.has_mem_headroom(cost["mem_mb"]):
            self.rejections += 1
            return DispatchResult(None)
        b.mem_used_mb += cost["mem_mb"]
        self.dispatched += 1
        return DispatchResult(b, cost, resources)


class PowerOfTwoChoicesDispatcher(RoundRobinDispatcher):
    """Sample two backends at random and pick the one with fewer active flows."""

    def __init__(self, calibration: Optional[CostCalibration] = None, seed: int = 11):
        super().__init__(calibration)
        self.rng = random.Random(seed)

    def select_backend(self, req, backends, now_ms, piano_a) -> DispatchResult:
        piano_a.on_connection(req.tenant, req.profile.transport, now_ms)
        healthy = [b for b in backends if b.healthy]
        if not healthy:
            self.rejections += 1
            return DispatchResult(None)
        sample = self.rng.sample(healthy, k=min(2, len(healthy)))

        def total_load(b: Backend) -> int:
            return b.cpu.active_count + b.net.active_count + b.decrypt.active_count

        # random tie-break, as in LeastConnectionsDispatcher
        min_load = min(total_load(b) for b in sample)
        tied = [b for b in sample if total_load(b) == min_load]
        b = self.rng.choice(tied)
        cost, resources = self._cost_profile(req)
        if not b.has_mem_headroom(cost["mem_mb"]):
            self.rejections += 1
            return DispatchResult(None)
        b.mem_used_mb += cost["mem_mb"]
        self.dispatched += 1
        return DispatchResult(b, cost, resources)


class EWMALeastResponseTimeDispatcher(RoundRobinDispatcher):
    """Backend with the lowest predicted p95 for one undifferentiated class (latency-aware, not crypto-aware)."""

    def __init__(self, calibration: Optional[CostCalibration] = None, seed: int = 17):
        super().__init__(calibration)
        self.rng = random.Random(seed)

    def select_backend(self, req, backends, now_ms, piano_a) -> DispatchResult:
        piano_a.on_connection(req.tenant, req.profile.transport, now_ms)
        healthy = [b for b in backends if b.healthy]
        if not healthy:
            self.rejections += 1
            return DispatchResult(None)
        cost, resources = self._cost_profile(req)
        # Cold-start estimate uses each node's real bandwidth, not a fixed constant.
        def est_l95(x: Backend) -> float:
            return x.predicted_l95(
                "generic", cost["cpu_ms"] + cost["bytes"] / max(x.spec.net_capacity_bytes_per_ms, 1e-9))

        # random tie-break: homogeneous nodes share the same cold estimate
        best_est = min(est_l95(x) for x in healthy)
        tied = [x for x in healthy if est_l95(x) == best_est]
        b = self.rng.choice(tied)
        if not b.has_mem_headroom(cost["mem_mb"]):
            self.rejections += 1
            return DispatchResult(None)
        b.mem_used_mb += cost["mem_mb"]
        self.dispatched += 1
        return DispatchResult(b, cost, resources)

    def on_completion(self, req, backend, latency_ms, now_ms) -> None:
        backend.observe_latency("generic", latency_ms, now_ms)


# --------------------------------------------------------------------------
# "Security-as-constraint" baselines: security is a static eligibility
# requirement, not a scoring variable; requests with no eligible node are dropped.
# --------------------------------------------------------------------------

class SecurityConstraintLeastLoadedDispatcher(RoundRobinDispatcher):
    """Minimum Load (ML) algorithm of Singh & Auluck 2020 [22].

    Each tenant is pinned to a fixed "local" node. Requests are labelled
    lp/lsp/lpu (Table 2) and the label decides which nodes are eligible; among
    eligible nodes with rho < 1 the local one is preferred, otherwise minimum rho.
    Deviation from the paper: an lp request whose local node is saturated is
    dropped instead of queued.
    """

    def __init__(self, calibration: Optional[CostCalibration] = None, seed: int = 19):
        super().__init__(calibration)
        self.rng = random.Random(seed)
        self._local: Dict[str, str] = {}  # tenant -> fixed local mdc name

    def select_backend(self, req, backends, now_ms, piano_a) -> DispatchResult:
        piano_a.on_connection(req.tenant, req.profile.transport, now_ms)
        healthy = [b for b in backends if b.healthy]
        if not healthy:
            self.rejections += 1
            return DispatchResult(None)

        client_id = req.tenant
        local_name = self._local.get(client_id)
        if local_name is None:
            local_name = self.rng.choice(healthy).spec.name
            self._local[client_id] = local_name
        local = next((b for b in healthy if b.spec.name == local_name), None)
        if local is None:
            # Local node unhealthy: the paper defines no fallback.
            self.rejections += 1
            return DispatchResult(None)

        required_label = required_trust_label_singh_auluck(req.profile.payload)
        cost, resources = self._cost_profile(req)

        def rho_of(b: Backend) -> float:
            proj = b.projected_utilization(resources, extra=1)
            return dominant_risk(proj, b.thresholds(), resources)

        if required_label == TRUST_HIGHLY_TRUSTED:
            candidates = [local]  # lp: never migrated
        elif required_label == TRUST_SEMI_TRUSTED:
            candidates = [b for b in healthy if b.spec.security_tier >= TRUST_SEMI_TRUSTED]
        else:
            candidates = healthy  # lpu: any node

        with_room = [b for b in candidates if rho_of(b) < 1.0]
        if not with_room:
            self.rejections += 1
            return DispatchResult(None)

        if local in with_room:
            chosen = local
        else:
            min_rho = min(rho_of(b) for b in with_room)
            tied = [b for b in with_room if rho_of(b) == min_rho]
            chosen = self.rng.choice(tied)

        if not chosen.has_mem_headroom(cost["mem_mb"]):
            self.rejections += 1
            return DispatchResult(None)
        chosen.mem_used_mb += cost["mem_mb"]
        self.dispatched += 1
        return DispatchResult(chosen, cost, resources)


class SecurityConstraintFastestDispatcher(RoundRobinDispatcher):
    """Online adaptation of SeSiC, Wang et al. 2026 [24].

    Requests without a security requirement try the public pool
    (security_tier < 1) first; secured requests, or requests the public pool
    cannot serve within the SLO, go to the private pool. A node is eligible if
    predicted_l95 <= slo_ms and rho < 1; ties go to minimum rho. Requests with
    no eligible node are dropped. Procedure 3 (cost re-scheduling) and
    public-cloud elasticity are omitted.
    """

    def __init__(self, calibration: Optional[CostCalibration] = None,
                 slo_ms: float = 2000.0, seed: int = 23):
        super().__init__(calibration)
        self.slo_ms = slo_ms
        self.rng = random.Random(seed)

    def select_backend(self, req, backends, now_ms, piano_a) -> DispatchResult:
        piano_a.on_connection(req.tenant, req.profile.transport, now_ms)
        healthy = [b for b in backends if b.healthy]
        if not healthy:
            self.rejections += 1
            return DispatchResult(None)

        cost, resources = self._cost_profile(req)
        security_required = required_security_tier(req.profile) >= 1
        public_pool = [b for b in healthy if b.spec.security_tier < 1]
        private_pool = [b for b in healthy if b.spec.security_tier >= 1]

        def rho_of(b: Backend) -> float:
            proj = b.projected_utilization(resources, extra=1)
            return dominant_risk(proj, b.thresholds(), resources)

        def meets_deadline(b: Backend) -> bool:
            default_l95 = cost["cpu_ms"] + cost["bytes"] / max(b.spec.net_capacity_bytes_per_ms, 1e-9)
            predicted = b.predicted_l95(req.cls_key, default_l95)
            return predicted <= self.slo_ms and rho_of(b) < 1.0

        def pick_best(pool):
            candidates = [b for b in pool if meets_deadline(b)]
            if not candidates:
                return None
            min_rho = min(rho_of(b) for b in candidates)
            tied = [b for b in candidates if rho_of(b) == min_rho]
            return self.rng.choice(tied)

        chosen = None
        if not security_required:
            chosen = pick_best(public_pool)   # Procedure 1: public pool first
        if chosen is None:
            chosen = pick_best(private_pool)  # Procedure 2: private pool

        if chosen is None:
            self.rejections += 1
            return DispatchResult(None)

        if not chosen.has_mem_headroom(cost["mem_mb"]):
            self.rejections += 1
            return DispatchResult(None)
        chosen.mem_used_mb += cost["mem_mb"]
        self.dispatched += 1
        return DispatchResult(chosen, cost, resources)

    def on_completion(self, req, backend, latency_ms, now_ms) -> None:
        backend.observe_latency(req.cls_key, latency_ms, now_ms)
