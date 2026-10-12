"""Loose performance sanity: the pipeline must actually move frames.
Thresholds are deliberately generous (the cameras are exposure-limited in
dim light — ~10-30 fps is normal); <5 fps means something is broken."""

import time

from .conftest import stitch, wake_standby


def test_pipeline_frame_rate_sane():
    st = wake_standby()
    f0 = (st.get("cam0") or {}).get("frames", 0)
    time.sleep(10)
    f1 = (stitch().get("cam0") or {}).get("frames", 0)
    fps = (f1 - f0) / 10.0
    assert fps >= 5, f"cam0 at {fps:.1f} fps — pipeline is stalled or wedged"
