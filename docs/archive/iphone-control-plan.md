> **Historical**: superseded design plan; the single source of truth is [CLAUDE.md](../../CLAUDE.md).

afl-cam: iPhone control and connectivity design

Goal: GoPro/Insta360-level usability from an iPhone. Join the camera, see a live preview, start and stop recording, check health, and browse and watch past recordings, without a laptop.

This reviews the DeepSeek plan and then sets out the design I recommend. I haven't seen the code yet (no repo is attached to the project), so anything about cam2stitch, field_wifi.sh or the current snapshot page comes from the plan's own description.

1. Verdict on the DeepSeek plan

What it got right

The board as a WiFi access point that the phone joins is the right foundation. It's what GoPro and Insta360 do, and testing AP mode on bcmdhd really is step zero.
A web app served by the board, with a push channel for control and status, is the right first UI. You can build it today and it carries over to a native app later.
An H.264 preview from a second rkvenc session is the right video source. The hardware has plenty of headroom for it.

Where I'd change it

It optimises the wrong thing. You're on the field for the whole game. The phone is used for about five minutes of setup and framing, maybe a check at quarter time, and then stop and review afterwards. Nobody watches the preview during play, so 200 ms versus 1 s latency hardly matters. What matters is confidence: is it definitely recording, is the framing right, will it last the game, and did it work? Reliability and status deserve more effort than preview latency.
WebRTC isn't a "large" job if you don't build it yourself. Skip aiortc (Python, slow, wheel problems) and webrtcbin. Use MediaMTX instead. It's a single Go binary with arm64 builds that takes an H.264 stream in (RTSP) and serves it to Safari as WebRTC (WHEP), with HLS as a fallback, all from the same stream. Your code only has to produce a small H.264 stream, which it already knows how to do. That turns Phase B from days of work into roughly a day, so I'd skip the MJPEG phase entirely. MJPEG at 960×540 and 30 fps is about 15–25 Mbps over the AP, and it's a dead end you'd throw away.
It misses that the masters aren't phone material. The master is 5304×1896 H.265 at 30 Mbps, about 13.5 GB per hour. At about 10.1 MP per frame it needs HEVC Level 6, above the Level 5.x (4K-class) limit, and I haven't verified that iPhones hardware-decode Level 6. AirDrop yourself a clip to check. Even if it plays, it's too large to browse over WiFi. "Browse previous recordings" therefore needs a proxy file, and the preview encode can be that proxy for free (see §4).
Go native. You code agentically and can sideload, so development time isn't the constraint. That makes the native iOS app the primary client and the board's web UI the fallback, which removes most of the Safari limitations the plan was working around (§7).
Missing pieces that matter more than preview: recording has to survive the phone leaving, a physical start/stop button so the phone isn't a single point of failure, setting the board's clock from the phone (an offline AP has no NTP, so folder names will be wrong), and a cheap way to join (a QR code on the rig).
2. Architecture
 iPhone (Safari, added to Home Screen)
   │  WiFi: joins "AFLCAM-xxxx" (WPA2), board = 10.42.0.1 / aflcam.local
   │
   ├── https?/http  :80   ── afl-camd  (control daemon, web UI, REST + WebSocket)
   ├── WHEP / HLS   :8889/8888 ── MediaMTX  (preview video)
   │
 Orange Pi 5 Max
   afl-camd ── supervises ──► capture pipeline (cam2stitch, long-running)
                                  ├─ master: 5304×1896 H.264 30M → stitch.raw.h264
                                  └─ preview/proxy: ~1600×572 H.264 3–4M
                                         ├─ RTSP push → MediaMTX (only when watched)
                                         └─ proxy.mp4 on disk (always, while recording)

Three processes, each with one job:

Process	Job	If it dies
cam2stitch (C, existing)	cameras, stitch, encode, write	the only thing that must never fail
afl-camd (Python, new)	state machine, API, web UI, file listing	restarted by systemd; recording unaffected
MediaMTX (binary)	preview video to the phone	restarted by systemd; recording unaffected

Golden rule: nothing on the phone side can block or stop recording. The preview output from cam2stitch must be non-blocking (drop frames when the consumer is slow or gone). Recording continues if the phone disconnects, the page closes or WiFi drops. When the phone reconnects, the UI rebuilds itself from /status.

2.1 Change to the recorder: always-on pipeline, recording is a toggle

Today, starting a recording presumably starts cam2stitch. For a GoPro-like feel the pipeline should run continuously from power-on in standby: cameras open, stitching, preview encoding, with no master encode or write. Recording then just turns the master encoder and writer on (forcing an IDR on the first frame) and off again.

Why:

Preview works before you press record, which is when you need it for framing.
Start and stop are instant, with no re-opening of UVC devices, which avoids the classic "camera didn't come up this time" failure in front of a crowd.
Stop can finalise (mux, faststart, proxy) in the background while the pipeline keeps running.

The control channel to cam2stitch can be very simple: a Unix socket or stdin with REC_START <dir>, REC_STOP and STATUS, and a JSON status line every second (fps per camera, dropped frames, timestamp pairing skew, encoder queue depth, bytes written).

