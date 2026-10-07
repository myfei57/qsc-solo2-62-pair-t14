"""Component health checks."""

from __future__ import annotations

from .runtime import Runtime


def run_checks(rt: Runtime) -> list[dict[str, str]]:
    """Return one check row per mutable component."""

    checks = [{"name": "store", "status": "ok", "detail": f"keys={rt.store.count()}"}]

    if rt.interlock.is_tripped():
        checks.append(
            {"name": "interlock", "status": "fail", "detail": rt.interlock.reason() or "tripped"}
        )
    else:
        checks.append({"name": "interlock", "status": "ok", "detail": "not latched"})

    if rt.ammonia.latched():
        checks.append({"name": "ammonia", "status": "fail", "detail": "trip latched"})
    elif rt.ammonia.threshold() <= 0:
        checks.append({"name": "ammonia", "status": "fail", "detail": "threshold must be positive"})
    else:
        checks.append({"name": "ammonia", "status": "ok", "detail": "threshold set"})

    if rt.temp.within_window():
        checks.append({"name": "temp", "status": "ok", "detail": "rate inside the window"})
    else:
        checks.append({"name": "temp", "status": "warn", "detail": "rate outside the window"})

    if rt.compressors.count() == 0:
        checks.append({"name": "compressors", "status": "warn", "detail": "no compressor registered"})
    elif rt.compressors.load_ratio() >= 1.0:
        checks.append({"name": "compressors", "status": "warn", "detail": "fully staged"})
    else:
        checks.append({"name": "compressors", "status": "ok", "detail": "headroom available"})

    if rt.cold_fans.count() == 0:
        checks.append({"name": "fans", "status": "warn", "detail": "no fan registered"})
    elif rt.cold_fans.running_ids():
        checks.append({"name": "fans", "status": "ok", "detail": "fan running"})
    else:
        checks.append({"name": "fans", "status": "warn", "detail": "no fan running"})

    if rt.evap.is_high():
        checks.append(
            {
                "name": "evap",
                "status": "warn",
                "detail": (
                    f"{rt.evap.state().status} baseline={rt.evap.baseline():.4f} "
                    f"duty={rt.evap.duty() or ''}"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "evap",
                "status": "ok",
                "detail": f"at or below baseline={rt.evap.baseline():.4f}",
            }
        )

    unassigned = rt.rooms.unassigned()
    if rt.rooms.count() == 0:
        checks.append({"name": "rooms", "status": "warn", "detail": "no room registered"})
    elif unassigned:
        checks.append({"name": "rooms", "status": "warn", "detail": "unassigned " + ", ".join(unassigned)})
    else:
        checks.append({"name": "rooms", "status": "ok", "detail": "every room assigned"})

    return checks
