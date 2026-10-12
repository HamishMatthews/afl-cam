"""The rig's foundation: both cameras discovered, streaming, advancing."""

import time

from .conftest import stitch, wake_standby


def test_both_cameras_streaming_and_advancing():
    st = wake_standby()
    c0 = st.get("cam0") or {}
    c1 = st.get("cam1") or {}
    assert c0.get("frames", 0) > 0, "cam0 reports no frames"
    assert c1.get("frames", 0) > 0, "cam1 reports no frames"
    assert c0.get("errors", 99) == 0, "cam0 capture errors"
    assert c1.get("errors", 99) == 0, "cam1 capture errors"

    f0, f1 = c0.get("frames"), c1.get("frames")
    time.sleep(3)
    st2 = stitch()
    assert (st2.get("cam0") or {}).get("frames", 0) > f0, "cam0 frames not advancing"
    assert (st2.get("cam1") or {}).get("frames", 0) > f1, "cam1 frames not advancing"
