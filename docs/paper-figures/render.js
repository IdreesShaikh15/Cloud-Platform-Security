// node render.js  -> fig*.pdf (vector) and fig*.png (preview) from the SVG sources
const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const b = await chromium.launch();
  for (const n of ['fig1_architecture', 'fig2_workflow', 'fig3_testbed']) {
    const svg = fs.readFileSync(`${n}.svg`, 'utf8');
    const m = svg.match(/width="(\d+)" height="(\d+)"/);
    const w = +m[1], h = +m[2];
    const p = await b.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: 3 });
    await p.setContent(`<html><body style="margin:0">${svg}</body></html>`);
    await p.screenshot({ path: `${n}.png` });
    await p.pdf({ path: `${n}.pdf`, width: `${w}px`, height: `${h}px`, printBackground: true, pageRanges: '1' });
    await p.close();
  }
  await b.close();
})();
