"""Finalize muxing: the real frame timing (#9) and the camera audio (#10).

The fixture (tests/unit/es_fixture.py) is 45 frames — one second at 30 fps
then one second at 15 fps, 2.00 s of real time. Its median frame interval
reads 30 fps, so the CFR-at-the-median-rate mux these replace would ship
1.50 s of video for 2.00 s of game.

Needs ffmpeg and PyAV under the interpreter the mux will use (the board's
venv at AFL_CAM_RETIME_PY, else ours — the `mux` dependency group, which
the unit job installs); skipped where neither has it.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from afl_cam import daemon, resume
from afl_cam.retime import _interleaved, retime_interpreter

BUILDER = Path(__file__).resolve().parent / "es_fixture.py"
RATE = 44100
ONE_FRAME_S = 1 / 15          # the fixture's slowest frame
AV_PY = retime_interpreter()


def _has_pyav(py: Path) -> bool:
    """Ask the interpreter the mux will use, not this one."""
    return subprocess.run([str(py), "-c", "import av"],
                          capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or not _has_pyav(AV_PY),
    reason=f"needs ffmpeg and PyAV under {AV_PY} "
           "(uv sync --group mux, or set AFL_CAM_RETIME_PY)")


@pytest.fixture(scope="module")
def es(tmp_path_factory) -> Path:
    """The 30→15 fps fixtures, built by the PyAV interpreter."""
    out = tmp_path_factory.mktemp("es")
    built = subprocess.run([str(AV_PY), str(BUILDER), str(out)],
                           capture_output=True, text=True)
    assert built.returncode == 0, built.stderr or built.stdout
    return out


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=index,codec_type,codec_name,"
         "codec_tag_string,duration,nb_frames,sample_rate",
         "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def stamps(pts_path: Path) -> list[int]:
    return [int(x) for x in pts_path.read_text().split()]


def real_duration(pts: list[int]) -> float:
    """What the container should report: the last stamp plus its interval."""
    return (pts[-1] - pts[0] + pts[-1] - pts[-2]) / 1e6


def first_sound_s(path: Path) -> float:
    """When the audio first rises above silence (0.0 if it never does)."""
    out = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(path), "-vn",
         "-af", "silencedetect=noise=-40dB:d=0.05", "-f", "null", "-"],
        capture_output=True, text=True)
    found = re.search(r"silence_end: ([\d.]+)", out.stderr)
    return float(found.group(1)) if found else 0.0


def write_tone(path: Path, seconds: float) -> None:
    """A square-wave tone: unmistakably not silence."""
    half = RATE // 880
    frames = b"".join(
        struct.pack("<hh", v, v)
        for v in (12000 if (i // half) % 2 else -12000
                  for i in range(int(seconds * RATE))))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(frames)


def test_master_duration_follows_the_sidecar_not_the_median(
        es, tmp_path, monkeypatch):
    """2.00 s of game stays 2.00 s even though the rate halved mid-recording."""
    raw, pts_side = es / "h264_30to15.264", es / "h264_30to15.pts"
    pts = stamps(pts_side)
    out = tmp_path / "stitch.mp4"
    monkeypatch.setattr(daemon, "HEVC", False)

    assert daemon.mux_master(raw, pts_side, out) is True

    info = probe(out)
    duration = float(info["format"]["duration"])
    assert duration == pytest.approx(real_duration(pts), abs=ONE_FRAME_S)
    # the median interval is 33333 us, so a median-rate mux would say 1.50 s
    assert duration > len(pts) / 30.0 + 0.4
    assert int(info["streams"][0]["nb_frames"]) == len(pts)


def test_hevc_master_keeps_the_hvc1_tag(es, tmp_path, monkeypatch):
    """iPhone/QuickTime refuse a hev1-tagged HEVC master."""
    out = tmp_path / "stitch.mp4"
    monkeypatch.setattr(daemon, "HEVC", True)

    assert daemon.mux_master(es / "hevc_30to15.h265",
                             es / "hevc_30to15.pts", out) is True

    stream = probe(out)["streams"][0]
    assert stream["codec_name"] == "hevc"
    assert stream["codec_tag_string"] == "hvc1"


def test_proxy_faststarts_and_keeps_the_timing(es, tmp_path):
    """Safari seeks the proxy, so its moov has to come before mdat."""
    raw, pts_side = es / "h264_30to15.264", es / "h264_30to15.pts"
    out = tmp_path / "proxy.mp4"

    assert daemon.mux_proxy(raw, pts_side, out) is True

    head = out.read_bytes()[:65536]
    assert 0 <= head.find(b"moov") < head.find(b"mdat")
    duration = float(probe(out)["format"]["duration"])
    assert duration == pytest.approx(real_duration(stamps(pts_side)),
                                     abs=ONE_FRAME_S)


def test_mux_carries_the_audio_aligned_to_the_first_frame(es, tmp_path):
    """The mic started 0.25 s before the video's first frame, so the muxed
    audio opens with that much silence — otherwise every whistle is early."""
    raw, pts_side = es / "h264_30to15.264", es / "h264_30to15.pts"
    pts = stamps(pts_side)
    wav = tmp_path / "cam0.wav"
    write_tone(wav, 3.0)
    resume.audio_marker(wav).write_text(f"{pts[0] - 250_000}\n")
    out = tmp_path / "proxy.mp4"

    assert daemon.mux_proxy(raw, pts_side, out, audio=wav) is True

    info = probe(out)
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert audio, f"no audio stream in {info['streams']}"
    assert audio[0]["codec_name"] == "aac"
    assert float(audio[0]["duration"]) == pytest.approx(
        real_duration(pts), abs=2 * ONE_FRAME_S)
    assert first_sound_s(out) == pytest.approx(0.25, abs=0.05)


def test_audio_offset_follows_the_arecord_stamp(tmp_path):
    """Positive delay when the mic started first, a trim when it started late."""
    wav = tmp_path / "cam0.wav"
    write_tone(wav, 2.0)
    resume.audio_marker(wav).write_text("1000000")

    assert daemon._audio_offset_s(wav, 1_250_000) == pytest.approx(0.25)
    assert daemon._audio_offset_s(wav, 750_000) == pytest.approx(-0.25)

    resume.audio_marker(wav).unlink()          # nothing to align against
    assert daemon._audio_offset_s(wav, 1_250_000) == 0.0


def test_audio_offset_ignores_a_stale_marker(tmp_path):
    """A stamp from another run would pad the whole track with silence."""
    wav = tmp_path / "cam0.wav"
    write_tone(wav, 2.0)
    resume.audio_marker(wav).write_text("1000000")

    assert daemon._audio_offset_s(wav, 11_000_000) == 0.0


def test_concat_joins_resumed_segments_with_their_audio(es, tmp_path):
    """A recording that resumed after a crash finalizes as parts joined by
    the concat demuxer, so the VFR video and the audio both have to
    survive the join."""
    raw, pts_side = es / "h264_30to15.264", es / "h264_30to15.pts"
    wav = tmp_path / "cam0.wav"
    write_tone(wav, 3.0)
    resume.audio_marker(wav).write_text(f"{stamps(pts_side)[0]}\n")

    parts = []
    for i in range(2):
        part = tmp_path / f"stitch.part{i:03d}.mp4"
        assert daemon.mux_proxy(raw, pts_side, part, audio=wav) is True
        parts.append(part)

    out = tmp_path / "stitch.mp4"
    ok, err = resume.concat(parts, out, ("-movflags", "+faststart"))
    assert ok, err

    info = probe(out)
    assert [s["codec_type"] for s in info["streams"]] == ["video", "audio"]
    assert float(info["format"]["duration"]) == pytest.approx(
        2 * real_duration(stamps(pts_side)), abs=3 * ONE_FRAME_S)


def test_audio_files_follow_the_segment_numbering(tmp_path):
    """A resumed recording's mic takes are numbered with its video."""
    resumed = tmp_path / "stitch.000.raw.h264"

    assert resume.audio_for(resumed).name == "cam0.000.wav"
    assert resume.audio_for(resumed, "cam1").name == "cam1.000.wav"
    assert resume.audio_for(tmp_path / "stitch.raw.h264").name == "cam0.wav"
    assert resume.audio_marker(
        resume.audio_for(resumed)).name == "cam0.000.wav.start"


def test_drop_audio_takes_both_mics(tmp_path):
    """Live muxes only cam0's audio, so cam1's goes too — not left behind."""
    for cam in ("cam0", "cam1"):
        wav = tmp_path / f"{cam}.wav"
        write_tone(wav, 0.2)
        resume.audio_marker(wav).write_text("1")

    resume.drop_audio(tmp_path / "stitch.raw.h264")

    assert list(tmp_path.glob("cam*.wav*")) == []


def test_interleave_orders_packets_by_time():
    """The mp4 muxer only reorders within ~10 s, so feeding a whole track
    first would leave the other at the end of mdat — Safari would then have
    to range-fetch it separately."""
    video = iter([(0.0, "v0"), (2.0, "v1"), (4.0, "v2")])
    audio = iter([(1.0, "a0"), (3.0, "a1")])

    assert list(_interleaved(video, audio)) == ["v0", "a0", "v1", "a1", "v2"]
