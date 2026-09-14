"""
Flow-level discrete-event simulator.

Each shared resource (cpu, net, decrypt) is a dynamic fair-share
SharedResource; completion events are invalidated via per-resource
generation counters whenever the active flow set changes.
"""
from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .backend import Backend
from .piano_a import PianoA
from .workload import Request, Workflow


@dataclass
class RequestState:
    req: Request
    backend: Backend
    cost: Dict[str, float]
    resources_pending: set
    arrival_ms: float
    dispatch_ms: float


@dataclass
class CompletedRecord:
    req_id: int
    profile: str
    tenant: str
    backend: str
    arrival_ms: float
    dispatch_ms: float
    completion_ms: float
    queueing_ms: float          # dispatch_ms - arrival_ms
    service_ms: float           # completion_ms - dispatch_ms
    total_ms: float             # completion_ms - arrival_ms
    workflow_id: Optional[int] = None  # None for independent requests


@dataclass
class WorkflowRecord:
    """Final outcome of a workflow, recorded once by Simulator._maybe_finalize_workflow()."""
    workflow_id: int
    tenant: str
    topology: str
    n_tasks: int
    depth: int  # number of topological levels (critical path in tasks, not ms)
    arrival_ms: float
    completed: bool
    completion_ms: Optional[float] = None
    makespan_ms: Optional[float] = None
    tasks_completed: int = 0
    tasks_rejected: int = 0
    tasks_unreachable: int = 0
    # Work already done by completed tasks of a failed workflow; 0 if completed
    # or rejected at admission.
    wasted_cpu_ms: float = 0.0
    wasted_bytes: float = 0.0
    rejected_at_admission: bool = False


def _dag_depth(tasks: List[Request]) -> int:
    """Number of topological levels of the DAG; requires `tasks` in topological order."""
    level_of: Dict[int, int] = {}
    for t in tasks:
        level_of[t.req_id] = 0 if not t.predecessors else 1 + max(level_of[p] for p in t.predecessors)
    return (max(level_of.values()) + 1) if level_of else 1


@dataclass
class _WorkflowState:
    """Internal bookkeeping for an in-progress workflow."""
    workflow: Workflow
    task_by_id: Dict[int, Request]
    pending_predecessors: Dict[int, int]
    depth: int
    completed_task_ids: Set[int] = field(default_factory=set)
    rejected_task_ids: Set[int] = field(default_factory=set)
    unreachable_task_ids: Set[int] = field(default_factory=set)
    wasted_cost: List[Dict[str, float]] = field(default_factory=list)
    finalized: bool = False
    rejected_at_admission: bool = False


