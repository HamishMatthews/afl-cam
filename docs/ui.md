# Phone UI (`afl_cam/web/index.html`)

One static page, no build step, no CDN (the rig's AP has no internet).
Vanilla JS; state arrives over the `/api/events` WebSocket as the same
snapshot `/api/status` returns, and `applyStatus(s)` fans it out.

## Shell

- **Top bar**: name and link indicator (WebSocket up/down).
- **State banner** (`#statebar`): the one place that says what the rig is
  doing. `applyStateBar` maps the daemon state to a kind (`standby`,
  `recording`, `busy`, `error`, `ready`) which sets the colour; `error`
  text from the daemon shows underneath.
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

## Health panel

`afl_cam/sysmon.py` samples CPU (total and per core), memory, labelled
temperatures and GPU load; the daemon puts it in the status feed as `sys`.
`applyHealth` draws bars, per-core columns, 60 s sparklines and chips for
temperatures, disk, bitrate, skipped frames and camera errors. To add a
reading: add it to `SysMon.snapshot`, then a row or chip in `applyHealth`.

## Adding something

1. New setting: add a `.row` in the Recording card with a `.seg` or `.field`.
2. New metric: add a `.metric` with `.v` and `.k`; fill it in `applyStatus`.
3. New view: add a name to `TABS` in the script, a `data-view` on its
   sections, a button in `.tabbar`, and the `body:not([data-tab=...])` rule.

`tests/unit/test_web_ui.py` checks that every `$("id")` the script looks up
exists in the markup and that nothing loads from the internet.
The preview transport (WHEP/HLS) is separate work, see the `pv*` functions.
