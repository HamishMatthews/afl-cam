"""Board health for the phone's status feed: CPU, memory, temperatures, GPU.

Stdlib only. Every reader takes its paths as arguments so tests can point
it at a fake /proc and /sys.
"""

import threading
import time
from pathlib import Path
from typing import Optional

# thermal zone type -> label shown on the phone
_ZONE_LABELS = {
    "soc-thermal": "soc",
    "bigcore0-thermal": "big0",
    "bigcore1-thermal": "big1",
    "littlecore-thermal": "little",
    "center-thermal": "center",
    "gpu-thermal": "gpu",
    "npu-thermal": "npu",
}


def parse_cpu_times(text: str) -> list[tuple[int, int]]:
    """/proc/stat -> [(busy, total)] with the aggregate line first, then one
    entry per core."""
    out = []
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        vals = [int(v) for v in parts[1:9]]  # user..steal
        idle = vals[3] + vals[4]             # idle + iowait
        total = sum(vals)
        out.append((total - idle, total))
    return out


def cpu_percent(prev: list[tuple[int, int]],
                cur: list[tuple[int, int]]) -> tuple[float, list[float]]:
    pcts = []
    for (b0, t0), (b1, t1) in zip(prev, cur):
        dt = t1 - t0
        pcts.append(round(100.0 * (b1 - b0) / dt, 1) if dt > 0 else 0.0)
    if not pcts:
        return 0.0, []
    return pcts[0], pcts[1:]


def parse_meminfo(text: str) -> dict:
    kb = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if rest.split():
            kb[key] = int(rest.split()[0])
    total = kb.get("MemTotal", 0)
    avail = kb.get("MemAvailable", kb.get("MemFree", 0))
    used = max(0, total - avail)
    return {"pct": round(100.0 * used / total, 1) if total else 0.0,
            "used_mb": used // 1024, "total_mb": total // 1024}


def read_temps(thermal: Path = Path("/sys/class/thermal"),
               hwmon: Path = Path("/sys/class/hwmon")) -> dict:
    temps: dict[str, float] = {}
    try:
        for zone in sorted(thermal.glob("thermal_zone*")):
            label = _ZONE_LABELS.get((zone / "type").read_text().strip())
            if label:
                temps[label] = round(int((zone / "temp").read_text()) / 1000.0, 1)
    except (OSError, ValueError):
        pass
    try:
        for hw in sorted(hwmon.glob("hwmon*")):
            if (hw / "name").read_text().strip() == "nvme" \
                    and (hw / "temp1_input").exists():
                temps["nvme"] = round(
                    int((hw / "temp1_input").read_text()) / 1000.0, 1)
    except (OSError, ValueError):
        pass
    return temps


def read_gpu(devfreq: Path = Path("/sys/class/devfreq/fb000000.gpu")
             ) -> Optional[dict]:
    """The Mali devfreq 'load' file reads 'LOAD@FREQ' (percent @ Hz)."""
    try:
        load, _, freq = (devfreq / "load").read_text().strip().partition("@")
        return {"pct": float(load), "mhz": round(int(freq) / 1e6)}
    except (OSError, ValueError):
        return None


class SysMon:
    """Samples at most once per `min_interval` so every status push can call
    it. CPU percent needs two samples, so the first snapshot reads 0."""

    def __init__(self, proc: Path = Path("/proc"), min_interval: float = 0.9):
        self.proc = proc
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._prev: Optional[list[tuple[int, int]]] = None
        self._at = 0.0
        self._snap: dict = {}

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            if self._snap and now - self._at < self.min_interval:
                return self._snap
            snap: dict = {}
            try:
                cur = parse_cpu_times((self.proc / "stat").read_text())
                total, cores = cpu_percent(self._prev or cur, cur)
                self._prev = cur
                snap["cpu"] = {"total": total, "cores": cores}
            except (OSError, ValueError):
                pass
            try:
                snap["mem"] = parse_meminfo((self.proc / "meminfo").read_text())
            except (OSError, ValueError):
                pass
            snap["temps"] = read_temps()
            gpu = read_gpu()
            if gpu:
                snap["gpu"] = gpu
            self._at, self._snap = now, snap
            return snap
