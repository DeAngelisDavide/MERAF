"""
Processor-sharing resource used to model a backend's CPU, network bandwidth
and decryption subsystem: capacity is split equally among active flows and
remaining work is recomputed whenever a flow joins or leaves.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass
class _Flow:
    flow_id: int
    remaining_work: float   # remaining work units (e.g. CPU-ms or bytes)


class SharedResource:
    """Fixed-capacity resource (work units per ms) shared max-min fairly among active flows."""

    def __init__(self, name: str, capacity_per_ms: float):
        self.name = name
        self.capacity_per_ms = capacity_per_ms
        self._flows: Dict[int, _Flow] = {}
        self._last_update_ms: float = 0.0

    @property
    def active_count(self) -> int:
        return len(self._flows)

    def _advance(self, now_ms: float) -> None:
        """Charge every active flow for the work done since the last update
        at the fair share that prevailed over that interval."""
        if not self._flows:
            self._last_update_ms = now_ms
            return
        elapsed = now_ms - self._last_update_ms
        if elapsed <= 0:
            self._last_update_ms = now_ms
            return
        share = self.capacity_per_ms / len(self._flows)
        done = share * elapsed
        for f in self._flows.values():
            f.remaining_work -= done
        self._last_update_ms = now_ms

    def add_flow(self, now_ms: float, flow_id: int, work_units: float) -> None:
        """Register a new flow with `work_units` of remaining work at now_ms."""
        self._advance(now_ms)
        self._flows[flow_id] = _Flow(flow_id, max(work_units, 1e-9))

    def remove_flow(self, now_ms: float, flow_id: int) -> None:
        self._advance(now_ms)
        self._flows.pop(flow_id, None)

    def next_completion(self, now_ms: float) -> Optional[Tuple[int, float]]:
        """(flow_id, absolute completion time) of the earliest-finishing flow,
        assuming the active set does not change; None if idle."""
        self._advance(now_ms)
        if not self._flows:
            return None
        share = self.capacity_per_ms / len(self._flows)
        if share <= 0:
            return None
        best_id, best_t = None, float("inf")
        for f in self._flows.values():
            t_left = f.remaining_work / share
            if t_left < best_t:
                best_id, best_t = f.flow_id, t_left
        return best_id, now_ms + best_t

    def projected_active_count(self, extra_flows: int = 1) -> int:
        """Active flow count if `extra_flows` more were added now; the
        dispatcher divides it by h_k to obtain u'_{i,k}."""
        return len(self._flows) + extra_flows
