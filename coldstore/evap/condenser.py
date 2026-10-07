"""Evaporative condenser temperature baseline.

The condenser is held near an adaptive baseline: an ambient binned, hourly
sliding curve that replaces the single manually typed number. When the
reading climbs past the over-limit threshold the healthy standby fan is
brought online automatically (in parallel, then promoted on recovery).

``Condenser`` remains the single facade the runtime, telemetry and reports
read, so every consumer sees the same baseline value and verdict.
"""

from __future__ import annotations

from coldstore.fan import FanSet
from coldstore.store.store import Store

from .controller import (
    DEFAULT_FALLBACK,
    CondenserController,
)
from .report import CondenserState

DEFAULT_BASELINE = DEFAULT_FALLBACK

# Legacy key names kept so older state files still load.
BASELINE_KEY = "evap:baseline"
TEMP_KEY = "evap:temperature"


class Condenser:
    """Reads and drives the adaptive baseline behind one fan pair."""

    def __init__(self, store: Store, fans: FanSet) -> None:
        self._store = store
        self._fans = fans
        self._controller = CondenserController(store, fans)

    @property
    def controller(self) -> CondenserController:
        return self._controller

    def baseline(self) -> float:
        """The baseline value every consumer must use (ambient adjusted)."""

        return self._controller.baseline_value()[0]

    def baseline_projection(self) -> tuple[float, str, float, str]:
        return self._controller.baseline_value()

    def set_baseline(self, value: float) -> float:
        """Set the manual fallback used while the learned curve is cold."""

        return self._controller.set_fallback(value)

    def temperature(self) -> float:
        return self._controller.temperature()

    def set_temperature(self, value: float) -> float:
        """Legacy manual write; stored and judged without training the curve."""

        import time as _time

        from .controller import AMBIENT_KEY

        self._store.put(TEMP_KEY, f"{float(value):.4f}")
        if not self._store.get(AMBIENT_KEY)[1]:
            self._store.put(AMBIENT_KEY, "0.0000")
        self._controller.evaluate_manual(_time.time())
        return value

    def above_baseline(self) -> bool:
        return self._controller.above_baseline()

    def duty(self) -> str:
        """Active evaporative fan, or an empty string when none is running."""

        return self._controller.duty()

    def switch_to_standby(self, fan_id: str) -> str:
        """Promote one fan and stop the unit that was running before."""

        return self._controller.switch_to_standby(fan_id)

    def state(self) -> CondenserState:
        value, quality, confidence, basis = self._controller.baseline_value()
        accepted, rejected = self._controller.counters()
        status = self._controller.status()
        temperature = self._controller.temperature()
        verdict = self._controller.last_verdict()
        return CondenserState(
            temperature=temperature,
            baseline=value,
            above_baseline=self.above_baseline(),
            duty=self.duty(),
            ambient=self._controller.ambient(),
            status=status,
            verdict=verdict,
            baseline_quality=quality,
            threshold=value + self._controller.config().over_margin,
            residual=temperature - value,
            accepted=accepted,
            rejected=rejected,
            basis=basis,
            detail={
                "confidence": confidence,
                "running": self._controller.running_ids(),
                "standby": self._fans.healthy_standby(),
                "command": self._controller.last_command(),
                "alarms": list(self._controller.last_alarms()),
                "sliding": "hourly",
            },
        )

    def describe(self) -> str:
        state = self.state()
        return (
            f"evap temperature={state.temperature:.4f} baseline={state.baseline:.4f} "
            f"ambient={state.ambient:.4f} quality={state.baseline_quality} "
            f"status={state.status} duty={state.duty or ''}"
        )
