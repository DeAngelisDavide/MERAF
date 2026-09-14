"""
Backend node with heterogeneous capacities. cpu/net/decrypt are fair-share
SharedResources; mem is a plain concurrent-capacity constraint (held for the
whole request, never shared).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from .resources import SharedResource


@dataclass
class BackendSpec:
    """Nominal capacities and tolerance thresholds of a backend."""
    name: str
    cpu_capacity_per_ms: float     # CPU work units per ms
    net_capacity_bytes_per_ms: float
    decrypt_capacity_per_ms: float  # dedicated decryption capacity
    mem_capacity_mb: float
    # h_k: max tolerated "equivalent concurrent flows" per resource.
    h_cpu: float = 40.0
    h_net: float = 8.0
    h_decrypt: float = 20.0
    h_mem: float = 1.0   # mem utilisation is already a ratio in [0,1]
    base_rtt_ms: float = 0.0   # fixed propagation/RTT before data flows (0 on loopback)
    # Static trust tier, used only by the security-as-constraint baselines; the
    # scale depends on the baseline (see model.required_*). MERAF ignores it.
    security_tier: int = 0


class Backend:
    """Runtime state of a backend: shared resources, memory, per-class latency EWMA."""

    def __init__(self, spec: BackendSpec):
        self.spec = spec
        self.cpu = SharedResource("cpu", spec.cpu_capacity_per_ms)
        self.net = SharedResource("net", spec.net_capacity_bytes_per_ms)
        self.decrypt = SharedResource("decrypt", spec.decrypt_capacity_per_ms)
        self.mem_used_mb: float = 0.0
        self.healthy: bool = True

        # Per-class (Request.cls_key) latency EWMA mean/variance and sample count.
        self._latency_ewma_mean: Dict[str, float] = {}
        self._latency_ewma_var: Dict[str, float] = {}
        self._n_obs: Dict[str, int] = {}
        # Sim time (ms) of the last observation per class; drives is_stale().
        self._last_obs_ms: Dict[str, float] = {}

    # ---- risk projection ----

    def projected_utilization(self, resources_needed: tuple, extra: int = 1) -> Dict[str, float]:
        """Projected utilisation u'_{i,k} per resource after admitting `extra` flows."""
        u = {}
        if "cpu" in resources_needed or "cpu(io)" in resources_needed:
            u["cpu"] = self.cpu.projected_active_count(extra)
        if "net" in resources_needed:
            u["net"] = self.net.projected_active_count(extra)
        if "decrypt" in resources_needed:
            u["decrypt"] = self.decrypt.projected_active_count(extra)
        if "mem" in resources_needed:
            u["mem"] = self.mem_used_mb / max(self.spec.mem_capacity_mb, 1e-9)
        return u

    def thresholds(self) -> Dict[str, float]:
        return dict(cpu=self.spec.h_cpu, net=self.spec.h_net,
                    decrypt=self.spec.h_decrypt, mem=self.spec.h_mem)

    def has_mem_headroom(self, extra_mb: float) -> bool:
        return (self.mem_used_mb + extra_mb) <= self.spec.mem_capacity_mb

    # ---- adaptive learning: per-class latency EWMA ----

    def observe_latency(self, cls: str, latency_ms: float, now_ms: float,
                         alpha: float = 0.2) -> None:
        n = self._n_obs.get(cls, 0)
        if n == 0:
            self._latency_ewma_mean[cls] = latency_ms
            self._latency_ewma_var[cls] = 0.0
        else:
            prev_mean = self._latency_ewma_mean[cls]
            new_mean = alpha * latency_ms + (1 - alpha) * prev_mean
            # standard EWMA variance recursion
            diff = latency_ms - prev_mean
            self._latency_ewma_var[cls] = (
                (1 - alpha) * (self._latency_ewma_var[cls] + alpha * diff * diff)
            )
            self._latency_ewma_mean[cls] = new_mean
        self._n_obs[cls] = n + 1
        self._last_obs_ms[cls] = now_ms

    def is_stale(self, cls: str, now_ms: float, max_age_ms: float) -> bool:
        """IsStale(): no EWMA observation for `cls`, or the last one is older than
        max_age_ms. Callers must then fall back to a structural estimate
        (e.g. default_l95) instead of predicted_l95/predicted_sigma."""
        last = self._last_obs_ms.get(cls)
        return last is None or (now_ms - last) > max_age_ms

    def predicted_mean(self, cls: str, default_ms: float) -> float:
        """Raw EWMA mean (no +1.645*std offset), for the probabilistic scoring variant."""
        return self._latency_ewma_mean.get(cls, default_ms)

    def predicted_l95(self, cls: str, default_ms: float) -> float:
        mean = self._latency_ewma_mean.get(cls, default_ms)
        var = self._latency_ewma_var.get(cls, (default_ms * 0.3) ** 2)
        std = var ** 0.5
        # p95 ~ mean + 1.645*std under a Gaussian prior
        return max(mean + 1.645 * std, 0.0)

    def predicted_sigma(self, cls: str, default_ms: float) -> float:
        n = self._n_obs.get(cls, 0)
        var = self._latency_ewma_var.get(cls, (default_ms * 0.5) ** 2)
        std = var ** 0.5
        if n < 5:
            # few samples: inflate uncertainty
            return max(std, default_ms * 0.5)
        return std
