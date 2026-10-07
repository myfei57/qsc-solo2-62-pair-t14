"""Verdict and duty fan control for the evaporative condenser.

Exceedance action is decided once and for all here: an excursion brings the
healthy standby fan online **in parallel** (the running duty fan keeps
working, so airflow is never removed during a fault); only after recovery is
the old unit stopped and the standby promoted. There is no speed increase:
the fan pair is single speed and adding a whole unit is the only larger
cooling response available.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from coldstore.fan import FanSet
from coldstore.store.store import Store

from .baseline import (
    AMBIENT_RANGE,
    BAD,
    BaselineConfig,
    RateLimiter,
    SteadyGate,
    TEMPERATURE_RANGE,
    TemperatureBaseline,
    load_config,
    update_config,
)

CONTROL_KEY = "evap:control"
FALLBACK_KEY = "evap:baseline"
TEMP_KEY = "evap:temperature"
AMBIENT_KEY = "evap:ambient"
LAST_SAMPLE_KEY = "evap:last-sample"
DEFAULT_FALLBACK = 32.0

NORMAL = "normal"
WARN = "warn"
OVER = "over"
FAULT = "fault"

# Verdict labels carried into telemetry and the audit trail.
OK = "ok"
WARNING = "warning"
EXCEEDED = "exceeded"
PERSISTENT_EXCEEDED = "persistent-exceeded"
SENSOR_FAULT = "sensor-fault"


@dataclass(frozen=True)
class SampleResult:
    accepted: bool
    reason: str
    learned: bool
    steady: bool
    status: str
    verdict: str
    temperature: float
    ambient: float
    baseline: float
    threshold: float
    residual: float
    quality: str
    duty: str
    standby: str
    command: str
    alarms: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "learned": self.learned,
            "steady": self.steady,
            "status": self.status,
            "verdict": self.verdict,
            "temperature": self.temperature,
            "ambient": self.ambient,
            "baseline": self.baseline,
            "threshold": self.threshold,
            "residual": self.residual,
            "baseline_quality": self.quality,
            "duty": self.duty,
            "standby": self.standby,
            "command": self.command,
            "alarms": list(self.alarms),
        }


class CondenserController:
    """Owns the adaptive baseline, the verdict state and the fan actions."""

    def __init__(self, store: Store, fans: FanSet) -> None:
        self._store = store
        self._fans = fans
        self._config = load_config(store)
        self._baseline = TemperatureBaseline(store, self._config)
        control = self._load_control()
        self._limiter = RateLimiter(
            float(control.get("tokens", 0.0)),
            float(control.get("last_feed_at", 0.0)),
        )
        gate = control.get("gate")
        self._gate = SteadyGate(gate if isinstance(gate, dict) else None)
        self._status = str(control.get("status", NORMAL))
        self._over_since = float(control.get("over_since", 0.0) or 0.0)
        self._alarm_at = float(control.get("alarm_at", 0.0) or 0.0)
        self._last_changeover = float(control.get("last_changeover", 0.0) or 0.0)
        self._rotation_anchor = float(control.get("rotation_anchor", 0.0) or 0.0)
        self._accepted_total = int(control.get("accepted_total", 0))
        self._rejected_total = int(control.get("rejected_total", 0))
        self._last_verdict = str(control.get("last_verdict", OK))
        self._last_command = str(control.get("last_command", "none"))
        raw_alarms = control.get("last_alarms")
        self._last_alarms = tuple(str(item) for item in raw_alarms) if isinstance(raw_alarms, list) else ()
        self._last_reason = str(control.get("last_reason", ""))

    def last_verdict(self) -> str:
        return self._last_verdict

    def last_command(self) -> str:
        return self._last_command

    def last_alarms(self) -> tuple[str, ...]:
        return self._last_alarms

    # ---- configuration --------------------------------------------------

    def config(self) -> BaselineConfig:
        return self._config

    def set_config(self, changes: dict[str, object]) -> BaselineConfig:
        self._config = update_config(self._store, changes)
        self._baseline = TemperatureBaseline(self._store, self._config)
        self._save_control()
        return self._config

    # ---- persisted manual fallback / latest readings --------------------

    def fallback(self) -> float:
        raw, present = self._store.get(FALLBACK_KEY)
        if not present:
            return DEFAULT_FALLBACK
        try:
            return float(raw)
        except ValueError:
            return DEFAULT_FALLBACK

    def set_fallback(self, value: float) -> float:
        if value < -100 or value > 100:
            raise ValueError("baseline is outside the supported range")
        self._store.put(FALLBACK_KEY, f"{value:.4f}")
        return value

    def temperature(self) -> float:
        raw, _ = self._store.get(TEMP_KEY)
        try:
            return float(raw)
        except ValueError:
            return 0.0

    def ambient(self) -> float:
        raw, _ = self._store.get(AMBIENT_KEY)
        try:
            return float(raw)
        except ValueError:
            return 0.0

    def last_sample_at(self) -> float:
        raw, present = self._store.get(LAST_SAMPLE_KEY)
        if not present:
            return 0.0
        try:
            return float(raw)
        except ValueError:
            return 0.0

    def status(self) -> str:
        return self._status

    # ---- the single baseline entry point --------------------------------

    def baseline_value(self, at: float | None = None) -> tuple[float, str, float, str]:
        """Return ``(value, quality, confidence, basis)`` everyone must read."""

        moment = at if at is not None else self.last_sample_at()
        view = self._baseline.query(self.ambient(), self.fallback(), moment)
        return view.value, view.quality, view.confidence, view.basis

    def above_baseline(self) -> bool:
        return self._status in (WARN, OVER)

    # ---- sampling -------------------------------------------------------

    def sample(
        self,
        at: float,
        ambient: float,
        temperature: float,
        fan_level: int = 1,
    ) -> SampleResult:
        """Feed one raw high frequency sample through the whole pipeline."""

        if not AMBIENT_RANGE[0] <= ambient <= AMBIENT_RANGE[1]:
            raise ValueError("ambient temperature is outside the sensor range")
        if not TEMPERATURE_RANGE[0] <= temperature <= TEMPERATURE_RANGE[1]:
            raise ValueError("condenser temperature is outside the sensor range")

        self._store.put(AMBIENT_KEY, f"{ambient:.4f}")
        self._store.put(TEMP_KEY, f"{temperature:.4f}")

        if not self._limiter.allow(at, self._config):
            self._rejected_total += 1
            self._last_reason = "rate-limited"
            self._save_control()
            return self._result(
                accepted=False,
                reason="rate-limited",
                learned=False,
                steady=False,
                command="none",
                alarms=(),
            )

        bin_index = self._baseline.bin_index(ambient)
        steady, _slopes = self._gate.is_steady(
            at, ambient, temperature, fan_level, self._config, bin_index
        )
        # While the process is moving or the point is a statistical outlier,
        # the sample still drives the verdict and the duty response; only the
        # baseline must never learn from it.
        statistical_ok, reason = self._baseline.consider(
            at, ambient, temperature
        )
        accepted = steady and statistical_ok
        if not steady:
            reason = "unsteady"
        learned = accepted and self._status in (NORMAL, WARN)
        if learned:
            self._baseline.learn(at, ambient, temperature)
        if accepted:
            self._accepted_total += 1
            self._gate.remember(at, ambient, temperature, fan_level, bin_index)
            if not self._rotation_anchor:
                self._rotation_anchor = at
        else:
            self._rejected_total += 1

        self._store.put(LAST_SAMPLE_KEY, f"{at:.3f}")
        command, alarms = self._evaluate(at)
        self._last_command = command
        self._last_alarms = alarms
        self._last_reason = reason
        self._save_control()
        result = self._result(
            accepted=accepted,
            reason=reason,
            learned=learned,
            steady=steady,
            command=command,
            alarms=alarms,
        )
        self._last_verdict = result.verdict
        self._save_control()
        return result

    def _evaluate(self, at: float) -> tuple[str, tuple[str, ...]]:
        """Update status against the served baseline and act on transitions."""

        if self._status == FAULT:
            return "none", ()

        temperature = self.temperature()
        baseline = self.baseline_value(at)[0]
        threshold = baseline + self._config.over_margin
        alarms: list[str] = []
        command = "none"

        if self._status == OVER:
            if temperature <= baseline + self._config.warn_margin - self._config.hysteresis:
                self._status = WARN
                self._over_since = 0.0
                self._alarm_at = 0.0
                command = self._finish_changeover()
            elif at >= self._alarm_at > 0:
                alarms.append(PERSISTENT_EXCEEDED)
                self._alarm_at = at + self._config.over_alarm_after
            return command, tuple(alarms)

        if temperature > threshold:
            if self._over_since == 0.0:
                self._over_since = at
            if at - self._over_since >= self._config.over_debounce:
                self._status = OVER
                command = self._bring_standby_online()
                self._alarm_at = at + self._config.over_alarm_after
                alarms.append(EXCEEDED)
            else:
                self._status = WARN
                alarms.append(WARNING)
        elif temperature > baseline + self._config.warn_margin:
            self._status = WARN
            self._over_since = 0.0
            alarms.append(WARNING)
        else:
            self._status = NORMAL
            self._over_since = 0.0
        return command, tuple(alarms)

    # ---- fan actions ----------------------------------------------------

    def _bring_standby_online(self) -> str:
        """Start a healthy standby next to the running duty fan."""

        standby = self._fans.healthy_standby()
        if not standby:
            return "unavailable"
        if not self._fans.active():
            self._fans.switch_over(standby)
            self._fans.start(standby)
            self._last_changeover = self.last_sample_at()
            return f"start:{standby}"
        # The running unit is deliberately kept running: no airflow gap.
        self._fans.start(standby)
        return f"parallel:{standby}"

    def _finish_changeover(self) -> str:
        """After recovery, promote the standby and stop the old duty unit."""

        running = self._fans.running_ids()
        standby = self._fans.healthy_standby()
        target = standby if standby and self._fans.fan(standby).running else (
            running[0] if running else ""
        )
        if not target:
            return "none"
        self._fans.switch_over(target)
        for fan_id in running:
            if fan_id != target:
                self._fans.stop(fan_id)
        self._last_changeover = self.last_sample_at()
        return f"promote:{target}"

    def switch_to_standby(self, fan_id: str) -> str:
        """Manual changeover, keeping the old fan up during the overlap."""

        unit = self._fans.fan(fan_id)
        if unit is None or unit.faulted:
            raise ValueError(f"fan {fan_id} is not a usable standby")
        previous = self._fans.active()
        self._fans.switch_over(fan_id)
        if not unit.running:
            self._fans.start(fan_id)
        if previous and previous != fan_id:
            self._fans.stop(previous)
        self._last_changeover = self.last_sample_at()
        return fan_id

    def duty(self) -> str:
        return self._fans.active()

    def running_ids(self) -> list[str]:
        return self._fans.running_ids()

    # ---- hourly tick: cheap bookkeeping, rotation, staleness ------------

    def tick(self, at: float) -> dict[str, object]:
        """Hourly maintenance only; learning already happened on samples."""

        actions: list[str] = []
        alarms: list[str] = []

        if self._status != FAULT:
            last = self.last_sample_at()
            if last and at - last > self._config.stale_seconds:
                self._status = FAULT
                command = self._fail_safe()
                actions.append(command)
                alarms.append(SENSOR_FAULT)
                self._last_verdict = SENSOR_FAULT
                self._last_command = command
                self._last_alarms = (SENSOR_FAULT,)

        if (
            self._status == NORMAL
            and self._rotation_anchor
            and at - self._rotation_anchor >= self._config.rotate_interval
        ):
            standby = self._fans.healthy_standby()
            if standby:
                self.switch_to_standby(standby)
                actions.append(f"rotate:{standby}")
                self._rotation_anchor = at

        self._save_control()
        return {
            "at": at,
            "sliding": "hourly",
            "actions": actions,
            "alarms": alarms,
            "status": self._status,
            "accepted": self._accepted_total,
            "rejected": self._rejected_total,
        }

    def _fail_safe(self) -> str:
        """Loss of signal: force maximum available evaporative airflow."""

        command = "none"
        for fan in self._fans.fans():
            if not fan.faulted and not fan.running:
                self._fans.start(fan.id)
                command = f"failsafe:{fan.id}"
        return command

    def clear_fault(self, at: float) -> str:
        """Operator acknowledgement once fresh samples are flowing again."""

        if self._status != FAULT:
            return "not-faulted"
        if not self.last_sample_at() or at - self.last_sample_at() > self._config.stale_seconds:
            return "still-stale"
        self._status = NORMAL
        self._over_since = 0.0
        self._alarm_at = 0.0
        self._save_control()
        return "cleared"

    # ---- persistence ----------------------------------------------------

    def _load_control(self) -> dict[str, object]:
        raw, present = self._store.get(CONTROL_KEY)
        if not present:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_control(self) -> None:
        payload = {
            "tokens": self._limiter.tokens,
            "last_feed_at": self._limiter.last_at,
            "gate": self._gate.as_dict(),
            "status": self._status,
            "over_since": self._over_since,
            "alarm_at": self._alarm_at,
            "last_changeover": self._last_changeover,
            "rotation_anchor": self._rotation_anchor,
            "accepted_total": self._accepted_total,
            "rejected_total": self._rejected_total,
            "last_verdict": self._last_verdict,
            "last_command": self._last_command,
            "last_alarms": list(self._last_alarms),
            "last_reason": self._last_reason,
        }
        self._store.put(CONTROL_KEY, json.dumps(payload))

    def evaluate_manual(self, at: float) -> str:
        """Re-evaluate a value written outside the sampling pipeline."""

        if self._status != FAULT:
            command, _alarms = self._evaluate(at)
            if command != "none":
                self._last_command = command
            self._last_verdict = (
                WARNING if self._status == WARN else (
                    EXCEEDED if self._status == OVER else OK
                )
            )
            self._save_control()
        return self._status

    def counters(self) -> tuple[int, int]:
        return self._accepted_total, self._rejected_total

    def _result(
        self,
        accepted: bool,
        reason: str,
        learned: bool,
        steady: bool,
        command: str,
        alarms: tuple[str, ...],
    ) -> SampleResult:
        value, quality, confidence, basis = self.baseline_value()
        verdict = alarms[0] if alarms else (
            WARNING if self._status == WARN else (SENSOR_FAULT if self._status == FAULT else OK)
        )
        if accepted and self._status == OVER and not alarms:
            verdict = EXCEEDED
        return SampleResult(
            accepted=accepted,
            reason=reason,
            learned=learned,
            steady=steady,
            status=self._status,
            verdict=verdict,
            temperature=self.temperature(),
            ambient=self.ambient(),
            baseline=value,
            threshold=value + self._config.over_margin,
            residual=self.temperature() - value,
            quality=BAD if self._status == FAULT else quality,
            duty=self.duty(),
            standby=self._fans.healthy_standby(),
            command=command,
            alarms=alarms,
        )
