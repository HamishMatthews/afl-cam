/* Mock rig for the phone page. Injected after the page's own script by
 * build.py, so the page runs against fake data in any browser with no
 * daemon: scripts/ui_preview/README.md. Nothing here ships to the board. */
(function () {
  "use strict";
  const q = new URLSearchParams(location.search);
  const T0 = Date.now();
  let skew = 0;                                   // fake clock offset, see tick()
  const realNow = Date.now.bind(Date);
  Date.now = () => realNow() + skew;

  const SCENARIOS = {
    standby:   {label: "Ready"},
    recording: {label: "Recording", recording: true},
    lowfps:    {label: "Low fps", fps: [17, 19]},
    crop:      {label: "Crop on", crop: true},
    separate:  {label: "Separate idle", mode: "separate"},
    sepRec:    {label: "Separate rec", mode: "separate", recording: true},
    starting:  {label: "Starting", state: "starting"},
    idle:      {label: "Asleep", state: "idle"},
    realign:   {label: "Re-aligning", state: "realigning"},
    error:     {label: "Error", state: "error", error: "cam1: no frames for 5 s"},
  };
  const M = {name: q.get("scenario") || "standby", t0: Date.now(), recStart: null,
             mode: "live", crop: false, recording: false, state: "standby", error: null, hold: 0};
  function load(name) {
    const sc = SCENARIOS[name] || SCENARIOS.standby;
    M.name = name;
    M.mode = sc.mode || "live"; M.crop = !!sc.crop; M.recording = !!sc.recording;
    M.state = sc.state || (M.recording ? "recording" : "standby"); M.error = sc.error || null;
    M.fps = sc.fps || (M.recording ? [24, 23] : [27, 26]);
    M.hold = 0; M.recStart = M.recording ? Date.now() - 42 * 60000 - 10000 : null;
    mark();
  }
  function mark() { document.querySelectorAll("#mockbar button").forEach(b => b.classList.toggle("on", b.dataset.n === M.name)); }
  /* a mode or crop change restarts the pipeline, as the daemon does */
  function restart() { M.state = "starting"; M.hold = Date.now() + 2500; }

  function hms(ms) { const s = Math.floor(ms / 1000); return Math.floor(s / 3600) + ":" + String(Math.floor(s / 60) % 60).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0"); }
  function status() {
    if (M.hold && Date.now() >= M.hold) { M.hold = 0; M.state = M.recording ? "recording" : "standby"; }
    const el = (Date.now() - M.t0) / 1000, wob = Math.sin(el / 3);
    const f0 = M.fps[0] + wob, f1 = M.fps[1] - wob * .7;
    const c0 = M.fps[0] * el + 3 * (1 - Math.cos(el / 3)), c1 = M.fps[1] * el - 2.1 * (1 - Math.cos(el / 3));
    const live = M.state === "standby" || M.state === "recording";
    const stitch = live ? {cam0: {frames: Math.floor(c0), errors: 0}, cam1: {frames: Math.floor(c1), errors: 0},
      skip: Math.floor(el * 14), paired: Math.floor(c0), rec_bytes: M.recording ? Math.floor(((Date.now() - M.recStart) / 1000) * 10e6) : 0,
      skew_avg_us: 17000 + wob * 3000} : null;
    const wave = k => Math.max(2, Math.min(98, 40 + 30 * Math.sin(el / 4 + k)));
    return {state: M.state, recording: M.recording && live, error: M.error, mode: M.mode, session_kind: M.mode === "separate" ? "separate" : "live",
      elapsed_hms: M.recording ? hms(Date.now() - M.recStart) : "0:00:00",
      storage: {minutes_left: 619, free_gb: 812}, temp: {soc_c: 57.3, nvme_c: 46.9, critical: false},
      stitch, crop: {enabled: M.crop, active: M.crop, canvas: M.crop ? [5192, 1680] : [5304, 1876]},
      clock: {synced: true}, resume: {}, schedule: null,
      sys: {cpu: {total: wave(0), cores: [0, 1, 2, 3, 4, 5, 6, 7].map(i => wave(i))}, mem: {pct: 34, used_mb: 5400, total_mb: 15900},
        gpu: {pct: wave(9), mhz: 800}, temps: {soc: 57.3, big0: 58, big1: 57, little: 55, gpu: 56, npu: 54, nvme: 46.9}},
      last_recording: {session_id: 3, ok: true, has_proxy: true, duration_s: 10.2},
      realign: {running: M.state === "realigning", started_at: Date.now() / 1000 - 40, log_tail: ["stills captured", "bundle fit..."]},
      restitch: null};
  }
  const RECS = {recordings: [
    {id: 3, created_at: "2026-10-13 16:05:00", duration_s: 5400, size_bytes: 9e9, has_proxy: true, has_master: true, proxy_bytes: 3.1e8, master_bytes: 8.9e9, kind: "live", recording: false},
    {id: 2, created_at: "2026-10-13 15:20:00", duration_s: 30, size_bytes: 3e8, has_proxy: true, has_master: false, proxy_bytes: 2e7, master_bytes: 0, kind: "separate", resumed: {restarts: 1, lost_s: 4}},
    {id: 1, created_at: "2026-10-12 11:02:00", duration_s: 10, size_bytes: 1e8, has_proxy: false, has_master: true, proxy_bytes: 0, master_bytes: 1e8, kind: "live"}]};
  const SELFTEST = {level: "warn", checks: [
    {name: "Cameras", level: "ok", detail: "both streaming at 4K"}, {name: "Storage", level: "ok", detail: "812 GB free"},
    {name: "Clock", level: "warn", detail: "set from this phone"}]};
  const FRAMING = {framing: {canvas: [5304, 1876], seam: 0.5, overlap: [0.42, 0.58], horizon: 0.38}};

  const jres = (o) => Promise.resolve(new Response(JSON.stringify(o), {headers: {"Content-Type": "application/json"}}));
  const realFetch = window.fetch.bind(window);
  window.fetch = function (url, opt) {
    const u = new URL(url, location.href), m = ((opt && opt.method) || "GET").toUpperCase();
    if (!u.pathname.startsWith("/api/")) return Promise.reject(new Error("mock: offline"));
    const body = opt && opt.body ? (() => { try { return JSON.parse(opt.body); } catch (e) { return {}; } })() : {};
    if (m === "GET") {
      if (u.pathname === "/api/status") return jres(status());
      if (u.pathname === "/api/recordings") return jres(RECS);
      if (u.pathname === "/api/selftest") return jres(SELFTEST);
      if (u.pathname === "/api/framing") return jres(FRAMING);
      if (u.pathname === "/api/realign") return jres({log: ["done"]});
      return jres({});
    }
    if (u.pathname === "/api/record/start") { M.recording = true; M.recStart = Date.now(); if (M.state === "idle") restart(); else M.state = "recording"; }
    if (u.pathname === "/api/record/stop") { M.recording = false; M.state = "standby"; }
    if (u.pathname === "/api/mode") { M.mode = body.mode; restart(); }
    if (u.pathname === "/api/crop") { M.crop = !!body.enabled; restart(); }
    return jres({ok: true});
  };
  const setLinkReal = window.setLink;
  window.setLink = () => setLinkReal(true);      // the mock is always reachable
  window.WebSocket = function () { throw new Error("mock: no websocket, the page polls /api/status"); };

  /* synthetic pictures: a pitch panorama, and one camera's half of it */
  function pic(w, h, half) {
    const x0 = half === 1 ? -w / 2 : 0;
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
      <defs><linearGradient id="s" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#7fa6d0"/><stop offset="1" stop-color="#cfe0ee"/></linearGradient></defs>
      <rect width="${w}" height="${h * .38}" fill="url(#s)"/><rect y="${h * .38}" width="${w}" height="${h * .62}" fill="#3f7d3a"/>
      <g stroke="#fff" stroke-width="3" fill="none" transform="translate(${x0},0)">
        <line x1="${w * .1}" y1="${h * .95}" x2="${w * .9}" y2="${h * .95}"/><line x1="${w * .5}" y1="${h * .45}" x2="${w * .5}" y2="${h * .95}"/>
        <ellipse cx="${w * .5}" cy="${h * .7}" rx="${w * .08}" ry="${h * .1}"/>
        <path d="M ${w * .12} ${h * .5} L ${w * .02} ${h * .95}"/><path d="M ${w * .88} ${h * .5} L ${w * .98} ${h * .95}"/></g></svg>`;
    return "data:image/svg+xml;utf8," + encodeURIComponent(svg);
  }
  const PANO = pic(1400, 500, 0);

  /* the page's own preview logic, with the transport replaced by a still */
  window.previewAttempt = function () {
    const gen = ++pv.gen;
    pvTeardown(); pv.live = false; pv.waiting = false;
    document.body.classList.remove("pv-live");
    clearTimeout(pv.timer); pv.timer = null;
    pvOverlay(pvPipelineText());
    if (!pvPipelineUp()) {
      pv.waiting = true; pv.tries = 0;
      if (!(S && S.mode === "separate")) pv.timer = setTimeout(() => { if (gen === pv.gen) previewAttempt(); }, 1000);
      return;
    }
    pv.timer = setTimeout(() => { if (gen !== pv.gen) return; $("vid").poster = PANO; pvLive(); }, 800);
  };
  window.snapLoad = function (n, on) {
    const pane = $("pane" + n);
    if (!on) { pane.classList.remove("has"); return; }
    $("snap" + n).src = pic(800, 450, n); pane.classList.add("has");
  };

  /* test hooks: advance the fake clock and push a status, as a WS tick would */
  window.__mock = {load, mark, tick(n) { setLink(true); for (let i = 0; i < (n || 1); i++) { skew += 1000; applyStatus(status()); } }};

  /* scenario bar: lets a reviewer flip states on the static page */
  if (!q.has("nobar")) {
    const css = document.createElement("style");
    css.textContent = `#mockbar{position:fixed;left:0;right:0;bottom:calc(var(--tabbar-h) + var(--safe-b));z-index:30;display:flex;gap:6px;
      overflow-x:auto;padding:6px 8px;background:rgba(0,0,0,.82);font:12px var(--font)}
      #mockbar button{flex:none;padding:5px 10px;border-radius:999px;border:1px solid #444;background:#222;color:#ddd}
      #mockbar button.on{background:#58a6ff;color:#0d1117;border-color:#58a6ff}`;
    document.head.appendChild(css);
    const bar = document.createElement("div"); bar.id = "mockbar";
    for (const [n, sc] of Object.entries(SCENARIOS)) {
      const b = document.createElement("button"); b.dataset.n = n; b.textContent = sc.label;
      b.onclick = () => { load(n); setLink(true); applyStatus(status()); previewAttempt(); };
      bar.appendChild(b);
    }
    document.body.appendChild(bar);
  }
  load(M.name);
  setLink(true);
  applyStatus(status());
  previewAttempt();
})();
