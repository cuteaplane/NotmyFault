from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityStatus:
    id: str
    available: bool
    backend: str | None
    reason: str | None
    degraded: bool
