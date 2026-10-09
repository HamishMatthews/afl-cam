# Field desk test — afl-cam iPhone control (iphone-control-plan.md §3)

Everything in this list needs the rig + an iPhone + 30 minutes on a desk.
The board-side pieces are already built and verified from a laptop browser
(2026-10-09): MediaMTX WHEP/HLS endpoints, the afl-camd API + WebSocket,
the record/stop/finalize cycle, preview UDP → ffmpeg → RTSP chain. What is
NOT yet verified is **Safari over the board's WiFi**, which only a phone
can settle.

## 1. Join and stay joined (do this first)

```bash
scripts/field_wifi.sh ap
```

- iPhone: join WiFi `afl-cam` (password `aflcam2026`).
- Open `http://10.42.0.1/` — the afl-cam page should come up, the chip
  should go `starting` → `standby`, and the preview should start within
  ~10 s (WHEP, or HLS after the 3 s fallback).
- Leave the phone on the page for 30+ minutes. The page must stay live
  (status ticking, preview frames) — this catches AP-side disconnects.
- iOS may show "No Internet Connection" under the WiFi name. Expected —
  cellular continues for internet. Do NOT hijack captive.apple.com
  (the plan explicitly avoids it, so no captive-portal sheet appears).

## 2. Throughput while recording (HE.NET Network Tools or similar)

1. Start a recording from the phone.
2. `iperf3` from the phone to the board (or a TCP throughput test app)
   at 5 m, 20 m and 50 m. You need ~5 Mbps sustained for the preview.
3. Watch for USB-2.4 GHz interference: both cameras stream ~125 Mbps each
   over USB while the radio is busy. If throughput collapses when
   recording starts, try the 5 GHz band (see below).

## 3. WebRTC in Safari over plain http — the http-vs-https question

This is the one thing the whole preview design hinges on:

1. On the board's AP, open `http://10.42.0.1/` in Safari.
2. The page attempts WHEP (`:8889`) first and falls back to HLS
   (`:8888/index.m3u8`) after ~3 s. Note which one wins and the latency
   (hold a hand in front of a camera: WebRTC ≈ 150–300 ms, HLS ≈ 2–5 s).
3. If WebRTC works over plain http → stay on http. If Safari blocks it,
   either live with HLS (fine for framing) or do the Plex-style HTTPS
   setup from the plan §3.1 (domain + DNS-01 Let's Encrypt) — do NOT use
   a self-signed cert.

## 4. Record / stop / review cycle

1. Press RECORD — the button turns red with elapsed time. Stop after ~1 min.
2. Wait for `finalizing` → `standby` (mux takes a few seconds).
3. Recordings list: thumbnail, duration, size, master + proxy badges.
4. Tap Play — the proxy must play and seek (tests faststart + Range/206).
5. Tap Save — the proxy downloads to the phone (share sheet → Save Video).
6. Clock check: the session's date/time in the list must match real time.
   (The page posts the phone's clock if the board hasn't NTP-synced since
   boot — verify with a session started on the AP.)
7. Kill the afl-cam page / turn the phone's WiFi off MID-RECORDING:
   recording must continue; reconnect and the UI must rebuild from status
   with the elapsed time intact.

## 5. Idle / wake

1. Close the page, wait `AFL_CAM_IDLE_SECS` (default 10 min): the
   pipeline closes the cameras (log line "idle … closing the pipeline").
2. Re-open the page: `idle` → `starting` → `standby`, preview returns.
   Cameras must come up first try (the thing this design exists to avoid).

## 6. Band choice (only if 2.4 GHz is flaky)

The rig sits ~20 m away, so 5 GHz (channel 36–48, 20/40 MHz) is the
likely default; 2.4 GHz has more range. The AP profile is
`afl-cam-ap` in NetworkManager — change the band there, re-run the AP,
repeat §2. Pick one and leave it.

## 7. Physical button (once wired)

`AFL_CAM_GPIO_BTN` / `AFL_CAM_GPIO_LED` in the afl-camd unit (default off).
Button press toggles record (300 ms debounce, active-low = button to GND);
LED solid while recording, blinking on error. Test: start from the button
with the phone in airplane mode, verify the LED, then stop.

## Sign-off

All of §1–5 pass with Safari on the AP → the field remote is game-ready
(subject to the still-open daylight-30 fps + NVMe-heatsink items).
Any failure → fix on the desk before the next game.
