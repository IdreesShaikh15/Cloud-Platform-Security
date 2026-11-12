#!/usr/bin/env node
// Dashboard render benchmark (development tool, simulator only): starts sim/demo_server.py --dashboard, opens Demo Mode in headless Chromium,
// runs the app-compromise scenario and measures what the BROWSER does: time spent in renderTimeline / renderState, long tasks (>50 ms),
// how many refresh() calls overlap, how many requests are in flight at once, and how many activity-log rows are drawn.
//
//   node scripts/bench-dashboard.js [--seconds 25] [--latency 0] [--json]
//   --latency MS   add MS milliseconds to every request, like a slow `kubectl port-forward` (default 0)
//   --throttle X   slow the browser's CPU X times (Chromium's CPU throttling), to imitate a busy laptop (default 1 = off)
//   --prefill N    start with N events already in the page's buffer (a Live page left open for an hour holds up to 3000)
// Needs: node + the `playwright` package with Chromium (already present in the cloud sandbox). Nothing in the cluster is touched.
const { spawn } = require("child_process");
const path = require("path");
const http = require("http");
const { chromium } = require("playwright");

const arg = (n, d) => { const i = process.argv.indexOf("--" + n); return i < 0 ? d : (process.argv[i + 1] ?? true); };
const SECONDS = +arg("seconds", 25), LATENCY = +arg("latency", 0), PREFILL = +arg("prefill", 0), THROTTLE = +arg("throttle", 1), JSON_OUT = process.argv.includes("--json");
const PORT = +arg("port", 18091), CTRL = +arg("control-port", 50271), BASE = +arg("base-port", 50351);
const ROOT = path.resolve(__dirname, "..");

function up(url) { return new Promise(r => http.get(url, res => { res.resume(); r(res.statusCode === 200); }).on("error", () => r(false))); }
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const srv = spawn("python3", [path.join(ROOT, "sim/demo_server.py"), "--dashboard", "--dashboard-port", String(PORT),
    "--control-port", String(CTRL), "--base-port", String(BASE)], { cwd: ROOT, stdio: "ignore", detached: true });
  const kill = () => { try { process.kill(-srv.pid, "SIGKILL"); } catch (e) {} };      // the whole process group: the server starts the dashboard as a child
  process.on("exit", kill);
  for (let i = 0; i < 120 && !(await up(`http://127.0.0.1:${PORT}/healthz`)); i++) await sleep(500);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1500, height: 1100 } });
  await page.addInitScript(({ latency }) => {
    const B = window.__bench = { inflight: 0, maxInflight: 0, requests: 0, stateFetches: 0,
      tl: [], st: [], long: [], timelineRows: 0 };
    const f = window.fetch.bind(window);
    window.fetch = async (...a) => {
      B.inflight++; B.requests++; B.maxInflight = Math.max(B.maxInflight, B.inflight);
      if (String(a[0]).includes("api/state")) B.stateFetches++;
      try { if (latency) await new Promise(r => setTimeout(r, latency)); return await f(...a); } finally { B.inflight--; }
    };
    try { new PerformanceObserver(l => { for (const e of l.getEntries()) B.long.push(e.duration); }).observe({ entryTypes: ["longtask"] }); } catch (e) {}
  }, { latency: LATENCY });
  if (THROTTLE > 1) await (await page.context().newCDPSession(page)).send("Emulation.setCPUThrottlingRate", { rate: THROTTLE });
  await page.goto(`http://127.0.0.1:${PORT}/?mode=demo`);
  await page.waitForSelector("#nodes .card", { timeout: 60000 });
  await page.evaluate(() => {
    const B = window.__bench, wrap = (name, bucket) => {
      const orig = window[name]; if (typeof orig !== "function") return;
      window[name] = function (...a) { const t = performance.now(); try { return orig.apply(this, a); } finally { bucket.push(performance.now() - t); } };
    };
    wrap("renderTimeline", B.tl); wrap("renderState", B.st);
  });
  if (PREFILL) await page.evaluate(n => {
    const cats = ["OBSERVE", "EVIDENCE_RECEIVED", "SCORE", "TRUST_CHANGE"];
    for (let i = 0; i < n; i++) events.push({ ts: Date.now() / 1000 - (n - i) * 0.2, gseq: -n + i, node: "ABCD"[i % 4], category: cats[i % 4],
      summary: `Agent ${"ABCD"[i % 4]} sees records-api (C) as normal on all 4 signals. ${i}`, target: "C", details: { n: i }, data_source: "SIMULATOR" });
  }, PREFILL);
  await page.waitForSelector("#simstart:not([disabled])", { timeout: 60000 });
  await page.click("#simstart");
  const t0 = Date.now();
  await sleep(SECONDS * 1000);
  const r = await page.evaluate(() => {
    const B = window.__bench, sum = a => a.reduce((x, y) => x + y, 0), mx = a => a.length ? Math.max(...a) : 0;
    return {
      seconds: null, requests: B.requests, max_requests_in_flight: B.maxInflight, state_fetches: B.stateFetches,
      
      renderTimeline_calls: B.tl.length, renderTimeline_total_ms: Math.round(sum(B.tl)), renderTimeline_max_ms: Math.round(mx(B.tl) * 10) / 10,
      renderState_calls: B.st.length, renderState_total_ms: Math.round(sum(B.st)), renderState_max_ms: Math.round(mx(B.st) * 10) / 10,
      long_tasks: B.long.length, long_task_total_ms: Math.round(sum(B.long)), long_task_max_ms: Math.round(mx(B.long)),
      timeline_rows_in_dom: document.querySelectorAll("#timeline .ev").length, dom_nodes: document.getElementsByTagName("*").length,
      events_buffered: (typeof events !== "undefined") ? events.length : null,
    };
  });
  r.seconds = Math.round((Date.now() - t0) / 1000); r.latency_ms = LATENCY; r.cpu_throttle = THROTTLE;
  await browser.close(); kill();
  if (JSON_OUT) console.log(JSON.stringify(r)); else for (const [k, v] of Object.entries(r)) console.log(k.padEnd(30), v);
  process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