Thermal and power: standby still runs both cameras and the GPU. Add an idle mode (cameras closed) that standby falls back to after N minutes with no client and no recording, and that wakes when the phone connects.

3. Connectivity

Primary: the board is the AP.

Band: test both. 5 GHz gives more throughput and less interference; 2.4 GHz gives more range. The rig sits on the boundary and you'll mostly use it from within about 20 m, so 5 GHz (channel 36–48, 20/40 MHz) is probably right, with 2.4 GHz as a config switch.
Fixed address: 10.42.0.1, plus mDNS aflcam.local (Safari resolves .local).
No internet on the AP: iOS shows "No Internet Connection" but still talks to the LAN, and usually keeps cellular for internet traffic. Don't hijack DNS for captive.apple.com, or iOS will pop the captive-portal sheet, a mini browser that breaks the app.
Joining: stick a QR code on the rig. iOS Camera understands WIFI:T:WPA;S:AFLCAM-xxxx;P:<pass>;; and joins in one tap. A second QR (or the same sticker) holds http://aflcam.local. Add it to the Home Screen once and it opens full-screen like an app from then on.
Fallback: keep hotspot-follow mode (board joins the phone's hotspot) for when you need internet on both. It's a poor default: iPhone hotspots switch off when idle and drain the phone during the game.

Desk test checklist (do this before writing anything):

iPhone joins the AP, opens a page, and stays joined for 30+ minutes.
iperf3 from the phone (e.g. the "HE.NET Network Tools" app) while cam2stitch is recording, at 5 m, 20 m and 50 m. You need about 5 Mbps sustained. This also catches USB interference with the radio.
Run MediaMTX with a test stream and open its built-in WebRTC page in iPhone Safari over plain http. This settles the http-versus-https question below in about ten minutes.
3.1 The http vs https question

Safari has historically allowed WebRTC playback on plain http because only camera and mic capture require HTTPS. That's what the desk test confirms. Several things you may want later do require a secure context: service workers (an offline app shell), WebCodecs, and Web Share with files ("share proxy clip to the team chat").

If you need HTTPS, don't use a self-signed certificate. It needs a profile install and trust toggle on every phone, which is fine for you and terrible for a product. Do what Plex does instead: own a domain, point cam.<yourdomain> at 10.42.0.1 in the board's local DNS, and get a real Let's Encrypt cert with a DNS-01 challenge. Renew it whenever the board has internet (hotspot mode or home WiFi). That gives every iPhone HTTPS with no setup. Start on http and switch only if the desk test or a feature forces it.

4. Live preview
Source: the stitched NV12 canvas, already on the GPU. Downscale it in the existing Vulkan pass, or with RGA (the RK3588's 2D scaler), to about 1600×572 (same 2.8:1 aspect). The full-width view is what you need to check that both goal squares are in frame. Don't preview a single camera.
Encode: a second rkvenc session. Use H.264 for the live preview, not H.265, because H.264 WebRTC works in every browser, including the Android/web fallback. H.265 WebRTC only arrived recently in Safari and isn't universal. Use Main or Constrained Baseline, no B-frames, GOP 1 s (MediaMTX can't request keyframes from the source, so a short GOP keeps joins fast), CBR 3–4 Mbps, 30 fps (or 15 fps to save power).
Transport: Annex-B out of cam2stitch (pipe or UDP), then ffmpeg -c copy -f rtsp (or a tiny RTSP push), then MediaMTX. Use the on-demand settings so nothing streams unless someone is watching.
Phone: WHEP in a <video playsinline muted autoplay> gives about 150–300 ms. If WebRTC fails to connect within 3 s, the page falls back to MediaMTX's HLS of the same stream (a few seconds of latency, which is fine for framing). Same encode, no extra work on the board.
Overlays in the page, not burned in: the overlap seam position, a centre-square guide, and a level indicator if you add an IMU later.

The same stream is the proxy. If you want a smaller proxy file, run a third, H.265 rkvenc session instead. Either way, an H.265 MP4 must be tagged hvc1, not hev1 (-tag:v hvc1 in ffmpeg), or iPhone and QuickTime won't play it. Check that your existing masters are muxed that way too. While recording, also write the preview encode to proxy.mp4 beside the master. You get a phone-playable, roughly 1.5–2 GB/hour review file the instant recording stops, with no post-processing.

5. Control and status

API (served by afl-camd):

GET  /api/status            full state snapshot (below)
WS   /api/events            pushes status every 1 s + state changes
POST /api/record/start      {request_id}   idempotent: phone retries are safe
POST /api/record/stop       {request_id}
POST /api/time              {epoch_ms, tz}  phone sets clock if not NTP-synced
GET  /api/recordings        list: id, start time, duration, size, has_proxy, thumb
GET  /api/recordings/{id}/proxy.mp4   (HTTP Range support; Safari requires it)
GET  /api/recordings/{id}/thumb.jpg
DELETE /api/recordings/{id}
POST /api/schedule          {start_at, stop_after}  optional

State machine: idle → standby → recording → finalizing → standby, plus error.

Status the phone shows. This is what earns the "GoPro feel":

Big REC indicator with elapsed time, and a red banner on any error.
Storage left, in minutes of recording (not GB).
Per-camera fps and dropped frames, and the timestamp pairing skew between cameras.
Audio level meters for both mics.
SoC temperature, with throttling warnings.
Battery or input voltage, if you can read it (USB-PD or an INA219 on the supply).
"Last recording OK" with a thumbnail and duration once finalised.

Things that make it trustworthy unattended:

Hardware button and LED on a GPIO: press to start or stop, solid when recording, blinking on error. The phone becomes optional on game day.
Scheduled start ("start at 2:10 pm, stop after 2 h 15 m"), in case you're warming up when the ball goes up.
Auto-stop on low storage or critical temperature, finalising cleanly rather than leaving a broken file.
Clock from phone: on connect, the page posts the phone's time. The board sets its clock if it hasn't synced NTP since boot, so folder names are right.
Auth: WPA2 password plus a per-device token stored by the page on first join from the QR, so someone else at the ground can't press stop.
6. Browsing recordings
The list shows a thumbnail, date, duration, size, and proxy and master status.
Tapping plays proxy.mp4 in the page. Mux it with -movflags +faststart (moov atom at the front) and serve it with Range/206 support, or Safari won't play or seek it.
To save to the phone, the page offers a download, then "Save Video" from the share sheet. That's fine at proxy size, roughly 300–500 MB per quarter if you split by quarter later. One-tap share needs HTTPS (§3.1).
Masters don't go to the phone. Pull them off over ethernet, SMB or USB to a laptop for analysis. The phone UI only shows that they exist and their size.
Later: quarter markers (tap "Q1 end" during the break), which give chapters in the proxy and seek points for review.
7. Clients: web UI first, native iOS app later

Both clients use the same API (§5) and video endpoint (WHEP from MediaMTX), so the board doesn't care which one is talking to it.

7.1 Web UI, built first

This is the first client. It covers status, start/stop, preview, clock sync and the recordings list, and you join with the QR code from §3. Plain http is fine for it (§3.1). It stays useful after the native app exists, for Android teammates, a laptop, or an expired sideload.

To make the native app a drop-in later, keep the status as one compact snapshot (state, elapsed time, minutes left, temperature, error flags) that could fit in a BLE characteristic.

7.2 Native iOS app (SwiftUI, iOS 17+), later

The biggest win: Bluetooth for control and status, WiFi only for video. This is how GoPro and Insta360 actually work.

The board runs a BLE GATT peripheral (BlueZ). Status is a notify characteristic carrying state, elapsed time, minutes of storage left, temperature, error flags and dropped frames. Commands go to a write characteristic: start, stop, set time, and AP on/off.
The phone stays on its normal network (internet, team group chat) and can still see and control the camera from the bench. It joins the camera's WiFi only to preview or browse.
With CoreBluetooth's background mode, the app gets status updates while it's in your bag. That allows a local notification if recording stops or errors, and a Live Activity on the lock screen showing REC and the elapsed time.
The AP can stay off until the app asks for it, which saves power and removes 2.4/5 GHz noise during the game.
First check that the Orange Pi 5 Max's onboard Bluetooth works under your Armbian build. BLE GATT through BlueZ D-Bus is well trodden (e.g. bless in Python).

The rest of the app:

Connect: the app finds the rig over BLE, reads the SSID and password from a characteristic, and joins with NEHotspotConfiguration. That's one tap, with no Settings app and no QR code. It needs the Hotspot Configuration entitlement, which may require a paid developer account (not verified). Bonjour (_aflcam._tcp via avahi) finds the API once on WiFi, and iOS will ask once for Local Network permission.
Preview: WHEP into the WebRTC framework (the stasel/WebRTC Swift package) gives about 150–300 ms, from the same MediaMTX endpoint the web UI uses. If you want lower latency later, pull RTSP and decode with VideoToolbox into an AVSampleBufferDisplayLayer for about 100 ms. That's a nice-to-have.
Recordings: proxies play in AVPlayer, downloads continue in the background (background URLSession), and Save to Photos is one tap (PHPhotoLibrary). Sharing a clip to the team chat is the standard share sheet.

Getting it onto your phone: sideloading with a free Apple ID works, but the app expires every 7 days, and that will bite on some game day. The US$99/yr developer account gives one-year installs, TestFlight for teammates, and the hotspot entitlement. Builds can run on a Mac or on a GitHub Actions macOS runner with fastlane, so no local Mac is strictly required.

8. Build order
Desk test (§3): AP, iPhone join, iperf3 while recording, MediaMTX WebRTC in Safari. Everything else depends on this.
Recorder control socket + standby/record toggle in cam2stitch (§2.1).
afl-camd: state machine, REST/WebSocket API, clock sync, auto-stop.
Preview/proxy encode + MediaMTX (§4).
Web UI (§7.1): status, start/stop, preview, recordings browser.
GPIO button + LED, scheduled start.
Later, once the rig is reliable in the field: the native iOS app (§7.2), starting with BLE status and control (which means adding the BLE service to afl-camd), then WiFi join, preview, recordings and notifications.