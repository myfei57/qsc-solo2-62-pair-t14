"""Adaptive evaporative condenser baseline.

The baseline is a curve over ambient temperature, not one manually typed
number: the expected condensing temperature is learned independently inside
small ambient temperature bins, so summer and winter samples never mix.

Cost control:

* samples pass a token bucket before any work is done;
* each bin keeps constant size EWMA statistics (O(1) memory per bin, O(B)
  per hourly tick, B is the fixed bin count);
* decay is time aware, so no per sample scan of a sliding window is needed.

Only healthy steady state samples are learned. A sample that is too far from
its bin is treated as a spike and dropped instead of being allowed to drag the
baseline with it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from coldstore.store.store import Store

CONFIG_KEY = "evap:config"
BINS_KEY = "evap:bins"

# Hourly sliding: one adaptation tick per hour, fourteen day memory per bin.
DEFAULT_HALFLIFE_HOURS = 14.0 * 24.0
DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_BURST = 2

AMBIENT_LOW = -10.0
AMBIENT_HIGH = 45.0
BIN_WIDTH = 2.5
N_FULL = 40.0

SPIKE_SIGMA = 4.0
SIGMA_FLOOR = 0.3
WARMUP_SAMPLES = 8

AMBIENT_SLOPE_MAX = 10.0 / 3600.0   # ~10 C/h of ambient movement
TEMP_SLOPE_MAX = 20.0 / 3600.0      # ~20 C/h of condenser movement

WARN_MARGIN = 2.0
OVER_MARGIN = 4.0
HYSTERESIS = 0.5
OVER_DEBOUNCE_SECONDS = 60
OVER_ALARM_AFTER = 300
STALE_SECONDS = 120
OVERLAP_SECONDS = 30
ROTATE_INTERVAL_SECONDS = 7 * 24 * 3600

AMBIENT_RANGE = (-40.0, 60.0)
TEMPERATURE_RANGE = (-50.0, 80.0)

GOOD = "good"
SUSPECT = "suspect"
COLD = "cold"
BAD = "bad"


@dataclass(frozen=True)
class BaselineConfig:
    """Tunable parameters of the adaptive baseline and its verdict."""

    min_interval: float = DEFAULT_MIN_INTERVAL
    burst: float = DEFAULT_BURST
    halflife_hours: float = DEFAULT_HALFLIFE_HOURS
    bin_low: float = AMBIENT_LOW
    bin_high: float = AMBIENT_HIGH
    bin_width: float = BIN_WIDTH
    n_full: float = N_FULL
    spike_sigma: float = SPIKE_SIGMA
    sigma_floor: float = SIGMA_FLOOR
    warmup_samples: float = WARMUP_SAMPLES
    ambient_slope_max: float = AMBIENT_SLOPE_MAX
    temp_slope_max: float = TEMP_SLOPE_MAX
    warn_margin: float = WARN_MARGIN
    over_margin: float = OVER_MARGIN
    hysteresis: float = HYSTERESIS
    over_debounce: int = OVER_DEBOUNCE_SECONDS
    over_alarm_after: int = OVER_ALARM_AFTER
    stale_seconds: int = STALE_SECONDS
    overlap_seconds: int = OVERLAP_SECONDS
    rotate_interval: int = ROTATE_INTERVAL_SECONDS

    def as_dict(self) -> dict[str, object]:
        return {
            "min_interval": self.min_interval,
            "burst": self.burst,
            "halflife_hours": self.halflife_hours,
            "bin_low": self.bin_low,
            "bin_high": self.bin_high,
            "bin_width": self.bin_width,
            "n_full": self.n_full,
            "spike_sigma": self.spike_sigma,
            "sigma_floor": self.sigma_floor,
            "warmup_samples": self.warmup_samples,
            "ambient_slope_max": self.ambient_slope_max,
            "temp_slope_max": self.temp_slope_max,
            "warn_margin": self.warn_margin,
            "over_margin": self.over_margin,
            "hysteresis": self.hysteresis,
            "over_debounce": self.over_debounce,
            "over_alarm_after": self.over_alarm_after,
            "stale_seconds": self.stale_seconds,
            "overlap_seconds": self.overlap_seconds,
            "rotate_interval": self.rotate_interval,
            "sliding": "hourly",
        }


_CONFIG_FIELDS = tuple(
    name for name in BaselineConfig().as_dict() if name != "sliding"
)


def load_config(store: Store) -> BaselineConfig:
    raw, present = store.get(CONFIG_KEY)
    if not present:
        return BaselineConfig()
    try:
        payload = json.loads(raw)
    except ValueError:
        return BaselineConfig()
    if not isinstance(payload, dict):
        return BaselineConfig()
    values: dict[str, object] = {}
    for field_name in _CONFIG_FIELDS:
        if field_name in payload:
            values[field_name] = payload[field_name]
    try:
        return BaselineConfig(**values)  # type: ignore[arg-type]
    except TypeError:
        return BaselineConfig()


def update_config(store: Store, changes: dict[str, object]) -> BaselineConfig:
    current = load_config(store).as_dict()
    for key, value in changes.items():
        if key == "sliding":
            continue
        if key not in _CONFIG_FIELDS:
            raise ValueError(f"unknown config field {key}")
        current[key] = value
    current.pop("sliding", None)
    config = BaselineConfig(**current)  # type: ignore[arg-type]
    _validate_config(config)
    store.put(CONFIG_KEY, json.dumps(config.as_dict()))
    return config


def _validate_config(config: BaselineConfig) -> None:
    if config.min_interval <= 0:
        raise ValueError("min_interval must be positive")
    if config.burst < 1:
        raise ValueError("burst must be at least 1")
    if config.halflife_hours <= 0:
        raise ValueError("halflife_hours must be positive")
    if config.bin_width <= 0 or config.bin_high <= config.bin_low:
        raise ValueError("temperature bin range is invalid")
    if config.n_full <= 0:
        raise ValueError("n_full must be positive")
    if config.warn_margin <= 0 or config.over_margin <= config.warn_margin:
        raise ValueError("over_margin must exceed warn_margin, both positive")
    if config.hysteresis < 0:
        raise ValueError("hysteresis must not be negative")
    if config.over_debounce < 0 or config.over_alarm_after < 0:
        raise ValueError("timer settings must not be negative")
    if config.stale_seconds <= 0 or config.overlap_seconds < 0:
        raise ValueError("stale and overlap settings are invalid")


class RateLimiter:
    """Token bucket in front of the sampling pipeline.

    High frequency feeds are shaped to at most one sample per configured
    interval; the small burst allowance absorbs a legitimate quick burst
    without admitting a noisy stream into the baseline.
    """

    def __init__(self, tokens: float = 0.0, last_at: float = 0.0) -> None:
        self.tokens = tokens
        self.last_at = last_at
        self.primed = last_at > 0.0

    def allow(self, at: float, config: BaselineConfig) -> bool:
        if self.primed:
            refill = (at - self.last_at) / config.min_interval
            self.tokens = min(config.burst, self.tokens + max(0.0, refill))
        else:
            # Cold start admits the first sample; the rest of the burst is
            # headroom for later quick bursts, not a startup flood.
            self.tokens = 1.0
            self.primed = True
        self.last_at = at
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def as_dict(self) -> dict[str, float]:
        return {"tokens": self.tokens, "last_at": self.last_at}


class SteadyGate:
    """Accepts a sample only while ambient and process temperatures are calm.

    A ramp (startup, door open, stage change) moves the condenser for reasons
    the baseline must not learn, so samples with steep slopes or a changed fan
    level are rejected as transient.
    """

    def __init__(self, previous: dict[str, object] | None = None) -> None:
        # Keyed by bin index: comparing this sample against the last point in
        # the *same* ambient regime avoids a winter -> summer switch being
        # mistaken for a ramp that must never be learned.
        self.memory: dict[int, dict[str, object]] = {}
        if isinstance(previous, dict) and previous:
            try:
                self.memory = {
                    int(index): value
                    for index, value in previous.items()
                    if isinstance(value, dict)
                }
            except (TypeError, ValueError):
                self.memory = {}

    def is_steady(
        self,
        at: float,
        ambient: float,
        temperature: float,
        fan_level: int,
        config: BaselineConfig,
        bin_index: int,
    ) -> tuple[bool, dict[str, float]]:
        previous = self.memory.get(bin_index)
        if not previous:
            return True, {"ambient_slope": 0.0, "temp_slope": 0.0}
        elapsed = at - float(previous["at"])
        if elapsed <= 0:
            return False, {"ambient_slope": 0.0, "temp_slope": 0.0}
        ambient_slope = abs(ambient - float(previous["ambient"])) / elapsed
        temp_slope = abs(temperature - float(previous["temperature"])) / elapsed
        level_changed = fan_level != int(previous["fan_level"])
        steady = (
            ambient_slope <= config.ambient_slope_max
            and temp_slope <= config.temp_slope_max
            and not level_changed
        )
        return steady, {"ambient_slope": ambient_slope, "temp_slope": temp_slope}

    def remember(
        self, at: float, ambient: float, temperature: float, fan_level: int, bin_index: int
    ) -> None:
        self.memory[bin_index] = {
            "at": at,
            "ambient": ambient,
            "temperature": temperature,
            "fan_level": fan_level,
        }

    def as_dict(self) -> dict[str, dict[str, object]]:
        return {str(index): value for index, value in self.memory.items()}


@dataclass
class _Bin:
    """Constant size EWMA statistics for one ambient temperature bin."""

    mean: float
    variance: float
    effective: float
    last_at: float

    def as_dict(self) -> dict[str, float]:
        return {
            "mean": self.mean,
            "variance": self.variance,
            "effective": self.effective,
            "last_at": self.last_at,
        }


@dataclass(frozen=True)
class BaselineView:
    """The baseline value served for one ambient temperature."""

    value: float
    quality: str
    confidence: float
    basis: str
    bins_used: int

    def as_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "quality": self.quality,
            "confidence": self.confidence,
            "basis": self.basis,
            "bins_used": self.bins_used,
        }


class TemperatureBaseline:
    """Ambient binned, time decayed expected condenser temperature."""

    def __init__(self, store: Store, config: BaselineConfig | None = None) -> None:
        self._store = store
        self._config = config or load_config(store)
        self._bins = self._load()

    @property
    def config(self) -> BaselineConfig:
        return self._config

    def refresh_config(self) -> BaselineConfig:
        self._config = load_config(self._store)
        return self._config

    def bin_count(self) -> int:
        return len(self._bins)

    def reset(self) -> None:
        self._bins = {}
        self._store.delete(BINS_KEY)

    def _load(self) -> dict[int, _Bin]:
        raw, present = self._store.get(BINS_KEY)
        if not present:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(payload, dict):
            return {}
        bins: dict[int, _Bin] = {}
        for key, item in payload.items():
            if not isinstance(item, dict):
                continue
            try:
                bins[int(key)] = _Bin(
                    mean=float(item["mean"]),
                    variance=float(item["variance"]),
                    effective=float(item["effective"]),
                    last_at=float(item["last_at"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
        return bins

    def _save(self) -> None:
        payload = {str(index): bin_.as_dict() for index, bin_ in sorted(self._bins.items())}
        self._store.put(BINS_KEY, json.dumps(payload))

    def bin_index(self, ambient: float) -> int:
        return self._bin_index(ambient)

    def _bin_index(self, ambient: float) -> int:
        clamped = min(max(ambient, self._config.bin_low), self._config.bin_high - 0.0001)
        return int((clamped - self._config.bin_low) // self._config.bin_width)

    def _alpha(self, elapsed: float) -> float:
        halflife = self._config.halflife_hours * 3600.0
        return 1.0 - 0.5 ** (max(0.0, elapsed) / halflife)

    def consider(
        self, at: float, ambient: float, temperature: float
    ) -> tuple[bool, str]:
        """Apply the statistical quality gate to one sample.

        Returns ``(accepted, reason)`` without touching stored statistics;
        call :meth:`learn` only when the caller approves the sample.
        """

        index = self._bin_index(ambient)
        bin_ = self._bins.get(index)
        if bin_ is None:
            effective_now = 0.0
        else:
            effective_now = bin_.effective * (1.0 - self._alpha(max(0.0, at - bin_.last_at)))
        if effective_now < self._config.warmup_samples:
            # Never learned, or the history has aged out across a long gap:
            # bootstrap instead of rejecting a shifted regime forever.
            return True, "warmup"
        delta = temperature - bin_.mean
        # ``sigma_floor`` is both a robust minimum spread and the outlier
        # band: an almost flat history must not collapse the threshold.
        spread = max(self._config.sigma_floor, bin_.variance**0.5)
        if abs(delta) > self._config.spike_sigma * spread:
            return False, "spike"
        return True, "ok"

    def learn(self, at: float, ambient: float, temperature: float) -> None:
        """Fold an accepted sample into its bin statistics."""

        self._update(self._bin_index(ambient), at, temperature)

    def _update(self, index: int, at: float, temperature: float) -> None:
        bin_ = self._bins.get(index)
        if bin_ is None:
            self._bins[index] = _Bin(
                mean=temperature, variance=0.0, effective=1.0, last_at=at
            )
        else:
            alpha = self._alpha(at - bin_.last_at)
            delta = temperature - bin_.mean
            bin_.mean += alpha * delta
            bin_.variance = (1.0 - alpha) * (bin_.variance + alpha * delta * delta)
            bin_.effective = bin_.effective * (1.0 - alpha) + 1.0
            bin_.last_at = at
        self._save()

    def query(self, ambient: float, fallback: float, at: float = 0.0) -> BaselineView:
        """Linearly interpolate between neighboring confident bins.

        Confidence blends toward the fallback (the last manually set baseline
        or the factory value) until the curve has enough history. The same
        queried value is the only number telemetry, reports and audits read.
        """

        low = self._config.bin_low
        high = self._config.bin_high
        width = self._config.bin_width
        position = (ambient - low) / width
        index = int(position)
        fraction = position - index
        clamped_out = ambient < low or ambient > high

        left = self._bins.get(index)
        right = self._bins.get(index + 1)

        def confidence(bin_: _Bin | None) -> float:
            if bin_ is None:
                return 0.0
            effective = bin_.effective
            if at and bin_.last_at:
                effective *= 0.5 ** (
                    max(0.0, at - bin_.last_at) / (self._config.halflife_hours * 3600.0)
                )
            return min(1.0, effective / self._config.n_full)

        left_conf = confidence(left)
        right_conf = confidence(right)
        used = int(left_conf > 0) + int(right_conf > 0)

        if left is not None and right is not None:
            learned = (
                left.mean * (1.0 - fraction) + right.mean * fraction
            )
            learned_conf = left_conf * (1.0 - fraction) + right_conf * fraction
        elif left is not None:
            learned, learned_conf = left.mean, left_conf
        elif right is not None:
            learned, learned_conf = right.mean, right_conf
        else:
            return BaselineView(value=fallback, quality=COLD, confidence=0.0, basis="fallback", bins_used=0)

        value = fallback * (1.0 - learned_conf) + learned * learned_conf
        if learned_conf >= 0.95 and not clamped_out:
            quality, basis = GOOD, "learned"
        elif learned_conf >= 0.5:
            quality, basis = SUSPECT, "blended"
        else:
            quality, basis = COLD, "fallback"
        if clamped_out:
            quality = SUSPECT
        return BaselineView(
            value=value, quality=quality, confidence=learned_conf, basis=basis, bins_used=used
        )
