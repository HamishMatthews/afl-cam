"""Pre-game self-test: the checks the phone's Setup view runs before a
game. Pure functions over numbers the daemon already has (stdlib only),
so each verdict is unit-testable off the board.

Each check returns {"name", "level": ok|warn|fail, "detail"}; the overall
level is the worst one.
"""

import time
from typing import Optional

GAME_MINUTES = 150          # 2.5 h: a full game plus warm-up and breaks
TEMP_WARN_C = 70.0
ALIGN_WARN_DAYS = 30
LEVELS = ("ok", "warn", "fail")


def _check(name: str, level: str, detail: str) -> dict:
    return {"name": name, "level": level, "detail": detail}


def cameras(found: dict, stitch: Optional[dict]) -> dict:
    """Both cameras enumerated, and (when the pipeline is up) both
    delivering frames without capture errors."""
    missing = [c for c in ("cam0", "cam1") if not found.get(c)]
    if missing:
        side = {"cam0": "left", "cam1": "right"}
        return _check("cameras", "fail", "not found: " + ", ".join(
            f"{c} ({side[c]})" for c in missing)
            + " — check the plug")
    if not stitch:
        return _check("cameras", "ok", "both connected (pipeline asleep)")
    frames = [stitch.get(c, {}).get("frames", 0) for c in ("cam0", "cam1")]
    errors = [stitch.get(c, {}).get("errors", 0) for c in ("cam0", "cam1")]
    if min(frames) <= 0:
        return _check("cameras", "fail", "connected but no frames from "
                      + ("cam0" if frames[0] <= 0 else "cam1"))
    if max(errors) > 0:
        return _check("cameras", "warn",
                      f"streaming, capture errors {errors[0]}/{errors[1]}")
    return _check("cameras", "ok", "both streaming")


def alignment(aligned_at: Optional[float], now: Optional[float] = None) -> dict:
    if not aligned_at:
        return _check("alignment", "fail",
                      "no alignment on the board — run Re-align")
    days = ((now or time.time()) - aligned_at) / 86400
    age = f"{days:.0f} days ago" if days >= 1 else "today"
    if days > ALIGN_WARN_DAYS:
        return _check("alignment", "warn",
                      f"last re-aligned {age} — re-align if the rig was moved")
    return _check("alignment", "ok", f"last re-aligned {age}")


def storage(minutes_left: Optional[float]) -> dict:
    if minutes_left is None:
        return _check("storage", "warn", "free space unknown")
    hours = minutes_left / 60
    if minutes_left < GAME_MINUTES:
        return _check("storage", "fail",
                      f"{hours:.1f} h left — free space (need 2.5 h)")
    return _check("storage", "ok", f"{hours:.1f} h of recording space")


def temperature(soc_c: Optional[float], nvme_c: Optional[float],
                stop_c: float) -> dict:
    temps = [t for t in (soc_c, nvme_c) if t is not None]
    if not temps:
        return _check("temperature", "warn", "no temperature sensors read")
    hot = max(temps)
    detail = " · ".join(f"{label} {t:.0f}°C" for label, t in
                        (("board", soc_c), ("disk", nvme_c)) if t is not None)
    if hot >= stop_c:
        return _check("temperature", "fail",
                      detail + " — recording would auto-stop; shade the box")
    if hot >= TEMP_WARN_C:
        return _check("temperature", "warn", detail + " — running hot")
    return _check("temperature", "ok", detail)


def clock(synced: bool) -> dict:
    if synced:
        return _check("clock", "ok", "set")
    return _check("clock", "warn",
                  "not set — the page sets it from the phone when it loads")


def overall(checks: list) -> str:
    return max((c["level"] for c in checks), key=LEVELS.index, default="ok")
