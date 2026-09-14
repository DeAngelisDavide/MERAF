"""
Plane A (Algorithm 1, OnConnection): TLS negotiation parametrised by T only,
session resumption and rate limiting of new PQC handshakes. Its only signal
to Plane B is the boolean is_full_handshake, used to modulate sigma in scoring.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

from .model import Transport, HANDSHAKE_OVERHEAD_BYTES


@dataclass
class PianoAStats:
    """Handshake counters."""
    full_handshakes: int = 0
    resumptions: int = 0
    rate_limited: int = 0


class PianoA:
    """Resumption model per (tenant, transport): the first connection is a full
    handshake, later ones within the session window are resumptions (TLS 1.3
    PSK / NewSessionTicket behaviour)."""

    def __init__(self, resumption_window_ms: float = 5000.0,
                 max_full_handshakes_per_ms: float = 1.0):
        self.resumption_window_ms = resumption_window_ms
        self.max_full_handshakes_per_ms = max_full_handshakes_per_ms
        self._last_seen_ms: Dict[tuple, float] = {}
        self._recent_full_handshakes: list = []  # full PQC handshake timestamps, for rate limiting
        self.stats = PianoAStats()

    def on_connection(self, tenant: str, transport: Transport, now_ms: float) -> bool:
        """Return True for a full handshake, False for a resumption. Full PQC
        handshakes beyond max_full_handshakes_per_ms are counted as rate-limited
        but not blocked."""
        key = (tenant, transport)
        last = self._last_seen_ms.get(key)
        self._last_seen_ms[key] = now_ms

        is_full = last is None or (now_ms - last) > self.resumption_window_ms

        if is_full:
            if transport == Transport.PQC:
                # sliding 1 ms window of full PQC handshakes
                self._recent_full_handshakes = [
                    t for t in self._recent_full_handshakes if now_ms - t <= 1.0
                ]
                if len(self._recent_full_handshakes) >= self.max_full_handshakes_per_ms:
                    self.stats.rate_limited += 1
                self._recent_full_handshakes.append(now_ms)
            self.stats.full_handshakes += 1
        else:
            self.stats.resumptions += 1
        return is_full

    @staticmethod
    def handshake_overhead_bytes(transport: Transport) -> int:
        return HANDSHAKE_OVERHEAD_BYTES[transport]
