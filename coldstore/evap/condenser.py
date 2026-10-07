"""Evaporative condenser temperature baseline.

The condenser is held near an adaptive baseline. High frequency condenser and
ambient samples are rate limited before they enter the daily regression that
corrects the baseline for ambient temperature; samples taken while the reading
is already high are refused for learning, so a fault never trains the baseline
up to itself.

A reading that stays above the baseline dead band for several confirmed
samples is a real excursion: the duty fan is exchanged for a healthy standby
automatically. Every baseline change, verdict transition and duty change is
written to the audit trail with the same numbers telemetry and the operator
report use.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from coldstore.fan import FanSet
from coldstore.store.epoch import load_float, save_float
from coldstore.store.store import Store

from .baseline import BaselineTracker, DEFAULT_ANCHOR
from .report import HIGH, NORMAL, CondenserState

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from coldstore.audit import Auditor

BASELINE_KEY = "evap:baseline"
TEMP_KEY = "evap:temperature"
DEFAULT_BASELINE = DEFAULT_ANCHOR

DEAD_BAND = 1.0
CONFIRM_SAMPLES = 2
AUDIT_DELTA = 0.5


class Condenser:
    """Holds the condenser temperature, its adaptive baseline and the fans."""

    def __init__(self, store: Store, fans: FanSet, auditor: Auditor | None = None) -> None:
        self._store = store
        self._fans = fans
        self._auditor = auditor
        self._tracker = BaselineTracker(store)

    def baseline(self) -> float:
        """Ambient corrected baseline reported by every consumer."""

        return self._tracker.effective_baseline()

    def set_baseline(self, value: float) -> float:
        """Manually seed the reference baseline and restart the daily learn."""

        self._tracker.set_anchor(value)
        self._store.put("evap:status", NORMAL)
        self._store.put("evap:high-streak", "0")
        self._store.put("evap:switch-latch", "0")
        save_float(self._store, "evap:baseline-audited", self._tracker.effective_baseline())
        self._audit(
            "evap_baseline",
            {
                "anchor": round(value, 4),
                "slope": 0.0,
                "baseline": round(self._tracker.effective_baseline(), 4),
                "source": "manual",
            },
        )
        return value

    def temperature(self) -> float:
        value, _ = load_float(self._store, TEMP_KEY)
        return value

    def set_temperature(self, value: float) -> float:
        save_float(self._store, TEMP_KEY, value)
        return value

    def ambient(self) -> float:
        return self._tracker.last_ambient()

    def tracker(self) -> BaselineTracker:
        return self._tracker

    def above_baseline(self) -> bool:
        return self.temperature() > self.baseline()

    def is_high(self) -> bool:
        return self.temperature() > self._tracker.effective_baseline() + self._margin()

    def _margin(self) -> float:
        # Before the first daily fit the anchor is only an operator seed, so
        # no dead band is added: cold start samples would otherwise be frozen
        # out whenever they sit slightly above that one-shot number.
        return DEAD_BAND if self._tracker.learned() else 0.0

    def _exceeds(self, temperature: float, ambient: float) -> bool:
        return temperature > self._tracker.baseline_for(ambient) + self._margin()

    def duty(self) -> str:
        """Active evaporative fan, or an empty string when none is running."""

        return self._fans.active()

    def observe(self, temperature: float, ambient: float, at: int | None = None) -> CondenserState:
        """Accept one high frequency sample and run the full control step.

        The latest temperature is always stored for monitoring. Learning is
        rate limited and frozen whenever the reading is above the dead band;
        a confirmed high excursion switches the duty fan to a healthy
        standby.
        """

        self.set_temperature(temperature)
        accepted = False
        high = self._exceeds(temperature, ambient)
        if not high:
            accepted, _ = self._tracker.observe(ambient, temperature, at)

        self._audit_baseline_drift()
        self._update_verdict(high)
        return self.state(accepted)

    def _update_verdict(self, high: bool) -> None:
        if high:
            streak = self._streak() + 1
            self._store.put("evap:high-streak", str(streak))
            if self._status_memory() != HIGH:
                self._audit(
                    "evap_verdict",
                    {
                        "status": HIGH,
                        "temperature": round(self.temperature(), 4),
                        "baseline": round(self.baseline(), 4),
                        "streak": streak,
                    },
                )
                self._store.put("evap:status", HIGH)
            if streak >= CONFIRM_SAMPLES and not self._latched():
                self._auto_switch()
            return

        if self._status_memory() == HIGH:
            self._audit(
                "evap_verdict",
                {
                    "status": NORMAL,
                    "temperature": round(self.temperature(), 4),
                    "baseline": round(self.baseline(), 4),
                },
            )
        self._store.put("evap:status", NORMAL)
        self._store.put("evap:high-streak", "0")
        self._store.put("evap:switch-latch", "0")

    def _auto_switch(self) -> None:
        standby = self._fans.healthy_standby()
        if not standby:
            if not self._latched():
                self._store.put("evap:switch-latch", "1")
                self._audit(
                    "evap_duty",
                    {
                        "action": "switch_unavailable",
                        "duty": self.duty(),
                        "reason": "no healthy standby fan",
                    },
                )
            return
        previous = self.duty()
        self.switch_to_standby(standby, automatic=True, reason="excursion confirmed")
        self._store.put("evap:switch-latch", "1")
        self._audit(
            "evap_duty",
            {
                "action": "auto_switch",
                "from": previous,
                "to": standby,
                "temperature": round(self.temperature(), 4),
                "baseline": round(self.baseline(), 4),
            },
        )

    def switch_to_standby(self, fan_id: str, automatic: bool = False, reason: str = "") -> str:
        """Promote one fan and stop the unit that was running before."""

        previous = self._fans.active()
        if previous and previous != fan_id:
            self._fans.stop(previous)
        primary = self._fans.switch_over(fan_id)
        self._fans.start(primary)
        if not automatic:
            self._audit(
                "evap_duty",
                {"action": "manual_switch", "from": previous, "to": primary, "reason": reason},
            )
        return primary

    def _audit_baseline_drift(self) -> None:
        baseline = self.baseline()
        raw, present = load_float(self._store, "evap:baseline-audited")
        if present and abs(baseline - raw) < AUDIT_DELTA:
            return
        save_float(self._store, "evap:baseline-audited", baseline)
        self._audit(
            "evap_baseline",
            {
                "anchor": round(self._tracker.anchor(), 4),
                "slope": round(self._tracker.slope(), 4),
                "baseline": round(baseline, 4),
                "ambient": round(self.ambient(), 4),
                "samples": self._tracker.sample_count(),
                "source": "daily_regression",
            },
        )

    def _audit(self, kind: str, detail: dict[str, object]) -> None:
        if self._auditor is not None:
            self._auditor.record(kind, json.dumps(detail, ensure_ascii=False, sort_keys=True))

    def _streak(self) -> int:
        raw, present = self._store.get("evap:high-streak")
        if not present:
            return 0
        try:
            value = int(raw)
        except ValueError:
            return 0
        return max(0, value)

    def _latched(self) -> bool:
        raw, present = self._store.get("evap:switch-latch")
        return present and raw == "1"

    def _status_memory(self) -> str:
        raw, present = self._store.get("evap:status")
        return raw if present and raw == HIGH else NORMAL

    def _status(self) -> str:
        return HIGH if self.is_high() else NORMAL

    def state(self, accepted: bool = False) -> CondenserState:
        tracker = self._tracker
        return CondenserState(
            temperature=self.temperature(),
            ambient=self.ambient(),
            baseline=self.baseline(),
            anchor=tracker.anchor(),
            slope=tracker.slope(),
            above_baseline=self.above_baseline(),
            status=self._status(),
            samples=tracker.sample_count(),
            learned=tracker.learned(),
            accepted=accepted,
            duty=self.duty(),
        )

    def describe(self) -> str:
        state = self.state()
        return (
            f"evap temperature={state.temperature:.4f} ambient={state.ambient:.4f} "
            f"baseline={state.baseline:.4f} slope={state.slope:.4f} "
            f"status={state.status} duty={state.duty or ''}"
        )