class Simulator:
    def __init__(self, backends: List[Backend], dispatcher, piano_a: Optional[PianoA] = None):
        self.backends = backends
        self.backend_by_name = {b.spec.name: b for b in backends}
        self.dispatcher = dispatcher
        self.piano_a = piano_a or PianoA()

        self._heap: List[Tuple[float, int, str, object]] = []
        self._seq = itertools.count()
        self._resource_gen: Dict[Tuple[str, str], int] = {}

        self.in_flight: Dict[int, RequestState] = {}
        self.completed: List[CompletedRecord] = []
        self.rejected: List[Request] = []

        # Only used by run_workflows(); empty for run().
        self._workflow_states: Dict[int, _WorkflowState] = {}
        self.completed_workflows: List[WorkflowRecord] = []

    # ---- event queue ----

    def _push(self, time_ms: float, event_type: str, data: object) -> None:
        heapq.heappush(self._heap, (time_ms, next(self._seq), event_type, data))

    def _gen(self, backend_name: str, resource_name: str) -> int:
        return self._resource_gen.get((backend_name, resource_name), 0)

    def _bump_gen(self, backend_name: str, resource_name: str) -> int:
        key = (backend_name, resource_name)
        self._resource_gen[key] = self._resource_gen.get(key, 0) + 1
        return self._resource_gen[key]

    def _resource_obj(self, backend: Backend, resource_name: str):
        return getattr(backend, resource_name)

    def _schedule_next_completion(self, backend: Backend, resource_name: str, now_ms: float) -> None:
        resource = self._resource_obj(backend, resource_name)
        nxt = resource.next_completion(now_ms)
        if nxt is None:
            return
        flow_id, t = nxt
        gen = self._gen(backend.spec.name, resource_name)
        self._push(t, "resource_complete", (backend.spec.name, resource_name, flow_id, gen))

    def _add_flow(self, backend: Backend, resource_name: str, flow_id: int,
                  work_units: float, now_ms: float) -> None:
        resource = self._resource_obj(backend, resource_name)
        resource.add_flow(now_ms, flow_id, work_units)
        self._bump_gen(backend.spec.name, resource_name)
        self._schedule_next_completion(backend, resource_name, now_ms)

    def _remove_flow(self, backend: Backend, resource_name: str, flow_id: int,
                      now_ms: float) -> None:
        resource = self._resource_obj(backend, resource_name)
        resource.remove_flow(now_ms, flow_id)
        self._bump_gen(backend.spec.name, resource_name)
        self._schedule_next_completion(backend, resource_name, now_ms)

    # ---- event handlers ----

    def _handle_arrival(self, req: Request, now_ms: float) -> None:
        result = self.dispatcher.select_backend(req, self.backends, now_ms, self.piano_a)
        if result.backend is None:
            self.rejected.append(req)
            if req.workflow_id is not None:
                self._on_workflow_task_rejected(req, now_ms)
            return

        backend = result.backend
        rtt = backend.spec.base_rtt_ms
        if rtt > 0:
            # Routing is decided now; data starts flowing after the RTT.
            self._push(now_ms + rtt, "begin_service",
                       (req, backend, result.cost, result.resources, now_ms))
        else:
            self._begin_service(req, backend, result.cost, result.resources, now_ms, now_ms)

    def _begin_service(self, req: Request, backend: Backend, cost: Dict[str, float],
                        resources: tuple, dispatch_ms: float, now_ms: float) -> None:
        pending = set()
        if "net" in resources:
            self._add_flow(backend, "net", req.req_id, cost["bytes"], now_ms)
            pending.add("net")
        if "cpu" in resources or "cpu(io)" in resources:
            cpu_work = cost["cpu_ms"]
            if "decrypt_ms" in cost:
                # DECRYPT class: crypto work goes to the dedicated resource, cpu covers the rest.
                cpu_work = max(cost["cpu_ms"] - cost["decrypt_ms"], 1e-6)
            self._add_flow(backend, "cpu", req.req_id, cpu_work, now_ms)
            pending.add("cpu")
        if "decrypt" in resources and "decrypt_ms" in cost and cost["decrypt_ms"] > 0:
            self._add_flow(backend, "decrypt", req.req_id, cost["decrypt_ms"], now_ms)
            pending.add("decrypt")

        state = RequestState(req=req, backend=backend, cost=cost,
                              resources_pending=pending, arrival_ms=req.arrival_ms,
                              dispatch_ms=dispatch_ms)
        self.in_flight[req.req_id] = state

        if not pending:
            # Zero-cost request: completes immediately.
            self._finish_request(state, now_ms)

    def _handle_resource_complete(self, data: tuple, now_ms: float) -> None:
        backend_name, resource_name, flow_id, gen = data
        if gen != self._gen(backend_name, resource_name):
            return  # stale event: flow set changed since it was scheduled
        backend = self.backend_by_name[backend_name]
        self._remove_flow(backend, resource_name, flow_id, now_ms)

        state = self.in_flight.get(flow_id)
        if state is None:
            return
        state.resources_pending.discard(resource_name)
        if not state.resources_pending:
            self._finish_request(state, now_ms)

    def _finish_request(self, state: RequestState, now_ms: float) -> None:
        state.backend.mem_used_mb = max(0.0, state.backend.mem_used_mb - state.cost["mem_mb"])
        latency_ms = now_ms - state.dispatch_ms
        total_ms = now_ms - state.arrival_ms
        self.dispatcher.on_completion(state.req, state.backend, latency_ms, now_ms)
        self.completed.append(CompletedRecord(
            req_id=state.req.req_id,
            profile=str(state.req.profile),
            tenant=state.req.tenant,
            backend=state.backend.spec.name,
            arrival_ms=state.arrival_ms,
            dispatch_ms=state.dispatch_ms,
            completion_ms=now_ms,
            queueing_ms=state.dispatch_ms - state.arrival_ms,
            service_ms=latency_ms,
            total_ms=total_ms,
            workflow_id=state.req.workflow_id,
        ))
        del self.in_flight[state.req.req_id]
        if state.req.workflow_id is not None:
            self._on_workflow_task_completed(state, now_ms)

    # ---- main loop (independent requests) ----

    def run(self, requests: List[Request]) -> None:
        for r in requests:
            self._push(r.arrival_ms, "arrival", r)

        while self._heap:
            time_ms, _, event_type, data = heapq.heappop(self._heap)
            if event_type == "arrival":
                self._handle_arrival(data, time_ms)
            elif event_type == "begin_service":
                req, backend, cost, resources, dispatch_ms = data
                self._begin_service(req, backend, cost, resources, dispatch_ms, time_ms)
            elif event_type == "resource_complete":
                self._handle_resource_complete(data, time_ms)
            else:
                raise ValueError(event_type)

    # ---- main loop (workflows) ----

    def run_workflows(self, workflows: List[Workflow]) -> None:
        """Like run(), but for DAGs of dependent tasks; supports optional per-workflow admission."""
        for wf in workflows:
            self._push(wf.arrival_ms, "workflow_arrival", wf)

        while self._heap:
            time_ms, _, event_type, data = heapq.heappop(self._heap)
            if event_type == "arrival":
                self._handle_arrival(data, time_ms)
            elif event_type == "begin_service":
                req, backend, cost, resources, dispatch_ms = data
                self._begin_service(req, backend, cost, resources, dispatch_ms, time_ms)
            elif event_type == "resource_complete":
                self._handle_resource_complete(data, time_ms)
            elif event_type == "workflow_arrival":
                self._handle_workflow_arrival(data, time_ms)
            else:
                raise ValueError(event_type)

    # ---- workflow handling ----

    def _handle_workflow_arrival(self, workflow: Workflow, now_ms: float) -> None:
        state = _WorkflowState(
            workflow=workflow,
            task_by_id=workflow.task_by_id,
            pending_predecessors={t.req_id: len(t.predecessors) for t in workflow.tasks},
            depth=_dag_depth(workflow.tasks),
        )
        self._workflow_states[workflow.workflow_id] = state

        admit_workflow = getattr(self.dispatcher, "admit_workflow", None)
        if admit_workflow is not None:
            if not admit_workflow(workflow, self.backends, now_ms):
                # Rejected as a whole: no task is attempted, no resource consumed.
                state.rejected_at_admission = True
                for t in workflow.tasks:
                    state.unreachable_task_ids.add(t.req_id)
                self._maybe_finalize_workflow(state, now_ms)
                return
            # Admitted: mark tasks so the dispatcher does not reject them later
            # on resource risk, otherwise upfront admission protects nothing.
            for t in workflow.tasks:
                t.workflow_admitted = True

        for t in workflow.tasks:
            if not t.predecessors:
                self._handle_arrival(t, now_ms)

    def _propagate_unreachable(self, state: _WorkflowState, task_id: int) -> None:
        """Mark all descendants of a discarded task as unreachable."""
        task = state.task_by_id[task_id]
        for succ_id in task.successors:
            if (succ_id in state.completed_task_ids or succ_id in state.rejected_task_ids
                    or succ_id in state.unreachable_task_ids):
                continue
            state.unreachable_task_ids.add(succ_id)
            self._propagate_unreachable(state, succ_id)

    def _on_workflow_task_rejected(self, req: Request, now_ms: float) -> None:
        state = self._workflow_states[req.workflow_id]
        state.rejected_task_ids.add(req.req_id)
        self._propagate_unreachable(state, req.req_id)
        self._maybe_finalize_workflow(state, now_ms)

    def _on_workflow_task_completed(self, req_state: RequestState, now_ms: float) -> None:
        req = req_state.req
        state = self._workflow_states[req.workflow_id]
        state.completed_task_ids.add(req.req_id)
        # Always accumulated; only reported if the workflow ends up failing.
        state.wasted_cost.append(req_state.cost)

        task = state.task_by_id[req.req_id]
        for succ_id in task.successors:
            if succ_id in state.unreachable_task_ids:
                continue
            state.pending_predecessors[succ_id] -= 1
            if state.pending_predecessors[succ_id] == 0:
                self._handle_arrival(state.task_by_id[succ_id], now_ms)

        self._maybe_finalize_workflow(state, now_ms)

    def _maybe_finalize_workflow(self, state: _WorkflowState, now_ms: float) -> None:
        if state.finalized:
            return
        n = len(state.workflow.tasks)
        resolved = (len(state.completed_task_ids) + len(state.rejected_task_ids)
                    + len(state.unreachable_task_ids))
        if resolved < n:
            return
        state.finalized = True

        failed = bool(state.rejected_task_ids) or bool(state.unreachable_task_ids)
        if failed:
            wasted_cpu_ms = sum(c.get("cpu_ms", 0.0) for c in state.wasted_cost)
            wasted_bytes = sum(c.get("bytes", 0.0) for c in state.wasted_cost)
            record = WorkflowRecord(
                workflow_id=state.workflow.workflow_id, tenant=state.workflow.tenant,
                topology=state.workflow.topology, n_tasks=n, depth=state.depth,
                arrival_ms=state.workflow.arrival_ms, completed=False,
                tasks_completed=len(state.completed_task_ids),
                tasks_rejected=len(state.rejected_task_ids),
                tasks_unreachable=len(state.unreachable_task_ids),
                wasted_cpu_ms=wasted_cpu_ms, wasted_bytes=wasted_bytes,
                rejected_at_admission=state.rejected_at_admission,
            )
        else:
            record = WorkflowRecord(
                workflow_id=state.workflow.workflow_id, tenant=state.workflow.tenant,
                topology=state.workflow.topology, n_tasks=n, depth=state.depth,
                arrival_ms=state.workflow.arrival_ms, completed=True,
                completion_ms=now_ms, makespan_ms=now_ms - state.workflow.arrival_ms,
                tasks_completed=len(state.completed_task_ids),
            )
        self.completed_workflows.append(record)

        # Release capacity reserved by admit_workflow(), or admission would
        # eventually starve.
        release_workflow = getattr(self.dispatcher, "release_workflow", None)
        if release_workflow is not None:
            release_workflow(state.workflow.workflow_id)
