"""Evaporative condenser projections and validation rules."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CondenserState:
    """Reading, baseline and the duty fan serving the condenser."""

    temperature: float
    baseline: float
    above_baseline: bool
    duty: str
    ambient: float = 0.0
    status: str = "normal"
    verdict: str = "ok"
    baseline_quality: str = "cold"
    threshold: float = 0.0
    residual: float = 0.0
    accepted: int = 0
    rejected: int = 0
    basis: str = "manual"
    detail: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "temperature": self.temperature,
            "baseline": self.baseline,
            "above_baseline": self.above_baseline,
            "duty": self.duty,
            "ambient": self.ambient,
            "status": self.status,
            "verdict": self.verdict,
            "baseline_quality": self.baseline_quality,
            "threshold": self.threshold,
            "residual": self.residual,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "basis": self.basis,
            "detail": self.detail,
        }


def validate_baseline(value: float) -> None:
    """Reject baselines outside the supported range."""

    if value < -100:
        raise ValueError("baseline is below the supported range")
    if value > 100:
        raise ValueError("baseline exceeds the supported range")
