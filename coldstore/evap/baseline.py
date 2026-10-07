"""Daily sliding baseline corrected for ambient temperature.

The condenser baseline used to be a number an operator wrote down by hand, so
it fit neither the summer nor the winter, and sampling noise dragged it off.
The adaptive baseline learns how the healthy condenser temperature follows the
ambient temperature with a regression over the trailing day:

baseline(ambient) = anchor + slope * (ambient - reference)

Samples land in one of 24 hourly buckets. Only the bucket aggregates are
persisted (count and four sums), so accepting one sample is a constant time
update and refitting the regression always costs O(24), no matter how many
samples the high frequency feed delivered during the day.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from coldstore.store.epoch import load_float, save_float
from coldstore.store.store import Store

BUCKETS_KEY = "evap:baseline-buckets"
SLOPE_KEY = "evap:baseline-slope"
ANCHOR_KEY = "evap:baseline"
LAST_AMBIENT_KEY = "evap:ambient:last"
LAST_AT_KEY = "evap:baseline-last-at"

DEFAULT_ANCHOR = 32.0
DEFAULT_SLOPE = 0.0
DEFAULT_REFERENCE = 25.0
BUCKET_COUNT = 24
MIN_SAMPLES = 24
MIN_BUCKETS = 6
MIN_SAMPLE_INTERVAL = 30
MAX_SLOPE = 1.0


@dataclass(frozen=True)
class Bucket:
    """Aggregated samples for one hour of the day."""

    hour: int
    count: int
    sum_x: float
    sum_y: float
    sum_xx: float
    sum_xy: float

    def as_dict(self) -> dict[str, object]:
        return {
            "hour": self.hour,
            "count": self.count,
            "sum_x": self.sum_x,
            "sum_y": self.sum_y,
            "sum_xx": self.sum_xx,
            "sum_xy": self.sum_xy,
        }

    @classmethod
    def from_dict(cls, payload: object) -> Bucket | None:
        if not isinstance(payload, dict):
            return None
        try:
            hour = int(payload["hour"])
            count = int(payload["count"])
            bucket = cls(
                hour=hour,
                count=count,
                sum_x=float(payload["sum_x"]),
                sum_y=float(payload["sum_y"]),
                sum_xx=float(payload["sum_xx"]),
                sum_xy=float(payload["sum_xy"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if not 0 <= bucket.hour or bucket.count < 0:
            return None
        return bucket


class BaselineTracker:
    """Daily sliding regression of condenser temperature on ambient temperature."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def anchor(self) -> float:
        value, present = load_float(self._store, ANCHOR_KEY)
        return value if present else DEFAULT_ANCHOR

    def slope(self) -> float:
        value, present = load_float(self._store, SLOPE_KEY)
        return value if present else DEFAULT_SLOPE

    def reference(self) -> float:
        return DEFAULT_REFERENCE

    def last_ambient(self) -> float:
        value, _ = load_float(self._store, LAST_AMBIENT_KEY)
        return value

    def last_sample_at(self) -> int:
        raw, present = self._store.get(LAST_AT_KEY)
        if not present:
            return 0
        try:
            return int(raw)
        except ValueError:
            return 0

    def baseline_for(self, ambient: float) -> float:
        """Temperature corrected baseline for an ambient reading."""

        return self.anchor() + self.slope() * (ambient - DEFAULT_REFERENCE)

    def effective_baseline(self) -> float:
        """Baseline for the most recently seen ambient, used by reports."""

        return self.baseline_for(self.last_ambient())

    def sample_count(self) -> int:
        return sum(bucket.count for bucket in self._buckets() if bucket is not None)

    def covered_buckets(self) -> int:
        return sum(
            1 for bucket in self._buckets() if bucket is not None and bucket.count > 0
        )

    def learned(self) -> bool:
        return self.sample_count() >= MIN_SAMPLES and self.covered_buckets() >= MIN_BUCKETS

    def accept(self, at: int | None = None) -> bool:
        """Whether a high frequency sample may enter the baseline feed.

        The feed is limited to one accepted sample per
        ``MIN_SAMPLE_INTERVAL`` seconds so bursts of noisy high frequency
        traffic can neither dominate a bucket nor run up the refit cost.
        """

        moment = int(at) if at is not None else int(time.time())
        if not self._store.get(LAST_AT_KEY)[1]:
            return True
        last = self.last_sample_at()
        if moment - last < MIN_SAMPLE_INTERVAL:
            return False
        return True

    def observe(
        self, ambient: float, temperature: float, at: int | None = None
    ) -> tuple[bool, float]:
        """Add one rate limited sample and refit the daily regression.

        Returns whether the sample was accepted alongside the corrected
        baseline for the ambient reading.
        """

        moment = int(at) if at is not None else int(time.time())
        if not self.accept(moment):
            return False, self.baseline_for(ambient)

        hour = moment // 3600
        buckets = self._buckets()
        index = hour % BUCKET_COUNT
        existing = buckets[index]
        if existing is None or existing.hour != hour:
            existing = Bucket(
                hour=hour, count=0, sum_x=0.0, sum_y=0.0, sum_xx=0.0, sum_xy=0.0
            )
        buckets[index] = Bucket(
            hour=hour,
            count=existing.count + 1,
            sum_x=existing.sum_x + ambient,
            sum_y=existing.sum_y + temperature,
            sum_xx=existing.sum_xx + ambient * ambient,
            sum_xy=existing.sum_xy + ambient * temperature,
        )
        buckets = self._roll_buckets(buckets, hour)
        self._save_buckets(buckets)
        save_float(self._store, LAST_AMBIENT_KEY, ambient)
        self._store.put(LAST_AT_KEY, str(moment))
        self._refit(buckets)
        return True, self.baseline_for(ambient)

    def _roll_buckets(self, buckets: list[Bucket | None], current_hour: int) -> list[Bucket | None]:
        """Drop buckets older than the trailing 24 hour window.

        The window is closed on both ends: a bucket stamped exactly a day
        before the incoming sample is still live (its slot is about to be
        reopened for the new hour).
        """

        oldest = current_hour - BUCKET_COUNT
        return [
            bucket if bucket is not None and bucket.hour >= oldest else None
            for bucket in buckets
        ]

    def set_anchor(self, value: float) -> float:
        """Manually seed the baseline at the reference ambient and relearn.

        The daily aggregates are cleared so the operator's value cannot be
        blended with samples gathered before it was written.
        """

        save_float(self._store, ANCHOR_KEY, value)
        save_float(self._store, SLOPE_KEY, DEFAULT_SLOPE)
        self._store.delete(BUCKETS_KEY)
        return value

    def reset(self) -> None:
        self._store.delete(BUCKETS_KEY)
        self._store.delete(SLOPE_KEY)
        self._store.delete(LAST_AMBIENT_KEY)
        self._store.delete(LAST_AT_KEY)

    def _refit(self, buckets: list[Bucket | None]) -> None:
        live = [bucket for bucket in buckets if bucket is not None and bucket.count > 0]
        total = sum(bucket.count for bucket in live)
        if total < MIN_SAMPLES or len(live) < MIN_BUCKETS:
            return
        sum_n = float(total)
        sum_x = sum(bucket.sum_x for bucket in live)
        sum_y = sum(bucket.sum_y for bucket in live)
        sum_xx = sum(bucket.sum_xx for bucket in live)
        sum_xy = sum(bucket.sum_xy for bucket in live)
        denominator = sum_n * sum_xx - sum_x * sum_x
        if denominator <= 0:
            return
        slope = (sum_n * sum_xy - sum_x * sum_y) / denominator
        slope = min(MAX_SLOPE, max(0.0, slope))
        mean_x = sum_x / sum_n
        mean_y = sum_y / sum_n
        anchor = mean_y - slope * (mean_x - DEFAULT_REFERENCE)
        save_float(self._store, SLOPE_KEY, slope)
        save_float(self._store, ANCHOR_KEY, anchor)

    def _buckets(self) -> list[Bucket | None]:
        raw, present = self._store.get(BUCKETS_KEY)
        if not present:
            return [None] * BUCKET_COUNT
        try:
            parsed = json.loads(raw)
        except ValueError:
            return [None] * BUCKET_COUNT
        if not isinstance(parsed, list):
            return [None] * BUCKET_COUNT
        buckets: list[Bucket | None] = [None] * BUCKET_COUNT
        for item in parsed:
            bucket = Bucket.from_dict(item)
            if bucket is not None:
                buckets[bucket.hour % BUCKET_COUNT] = bucket
        return buckets

    def _save_buckets(self, buckets: list[Bucket | None]) -> None:
        payload = [bucket.as_dict() for bucket in buckets if bucket is not None]
        self._store.put(BUCKETS_KEY, json.dumps(payload))
