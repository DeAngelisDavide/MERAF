"""
Cost model and security-profile taxonomy for MERAF: Eqs. (1)-(5) of the
paper (per-profile cost, dominant-resource risk, scoring). Stdlib only;
design notes and calibration sources live in docs/core-model.md.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


# ---- Security-axis taxonomy: transport T x payload P ----

class Transport(str, Enum):
    CLASSIC = "classic"   # X25519
    PQC = "pqc"            # X25519MLKEM768 (hybrid)


class Payload(str, Enum):
    PLAIN = "plain"
    DECRYPT = "decrypt"     # encrypted, decrypted server-side
    OPAQUE = "opaque"       # encrypted, never decrypted server-side


@dataclass(frozen=True)
class SecurityProfile:
    """Composable security profile (T, P)."""
    transport: Transport
    payload: Payload

    def __str__(self) -> str:
        return f"{self.transport.value}+{self.payload.value}"


# Client+server key material per handshake: X25519 ~64 B, hybrid ML-KEM ~2336 B.
HANDSHAKE_OVERHEAD_BYTES = {
    Transport.CLASSIC: 64,
    Transport.PQC: 2336,
}


@dataclass
class CostCalibration:
    """Cost-model constants (ms unless noted), calibrated on the paper's benchmarks."""

    c_fixed_ms: float = 0.05          # fixed per-request handling
    c_business_ms: float = 0.05       # minimal business logic
    c_parse_ms: float = 0.02          # encrypted-envelope parsing (DECRYPT only)
    c_meta_ms: float = 0.02           # metadata handling (OPAQUE only)
    m_base_mb: float = 2.0            # base memory per request (MB)

    # Symmetric-cipher cost per byte, from Server_AES_Time_ms on a 5 MB payload.
    alpha_cipher_hw_ms_per_byte: float = 27.5765 / (5120 * 1024)

    # Fixed ML-KEM-768 envelope cost; calibrated on the median (mean is outlier-skewed).
    kem_envelope_fixed_ms: float = 0.200

    # OPAQUE relay is pure copy/framing: a fraction of the cipher cost.
    gamma_io_ms_per_byte: float =  27.5765 / (5120 * 1024) * 0.15

    beta_relay: float = 1.0           # buffering factor for the OPAQUE relay

    # Memory prior for DECRYPT until enough RSS samples exist to estimate a p95.
    decrypt_mem_prior_ratio: float = 0.20  # fraction of payload size

    def cost_plain(self, payload_bytes: int) -> Dict[str, float]:
        """Eq. (1): P = PLAIN."""
        c_hat = self.c_fixed_ms + self.c_business_ms
        m_hat = self.m_base_mb
        return dict(cpu_ms=c_hat, mem_mb=m_hat, bytes=payload_bytes)

    def cost_decrypt(self, payload_bytes: int, use_kem_envelope: bool,
                      rss_p95_mb: Optional[float] = None) -> Dict[str, float]:
        """Eq. (2): P = DECRYPT, with or without an ML-KEM envelope."""
        aes_cost = self.alpha_cipher_hw_ms_per_byte * payload_bytes
        kem_cost = self.kem_envelope_fixed_ms if use_kem_envelope else 0.0
        c_hat = self.c_fixed_ms + aes_cost + kem_cost + self.c_parse_ms + self.c_business_ms
        m_hat = rss_p95_mb if rss_p95_mb is not None else (
            self.m_base_mb + self.decrypt_mem_prior_ratio * payload_bytes / (1024 * 1024)
        )
        decrypt_cost = aes_cost + kem_cost
        return dict(cpu_ms=c_hat, mem_mb=m_hat, bytes=payload_bytes,
                    decrypt_ms=decrypt_cost)

    def cost_opaque(self, payload_bytes: int) -> Dict[str, float]:
        """Eq. (3): P = OPAQUE."""
        io_cost = self.gamma_io_ms_per_byte * payload_bytes
        c_hat = self.c_fixed_ms + io_cost + self.c_meta_ms
        m_hat = self.m_base_mb + self.beta_relay * payload_bytes / (1024 * 1024)
        return dict(cpu_ms=c_hat, mem_mb=m_hat, bytes=payload_bytes)


