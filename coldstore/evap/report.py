"""Evaporative condenser projections and validation rules."""

from __future__ import annotations

from dataclasses import dataclass

NORMAL = "normal"
HIGH = "high"


@dataclass(frozen=True)
class CondenserState:
    """Reading, adaptive baseline, verdict and the duty fan."""

    temperature: float
    ambient: float
    baseline: float
    anchor: float
    slope: float
    above_baseline: bool
    status: str
    samples: int
    learned: bool
    accepted: bool
    duty: str

    def as_dict(self) -> dict[str, object]:
        return {
            "temperature": self.temperature,
            "ambient": self.ambient,
            "baseline": self.baseline,
            "anchor": self.anchor,
            "slope": self.slope,
            "above_baseline": self.above_baseline,
            "status": self.status,
            "samples": self.samples,
            "learned": self.learned,
            "accepted": self.accepted,
            "duty": self.duty,
        }


def validate_baseline(value: float) -> None:
    """Reject condenser temperatures and baselines outside the supported range."""

    if value < -100:
        raise ValueError("baseline is below the supported range")
    if value > 100:
        raise ValueError("baseline exceeds the supported range")
