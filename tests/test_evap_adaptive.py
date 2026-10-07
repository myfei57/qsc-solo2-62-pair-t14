"""Adaptive evaporative baseline: rate limiting, learning and duty control."""

from __future__ import annotations

import json
import tempfile
import unittest

from coldstore.audit import Auditor
from coldstore.evap import (
    Condenser,
    CondenserController,
    FAULT,
    NORMAL,
    OVER,
    WARN,
    TemperatureBaseline,
    load_config,
)
from coldstore.evap.baseline import RateLimiter, SteadyGate
from coldstore.fan import PRIMARY, STANDBY, FanSet
from coldstore.store import Store


def fresh_parts(path: str) -> tuple[Store, FanSet]:
    store = Store.open(path)
    fans = FanSet()
    fans.add_fan("e1", PRIMARY)
    fans.add_fan("e2", STANDBY)
    fans.start("e1")
    return store, fans


class RateLimiterCase(unittest.TestCase):
    def test_burst_then_throttle(self) -> None:
        from coldstore.evap.baseline import BaselineConfig

        config = BaselineConfig(min_interval=1.0, burst=1.0)
        limiter = RateLimiter(tokens=0.0)
        self.assertTrue(limiter.allow(0.0, config))
        self.assertFalse(limiter.allow(0.2, config))
        # one interval later a token has refilled
        self.assertTrue(limiter.allow(1.3, config))


class BinnedBaselineCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fast_config(self) -> None:
        # short memory so a test can observe hourly forgetting
        self.store.put(
            "evap:config",
            json.dumps(
                load_config(self.store).as_dict()
                | {"halflife_hours": 2.0, "n_full": 5.0, "warmup_samples": 2.0}
            ),
        )

    def test_summer_and_winter_bins_never_mix(self) -> None:
        self._fast_config()
        baseline = TemperatureBaseline(self.store)
        # winter ambient -2 C -> condensing near 26 C
        for i in range(8):
            accepted, _ = baseline.consider(float(i), -2.0 + (i % 2) * 0.3, 26.0)
            if accepted:
                baseline.learn(float(i), -2.0 + (i % 2) * 0.3, 26.0)
        # summer ambient 32 C -> condensing near 38 C
        for i in range(8, 16):
            ambient = 32.0 + (i % 2) * 0.3
            accepted, _ = baseline.consider(float(i), ambient, 38.0)
            if accepted:
                baseline.learn(float(i), ambient, 38.0)
        winter = baseline.query(-2.0, 32.0)
        summer = baseline.query(32.0, 32.0)
        self.assertAlmostEqual(winter.value, 26.0, delta=0.4)
        self.assertAlmostEqual(summer.value, 38.0, delta=0.4)
        self.assertEqual(winter.quality, "good")

    def test_spike_is_rejected_and_does_not_drag_baseline(self) -> None:
        self._fast_config()
        baseline = TemperatureBaseline(self.store)
        for i in range(10):
            accepted, _ = baseline.consider(float(i), 20.0, 30.0)
            self.assertTrue(accepted)
            baseline.learn(float(i), 20.0, 30.0)
        accepted, reason = baseline.consider(20.0, 20.0, 80.0)
        self.assertFalse(accepted)
        self.assertEqual(reason, "spike")
        self.assertAlmostEqual(baseline.query(20.0, 32.0).value, 30.0, delta=0.5)

    def test_time_decay_forgets_old_regime(self) -> None:
        self._fast_config()
        baseline = TemperatureBaseline(self.store)
        for i in range(10):
            accepted, _ = baseline.consider(float(i), 20.0, 30.0)
            if accepted:
                baseline.learn(float(i), 20.0, 30.0)
        # jump several half lives forward, then learn a new regime densely
        t0 = 4.0 * 3600.0
        for i in range(20):
            at = t0 + i * 1800.0
            accepted, _ = baseline.consider(at, 20.0, 34.0)
            if accepted:
                baseline.learn(at, 20.0, 34.0)
        self.assertAlmostEqual(baseline.query(20.0, 32.0, t0 + 20 * 1800).value, 34.0, delta=0.6)

    def test_persists_across_instances(self) -> None:
        self._fast_config()
        baseline = TemperatureBaseline(self.store)
        for i in range(8):
            accepted, _ = baseline.consider(float(i), 20.0, 30.0)
            if accepted:
                baseline.learn(float(i), 20.0, 30.0)
        reloaded = TemperatureBaseline(self.store)
        self.assertGreater(reloaded.bin_count(), 0)
        self.assertAlmostEqual(reloaded.query(20.0, 32.0).value, 30.0, delta=0.5)