def resource_set(payload: Payload) -> tuple:
    """Relevant resource set R(P) for a payload class."""
    if payload == Payload.PLAIN:
        return ("cpu", "mem", "net")
    if payload == Payload.DECRYPT:
        return ("cpu", "mem", "net", "decrypt")
    if payload == Payload.OPAQUE:
        return ("cpu(io)", "mem", "net")
    raise ValueError(payload)


# Static security-tier requirements below are used ONLY by the
# security-as-constraint baselines in dispatchers.py; MERAF has no notion of tiers.

def required_security_tier(profile: SecurityProfile) -> int:
    """Binary tier for the [24]-style baseline: PLAIN -> 0, encrypted -> 1."""
    return 0 if profile.payload == Payload.PLAIN else 1


# Trust labels of [22] Singh & Auluck 2020, Sec. 3; ordered, compared with >=.
TRUST_UNTRUSTED = 0     # lut
TRUST_SEMI_TRUSTED = 1  # lst
TRUST_HIGHLY_TRUSTED = 2  # lht


def required_trust_label_singh_auluck(payload: Payload) -> int:
    """Minimum node trust label per [22] Table 2: PLAIN->lpu, DECRYPT->lsp, OPAQUE->lp."""
    if payload == Payload.PLAIN:
        return TRUST_UNTRUSTED       # lpu: any node
    if payload == Payload.DECRYPT:
        return TRUST_SEMI_TRUSTED    # lsp: lst or lht
    if payload == Payload.OPAQUE:
        return TRUST_HIGHLY_TRUSTED  # lp: lht only
    raise ValueError(payload)


def dominant_risk(projected_util: Dict[str, float],
                   thresholds: Dict[str, float],
                   resources: tuple) -> float:
    """Eq. (4): rho_i(r,P) = max_{k in R(P)} u'_{i,k} / h_k."""
    worst = 0.0
    for k in resources:
        u = projected_util.get(k, 0.0)
        h = thresholds.get(k, 1.0)
        worst = max(worst, u / h if h > 0 else math.inf)
    return worst


@dataclass
class ScoreWeights:
    """Weights of the scoring function, Eq. (5)."""
    lam: float = 1.0     # lambda: risk penalty
    eta: float = 0.5     # eta: uncertainty weight
    gamma: float = 0.3   # gamma: affinity bonus


def derive_resource_threshold(capacity_per_ms: float, slo_ms: float,
                                typical_work_per_request: float) -> float:
    """Derive h_k = capacity_per_ms * slo_ms / typical_work_per_request (>= 1).

    Number of concurrent fair-share flows that still complete within the SLO
    on average, so the threshold scales with node capacity instead of being
    a fixed constant.
    """
    if typical_work_per_request <= 0:
        return float("inf")
    return max(capacity_per_ms * slo_ms / typical_work_per_request, 1.0)


def prob_exceed_slo(mean_ms: float, sigma_ms: float, slo_ms: float) -> float:
    """P(latency > SLO) under a Gaussian (mean, sigma); sigma <= 0 is deterministic."""
    if sigma_ms <= 0:
        return 1.0 if mean_ms > slo_ms else 0.0
    z = (slo_ms - mean_ms) / sigma_ms
    phi = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return 1.0 - phi


def score(l95_ms: float, slo_ms: float, rho: float, sigma_ms: float,
          affinity_bonus: float, weights: ScoreWeights,
          latency_term: Optional[float] = None) -> float:
    """Eq. (5): s = latency_term + lam*(-log(1-rho)) + eta*sigma/SLO - gamma*A.

    latency_term defaults to L95/SLO (unbounded); pass prob_exceed_slo(...)
    for the bounded [0,1] variant. rho is clamped below 1 so the barrier
    stays finite.
    """
    eps = 1e-6
    rho_c = min(max(rho, 0.0), 1.0 - eps)
    barrier = -math.log(1.0 - rho_c)
    if latency_term is None:
        latency_term = l95_ms / slo_ms
    return (latency_term
            + weights.lam * barrier
            + weights.eta * (sigma_ms / slo_ms)
            - weights.gamma * affinity_bonus)
