"""Board health readers for the phone's health panel."""

from afl_cam import sysmon

STAT0 = "cpu  100 0 100 800 0 0 0 0\ncpu0 50 0 50 400 0 0 0 0\ncpu1 50 0 50 400 0 0 0 0\n"
STAT1 = "cpu  200 0 200 900 0 0 0 0\ncpu0 150 0 150 400 0 0 0 0\ncpu1 50 0 50 500 0 0 0 0\n"


def test_cpu_percent_total_and_per_core():
    total, cores = sysmon.cpu_percent(sysmon.parse_cpu_times(STAT0),
                                      sysmon.parse_cpu_times(STAT1))
    assert total == 66.7           # 400 busy of 600
    assert cores == [100.0, 0.0]


def test_cpu_percent_no_elapsed_time_is_zero():
    cur = sysmon.parse_cpu_times(STAT0)
    assert sysmon.cpu_percent(cur, cur) == (0.0, [0.0, 0.0])


def test_meminfo_uses_available_not_free():
    m = sysmon.parse_meminfo("MemTotal: 16000000 kB\nMemFree: 100 kB\nMemAvailable: 12000000 kB\n")
    assert m["pct"] == 25.0 and m["total_mb"] == 15625


def test_temps_are_labelled(tmp_path):
    for i, (typ, milli) in enumerate([("soc-thermal", 57300), ("npu-thermal", 51000), ("other", 1)]):
        z = tmp_path / "th" / f"thermal_zone{i}"
        z.mkdir(parents=True)
        (z / "type").write_text(typ + "\n")
        (z / "temp").write_text(f"{milli}\n")
    hw = tmp_path / "hw" / "hwmon7"
    hw.mkdir(parents=True)
    (hw / "name").write_text("nvme\n")
    (hw / "temp1_input").write_text("46900\n")
    assert sysmon.read_temps(tmp_path / "th", tmp_path / "hw") == {
        "soc": 57.3, "npu": 51.0, "nvme": 46.9}


def test_gpu_load_and_missing(tmp_path):
    (tmp_path / "load").write_text("37@800000000\n")
    assert sysmon.read_gpu(tmp_path) == {"pct": 37.0, "mhz": 800}
    assert sysmon.read_gpu(tmp_path / "nope") is None


def test_snapshot_is_rate_limited_and_survives_a_missing_proc(tmp_path):
    mon = sysmon.SysMon(proc=tmp_path)
    first = mon.snapshot()
    assert "cpu" not in first and "mem" not in first
    assert mon.snapshot() is first
