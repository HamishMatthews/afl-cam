// Screenshot the mock page at several viewports and scenarios.
//   node scripts/ui_preview/shots.js <out-dir> [preview.html]
// Needs playwright-core and a chromium (PLAYWRIGHT_BROWSERS_PATH or CHROMIUM).
const { chromium } = require("playwright-core");
const path = require("path");
const fs = require("fs");
const out = process.argv[2] || "build/shots";
const page_html = path.resolve(process.argv[3] || "build/ui-preview.html");
fs.mkdirSync(out, { recursive: true });
const VIEWPORTS = { se: [375, 667], phone: [393, 852], landscape: [852, 393], desktop: [1270, 900] };
// [file stem, scenario, tab, extra]
const SHOTS = [
  ["record-ready", "standby", "record"], ["record-recording", "recording", "record"],
  ["record-lowfps", "lowfps", "record"], ["record-crop", "crop", "record"],
  ["record-separate", "separate", "record"], ["record-separate-rec", "sepRec", "record"],
  ["record-starting", "starting", "record"], ["record-error", "error", "record"],
  ["recordings", "standby", "recs", "menu"], ["setup", "standby", "setup", "readiness"],
  ["setup-realign", "realign", "setup"],
];
(async () => {
  const exe = process.env.CHROMIUM || "/opt/pw-browsers/chromium";
  const b = await chromium.launch({ executablePath: exe, args: ["--no-sandbox"] });
  let bad = 0;
  for (const [vn, [w, h]] of Object.entries(VIEWPORTS)) {
    const ctx = await b.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 2 });
    for (const [stem, scen, tab, extra] of SHOTS) {
      const p = await ctx.newPage();
      const errs = [];
      p.on("pageerror", (e) => errs.push(e.message));
      await p.goto(`file://${page_html}?scenario=${scen}&nobar`);
      await p.evaluate((t) => document.getElementById("tab-" + t).click(), tab);
      await p.evaluate(() => __mock.tick(45));          // fill the charts
      await p.waitForTimeout(1200);                    // preview settles
      if (extra === "readiness") await p.evaluate(() => {
        rdRender(readinessVerdict([{t:0,c0:0,c1:0,bytes:0,skip:0,err:0,skew_ms:12},{t:10,c0:180,c1:190,bytes:9e7,skip:140,err:0,skew_ms:19}], {ok:true,has_proxy:true,duration_s:10.2}));
        document.getElementById("rd-progress").textContent = "Done";
      });
      if (extra === "menu") await p.evaluate(() => document.querySelector("#rec-list details").open = true);
      const ow = await p.evaluate(() => document.documentElement.scrollWidth - innerWidth);
      if (ow > 0 || errs.length) { bad++; console.log(`PROBLEM ${vn}/${stem}: overflowX=${ow} errors=${errs}`); }
      await p.addStyleTag({ content: ".tabbar{position:static !important} .topbar{position:static !important}" });
      await p.screenshot({ path: path.join(out, `${vn}-${stem}.png`), fullPage: true });
      await p.close();
    }
    await ctx.close();
  }
  await b.close();
  console.log(bad ? `${bad} problem(s)` : "no overflow, no page errors");
  process.exit(bad ? 1 : 0);
})();
