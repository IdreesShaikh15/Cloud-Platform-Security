#!/usr/bin/env node
// Development check (simulator only): runs Demo Mode scenarios through the real page in headless Chromium and reports, for each one, whether the tabs
// the examiners look at actually fill in: Security Alerts, Suspicious activity, Recovery, Performance Results ("This run" graphs).
//   node scripts/check-demo-scenarios.js                       # every scenario, default service
//   node scripts/check-demo-scenarios.js --only slow-burn,obstruct --target A
//   node scripts/check-demo-scenarios.js --json
const { spawn } = require("child_process");
const path = require("path");
const http = require("http");
const { chromium } = require("playwright");
const arg = (n, d) => { const i = process.argv.indexOf("--" + n); return i < 0 ? d : (process.argv[i + 1] ?? true); };
const ONLY = (arg("only", "") || "").split(",").filter(Boolean), TARGET = arg("target", ""), JSON_OUT = process.argv.includes("--json");
const PORT = +arg("port", 18092), CTRL = +arg("control-port", 50272), BASE = +arg("base-port", 50552), ROOT = path.resolve(__dirname, "..");
const up = u => new Promise(r => http.get(u, res => { res.resume(); r(res.statusCode === 200); }).on("error", () => r(false)));
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const srv = spawn("python3", [path.join(ROOT, "sim/demo_server.py"), "--dashboard", "--dashboard-port", String(PORT), "--control-port", String(CTRL), "--base-port", String(BASE)],
    { cwd: ROOT, stdio: process.env.CHECK_SERVER_LOG ? ["ignore", "ignore", require("fs").openSync(process.env.CHECK_SERVER_LOG, "a")] : "ignore", detached: true });   // CHECK_SERVER_LOG=file keeps the simulator's error output
  const kill = () => { try { process.kill(-srv.pid, "SIGKILL"); } catch (e) {} }; process.on("exit", kill);
  for (let i = 0; i < 120 && !(await up(`http://127.0.0.1:${PORT}/healthz`)); i++) await sleep(500);
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 1500, height: 1100 } });
  const errs = []; p.on("pageerror", e => errs.push(String(e)));
  await p.goto(`http://127.0.0.1:${PORT}/?mode=demo`);
  await p.waitForSelector("#nodes .card", { timeout: 60000 });
  await p.waitForSelector("#simstart", { state: "attached", timeout: 60000 });
  const ids = await p.evaluate(() => [...document.querySelectorAll("#simsel option")].map(o => o.value));
  const results = [];
  for (const id of ids.filter(i => !ONLY.length || ONLY.includes(i))) {
    try { await p.waitForFunction(() => !document.getElementById("simstart").disabled, null, { timeout: 120000 }); }
    catch (e) {                                                                   // the Start button never came back: say why instead of dying silently
      const why = await p.evaluate(async () => ({ stat: document.getElementById("simstat").textContent, state: await (await fetch("api/sim/state")).json().catch(() => null) }));
      results.push({ id, target: "-", secs: -1, countdownSeen: false, ok: false, alerts: 0, suspicious: 0, evidence: 0, actions: 0, panels: [], detail: "START BUTTON STUCK: " + JSON.stringify(why).slice(0, 400) });
      await p.click("#simreset").catch(() => {}); await sleep(3000); continue;
    }
    await p.selectOption("#simsel", id);
    await sleep(1500);                                                           // the page rebuilds the service list for this scenario
    const hasTarget = await p.evaluate(() => !!document.getElementById("simtarget").value);
    if (TARGET && hasTarget) { await p.selectOption("#simtarget", TARGET).catch(() => {}); await sleep(800); }
    const target = await p.evaluate(() => document.getElementById("simtarget").value || "-");
    const t0 = Date.now(); await p.click("#simstart");
    let countdownSeen = false;
    for (let i = 0; i < 400; i++) {
      await sleep(500);
      const st = await p.evaluate(() => document.getElementById("simstat").textContent);
      if (/begins in/.test(st)) countdownSeen = true;
      if (Date.now() - t0 > 3000 && /ready|press Reset/.test(st)) break;
    }
    const secs = Math.round((Date.now() - t0) / 1000);
    await sleep(4000);                                                           // the recorder's tail + one page refresh
    const snap = await p.evaluate(async () => {
      const r = await (await fetch("api/sim/run")).json();
      const rows = sel => document.querySelectorAll(sel + " tr").length;
      return {
        alerts: document.querySelectorAll("#inctable tr.click").length, suspicious: document.querySelectorAll("#susptable td .tag").length, evidence: Math.max(0, rows("#snaps") - 1), actions: Math.max(0, rows("#acts") - 1), recnone: document.getElementById("recnone").textContent.slice(0, 60),
        panels: (r.panels || []).map(q => q.id + ":" + q.series.reduce((a, s) => a + s.points.length, 0)), markers: (r.markers || []).map(m => m.label), summary: r.summary, ok: (r.meta || {}).ok, detail: (r.meta || {}).detail,
        attack_at: (r.meta || {}).attack_at };
    });
    results.push({ id, target, secs, countdownSeen, ...snap });
    await p.click("#simreset"); await sleep(1000);
  }
  await b.close(); kill();
  if (JSON_OUT) console.log(JSON.stringify({ results, pageErrors: errs }, null, 1));
  else {
    for (const r of results) console.log(`${r.id.padEnd(26)} on ${r.target}  ${String(r.secs).padStart(3)}s  countdown:${r.countdownSeen ? "yes" : "NO "}  ok:${r.ok}  alerts:${r.alerts} suspicious:${r.suspicious} evidence:${r.evidence} actions:${r.actions}  graphs: ${r.panels.join(" ")}\n    run: detected ${r.summary.detected_s}s isolated ${r.summary.isolated_s}s check ${r.summary.recovery_check_s}s restored ${r.summary.full_access_s}s | ${String(r.detail).slice(0, 110)}`);
    if (errs.length) console.log("PAGE ERRORS:", errs.slice(0, 5));
  }
  process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