class SteadyGateCase(unittest.TestCase):
    def test_ramp_and_fan_level_change_rejected(self) -> None:
        from coldstore.evap.baseline import BaselineConfig

        config = BaselineConfig()
        gate = SteadyGate()
        index = 14
        steady, _ = gate.is_steady(0.0, 25.0, 30.0, 1, config, index)
        self.assertTrue(steady)
        gate.remember(0.0, 25.0, 30.0, 1, index)
        # ambient jumps 5 C in 60 s inside the same bin -> unsteady
        steady, _ = gate.is_steady(60.0, 30.0, 30.0, 1, config, index)
        self.assertFalse(steady)
        gate.remember(60.0, 30.0, 30.0, 1, index)
        # calm temperatures but fan level changed -> unsteady
        steady, _ = gate.is_steady(120.0, 30.0, 30.0, 2, config, index)
        self.assertFalse(steady)

    def test_new_ambient_regime_starts_fresh(self) -> None:
        from coldstore.evap.baseline import BaselineConfig

        config = BaselineConfig()
        gate = SteadyGate()
        gate.remember(0.0, -2.0, 26.0, 1, bin_index=3)
        # switching to the summer bin is not a ramp within that bin
        steady, _ = gate.is_steady(60.0, 33.0, 38.5, 1, config, bin_index=17)
        self.assertTrue(steady)


def feed(
    controller: CondenserController,
    times: list[float],
    ambient: float,
    temperature: float,
    fan_level: int = 1,
):
    results = []
    for at in times:
        results.append(controller.sample(at, ambient, temperature, fan_level))
    return results


class ControllerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store, self.fans = fresh_parts(f"{self._tmp.name}/state.json")
        self.store.put(
            "evap:config",
            json.dumps(
                load_config(self.store).as_dict()
                | {
                    "halflife_hours": 1.0,
                    "n_full": 4.0,
                    "warmup_samples": 2.0,
                    "warn_margin": 2.0,
                    "over_margin": 4.0,
                    "hysteresis": 0.5,
                    "over_debounce": 3,
                    "over_alarm_after": 10,
                    "stale_seconds": 120,
                    "min_interval": 1.0,
                }
            ),
        )
        self.controller = CondenserController(self.store, self.fans)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _train(self, ambient: float, temperature: float, start: float = 0.0) -> None:
        feed(self.controller, [start + i for i in range(8)], ambient, temperature)

    def test_rate_limited_samples_are_counted(self) -> None:
        r1 = self.controller.sample(0.0, 25.0, 30.0)
        r2 = self.controller.sample(0.9, 25.0, 30.0)
        self.assertTrue(r1.accepted)
        self.assertFalse(r2.accepted)
        self.assertEqual(r2.reason, "rate-limited")
        accepted, rejected = self.controller.counters()
        self.assertEqual(accepted, 1)
        self.assertEqual(rejected, 1)

    def test_over_limit_brings_standby_online_then_promotes_on_recovery(self) -> None:
        self._train(25.0, 30.0)
        self.assertEqual(self.controller.status(), NORMAL)
        # residual 5 > over margin 4; debounce 3 s. The excursion point is
        # statistically wild, so it is not learned, but it still trips the
        # verdict and drives the fan response.
        results = feed(self.controller, [10.0, 11.0, 12.0, 13.0], 25.0, 35.0)
        self.assertEqual(self.controller.status(), OVER)
        self.assertIn("parallel:e2", [r.command for r in results])
        self.assertTrue(all(not r.learned for r in results))
        self.assertEqual(sorted(self.fans.running_ids()), ["e1", "e2"])
        # still over after alarm window -> persistent alarm
        later = self.controller.sample(30.0, 25.0, 35.0)
        self.assertIn("persistent-exceeded", later.alarms)
        # recover below warn margin minus hysteresis -> old fan stopped, e2 duty
        recovered = self.controller.sample(40.0, 25.0, 31.0)
        self.assertEqual(recovered.command, "promote:e2")
        self.assertEqual(self.fans.running_ids(), ["e2"])
        self.assertEqual(self.fans.primary(), "e2")
        self.assertEqual(self.controller.status(), WARN)

    def test_unsteady_samples_judge_but_never_learn(self) -> None:
        self._train(25.0, 30.0)
        # 2 C ambient jump within the same bin over 60 s -> ramp, rejected
        result = self.controller.sample(100.0, 27.0, 30.0, 1)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "unsteady")
        self.assertFalse(result.learned)

    def test_no_standby_reports_unavailable(self) -> None:
        self.fans.fail("e2")
        self._train(25.0, 30.0)
        results = feed(self.controller, [10.0, 11.0, 12.0, 13.0], 25.0, 35.0)
        self.assertEqual(self.controller.status(), OVER)
        self.assertEqual([r.command for r in results][-1], "unavailable")

    def test_stale_sensor_fails_safe_on_tick(self) -> None:
        self._train(25.0, 30.0)
        tick = self.controller.tick(1000.0)
        self.assertEqual(self.controller.status(), FAULT)
        self.assertIn("sensor-fault", tick["alarms"])
        self.assertIn("failsafe:e2", tick["actions"])
        self.assertEqual(sorted(self.fans.running_ids()), ["e1", "e2"])
        # fresh sample alone does not clear; operator acknowledgement does
        self.controller.sample(1001.0, 25.0, 30.0)
        self.assertEqual(self.controller.status(), FAULT)
        self.assertEqual(self.controller.clear_fault(1002.0), "cleared")
        self.assertEqual(self.controller.status(), NORMAL)

    def test_weekly_rotation_only_when_normal(self) -> None:
        self.controller.set_config({"stale_seconds": 3600})
        self._train(25.0, 30.0, start=1000.0)
        week = 7 * 24 * 3600
        # fresh sample right before the weekly tick so the staleness failsafe
        # does not mask the rotation action
        self.controller.sample(float(1000 + week - 10), 25.0, 30.0)
        tick = self.controller.tick(float(1000 + week))
        self.assertIn("rotate:e2", tick["actions"])
        self.assertEqual(self.controller.duty(), "e2")
        # an over-limit state blocks rotation until normal service resumes
        self.controller.set_config(
            {"rotate_interval": 1, "stale_seconds": 10 ** 9, "over_margin": 3.0}
        )
        self._train(26.0, 36.0, start=500000.0)
        self.assertEqual(self.controller.status(), OVER)
        blocked = self.controller.tick(500002.0)
        self.assertFalse(any(a.startswith("rotate:") for a in blocked["actions"]))


class CondenserFacadeCase(unittest.TestCase):
    def test_telemetry_report_and_audit_share_one_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, fans = fresh_parts(f"{tmp}/state.json")
            store.put(
                "evap:config",
                json.dumps(
                    load_config(store).as_dict()
                    | {
                        "halflife_hours": 1.0,
                        "n_full": 4.0,
                        "warmup_samples": 2.0,
                        "stale_seconds": 7200,
                    }
                ),
            )
            auditor = Auditor(store)
            condenser = Condenser(store, fans)
            for i in range(8):
                result = condenser.controller.sample(float(i), 25.0, 30.0)
            self.assertTrue(result.accepted)
            state = condenser.state()
            self.assertAlmostEqual(state.baseline, 30.0, delta=0.5)
            self.assertEqual(state.basis, "learned")

            # hourly audit snapshot and live state carry identical figures
            tick_payload = condenser.controller.tick(3600.0)
            auditor.record(
                "evap.baseline-hour", json.dumps(condenser.state().as_dict(), default=str)
            )
            entry = auditor.last()
            logged = json.loads(entry.detail)
            self.assertAlmostEqual(float(logged["baseline"]), state.baseline, places=4)
            self.assertEqual(logged["status"], state.status)
            self.assertEqual(tick_payload["sliding"], "hourly")

    def test_manual_fallback_is_served_until_curve_warms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, fans = fresh_parts(f"{tmp}/state.json")
            condenser = Condenser(store, fans)
            condenser.set_baseline(31.5)
            state = condenser.state()
            self.assertEqual(state.baseline, 31.5)
            self.assertEqual(state.basis, "fallback")
            self.assertEqual(state.baseline_quality, "cold")


if __name__ == "__main__":
    unittest.main()
