# Phone UI (`afl_cam/web/index.html`)

One static page, no build step, no CDN (the rig's AP has no internet).
Vanilla JS; state arrives over the `/api/events` WebSocket as the same
snapshot `/api/status` returns, and `applyStatus(s)` fans it out.

## Shell

- **Top bar**: name and the one status indicator, a pill (`#chip`) at the
  top right. `applyStateBar` maps the daemon state to a kind (`standby`,
  `recording`, `busy`, `error`, `ready`) for the colour and shows "Offline"
  when the feed is down. A daemon `error` shows in a callout (`#banner`)
  only once it has persisted for a few seconds, since a restart reports
  errors briefly. There is no second state bar and no separate link dot.
- **Views**: every `<section data-view="record|recs|setup">` belongs to one
  view. `body[data-tab]` decides which show. `data-view="all"` (the preview)
  shows everywhere except Recordings.
- **Bottom tab bar** (`.tabbar`): Record, Recordings, Setup. The last tab is
  remembered in `localStorage`.

## Tokens and components

Tokens live at the top of the `<style>` block: colours, radii (`--r-*`),
spacing (`--s1..s5`), `--tap` (44 px minimum touch target), safe-area insets.
Components: `.card`, `.btn` (`.primary`, `.danger`, `.sm`), `.seg` (segmented
control), `.row` + `.lab` (a setting), `.note`, `.field`, `.metrics`/`.metric`.
Dim text on the panel colours is at least 4.5:1.

## Readiness

Setup's Readiness card shows the static self-test checks (cameras, alignment,
storage, temperature, clock) and a **Test recording** that records 10 s in
live-stitch mode while the page samples the status feed, stops, waits for
the file to finish saving, and judges what came out: frame rate, pair skew,
camera errors, bitrate written, master saved, proxy made,
duration. `readinessVerdict()` is a pure function, unit-tested through node
in `tests/unit/test_web_ui.py`. The test recording stays in Recordings; a
button deletes it.

## Health panel

`afl_cam/sysmon.py` samples CPU (total and per core), memory, labelled
temperatures and GPU load; the daemon puts it in the status feed as `sys`.
`applyHealth` draws bars, per-core columns, 60 s sparklines and chips for
temperatures, disk, bitrate and camera errors. The frame rate chart draws one
line per camera on a 0 to 35 fps scale, with bands under 20 fps (amber) and
under 12 fps (red). The cam2stitch `skip` counter is not shown: it counts
unused camera frames, not lost ones (issue #55). To add a
reading: add it to `SysMon.snapshot`, then a row or chip in `applyHealth`.

## Recordings

One compact row per session: thumbnail, date, duration and size, a Play
button and a menu (`<details class="menu">`) holding Download proxy,
Download full quality (the master; "stitched" for separate sessions), Make
proxy, Stitch and Delete, each download labelled with its size
(`proxy_bytes`, `master_bytes` in `/api/recordings`). Badges show only what
needs attention: live, resumed, error, two cameras. The list is not
re-rendered while a menu is open.

## Schedule

Start time (with "in 1/5/15 min" shortcuts) and one duration with minutes and
seconds fields (presets from 30 s to 90 min). The daemon takes
`stop_after` in seconds up to 6 h, so 30 s test recordings work.

## Copy and explanations

No em dashes or semicolons in UI copy (a test enforces it), and facts only:
the page never explains a reading with a guess. Longer explanations sit
behind an `(i)` control (`.info` with a `.tip`, shown on hover, focus or
press), not in running text.

## Preview

The picture state comes from the daemon. A mode or crop change restarts the
pipeline, so `pvWatchConfig` drops the picture at once and holds off
reconnecting for 2.5 s. While the daemon is not in `standby` or `recording`
the overlay shows a neutral reason ("Starting cameras") and nothing counts as
a failure. Guides draw only on Setup and only while a picture is showing
(`body.pv-live`). In separate mode the stitched preview is replaced by one
pane per camera (`#pane0`, `#pane1`), fed by the recorders' snapshots while
recording.

## Previewing and reviewing changes

`python3 scripts/ui_preview/build.py` writes `build/ui-preview.html`: the
page with a mock rig injected (scenario bar, working mode/crop/record
buttons). Open it from any browser, no daemon needed.
`NODE_PATH=<dir with playwright-core> node scripts/ui_preview/shots.js
build/shots` screenshots every scenario at four viewports and fails on
horizontal overflow or page errors. Review the screenshots before opening a
UI PR.

## Full-screen preview

The expand button on the preview turns `#preview` into a fixed overlay (iOS
Safari only full-screens a bare `<video>`, so the Fullscreen API is not
used). `#stage` holds the video and takes the zoom transform: pinch, drag
to pan, double-tap for 3x, mouse wheel on a desktop, a reset chip, Esc or
the close button to leave. A small HUD shows REC and elapsed time. Guides
are hidden in full screen. This does not touch the preview transport.

## Adding something

1. New setting: add a `.row` in the Recording card with a `.seg` or `.field`.
2. New metric: add a `.metric` with `.v` and `.k`; fill it in `applyStatus`.
3. New view: add a name to `TABS` in the script, a `data-view` on its
   sections, a button in `.tabbar`, and the `body:not([data-tab=...])` rule.

`tests/unit/test_web_ui.py` checks that every `$("id")` the script looks up
exists in the markup and that nothing loads from the internet.
The preview transport (WHEP/HLS) is separate work, see the `pv*` functions.
