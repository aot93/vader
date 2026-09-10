"""One-off: capture raw + annotated screenshots of every major Vader screen.

Run with the app already serving on $BASE (default http://localhost:8077) and
demo data seeded (scripts/dev_seed.py). Produces:
  assets/raw/<name>.png        clean capture
  assets/<name>.png            same, with numbered call-out overlays

Annotations are injected into the DOM as absolutely-positioned badges/boxes so
we do not need an image library.
"""
from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://localhost:8077")
OUT = Path(__file__).parent / "assets"
RAW = OUT / "raw"
OUT.mkdir(parents=True, exist_ok=True)
RAW.mkdir(parents=True, exist_ok=True)

OVERLAY_JS = r"""
(annos) => {
  document.querySelectorAll('.vader-anno').forEach(e => e.remove());
  const mk = (tag, css) => { const el = document.createElement(tag); el.className='vader-anno'; Object.assign(el.style, css); return el; };
  annos.forEach((a, i) => {
    let el = null;
    try { el = a.nth ? document.querySelectorAll(a.sel)[a.nth] : document.querySelector(a.sel); } catch(e){}
    if (!el) { console.log('anno miss', a.sel); return; }
    const r = el.getBoundingClientRect();
    const top = r.top + window.scrollY, left = r.left + window.scrollX;
    const box = mk('div', {
      position:'absolute', top:(top-4)+'px', left:(left-4)+'px',
      width:(r.width+8)+'px', height:(r.height+8)+'px',
      border:'3px solid #e0245e', borderRadius:'6px', zIndex:99998,
      boxShadow:'0 0 0 3px rgba(224,36,94,0.25)', pointerEvents:'none'
    });
    document.body.appendChild(box);
    const badge = mk('div', {
      position:'absolute', top:(top-16)+'px', left:(left-16)+'px',
      width:'26px', height:'26px', background:'#e0245e', color:'#fff',
      font:'700 15px/26px system-ui,sans-serif', textAlign:'center',
      borderRadius:'50%', zIndex:99999, pointerEvents:'none',
      boxShadow:'0 1px 4px rgba(0,0,0,0.4)'
    });
    badge.textContent = (a.n != null ? a.n : (i+1));
    document.body.appendChild(badge);
  });
}
"""

SHOTS = [
    dict(name="dashboard", url="/", annos=[
        dict(sel=".grid.cols-4", n=1),
        dict(sel=".warnbox", n=2),
        dict(sel=".grid.cols-2 .panel", nth=0, n=3),
        dict(sel=".grid.cols-2 .panel", nth=1, n=4),
        dict(sel=".grid.cols-2 .panel", nth=2, n=5),
    ]),
    dict(name="library", url="/library", annos=[
        dict(sel="form[action='/library/inventory'] button", n=1),
        dict(sel=".grid.cols-2", n=2),
        dict(sel="h2", nth=1, n=3),
        dict(sel="form[action='/library/format']", n=4),
        dict(sel="form[action='/library/clean']", n=5),
        dict(sel="form[action='/library/retire']", n=6),
    ]),
    dict(name="tapes-list", url="/tapes", annos=[
        dict(sel="a[href='/tapes/new']", n=1),
        dict(sel="p .muted", n=2),
        dict(sel="table tr", nth=1, n=3),
    ]),
    dict(name="tape-detail", url="/tapes/TEST001L8", annos=[
        dict(sel=".grid.cols-2 .panel", nth=0, n=1),
        dict(sel="a[href$='.csv']", n=2),
        dict(sel="form[action='/jobs/new/verify'] button", n=3),
        dict(sel=".grid.cols-2 .panel", nth=1, n=4),
        dict(sel="h2", nth=1, n=5),
        dict(sel="h2", nth=2, n=6),
        dict(sel="h2", nth=3, n=7),
    ]),
    dict(name="search", url="/search?q=seq010", annos=[
        dict(sel="form[action='/search']", n=1),
        dict(sel="input[name='destination_path']", n=2),
        dict(sel="input[name='include_manifests']", n=3),
        dict(sel="button", nth=2, n=4),
        dict(sel="table tr", nth=1, n=5),
        dict(sel="table td", nth=7, n=6),
    ]),
    dict(name="job-write-form", url="/jobs/new/write", annos=[
        dict(sel="input[name='source_path']", n=1),
        dict(sel="select[name='mode']", n=2),
        dict(sel="input[name='greedy_source']", n=3),
        dict(sel="select[name='backup_category']", n=4),
        dict(sel="input[name='drive']", n=5),
        dict(sel="button", nth=1, n=6),
    ]),
    dict(name="jobs-list", url="/jobs", annos=[
        dict(sel="a[href='/jobs/new/write']", n=1),
        dict(sel="form[action='/jobs/new/backup'] button", n=2),
        dict(sel="table tr", nth=1, n=3),
    ]),
    dict(name="job-detail", url="/jobs/1", annos=[
        dict(sel=".panel", nth=0, n=1),
        dict(sel="pre.plan", nth=0, n=2),
        dict(sel="h2", nth=1, n=3),
        dict(sel="p:last-of-type", n=4),
    ]),
    dict(name="restore-detail", url="/restores/1", annos=[
        dict(sel="p.tag", nth=0, n=1),
        dict(sel=".warnbox", n=2),
        dict(sel="h2", nth=1, n=3),
        dict(sel="pre.plan", n=4),
        dict(sel="form[action$='/run'] button", n=5),
    ]),
    dict(name="restores-list", url="/restores", annos=[
        dict(sel="table tr", nth=1, n=1),
    ]),
    dict(name="audit", url="/audit", annos=[
        dict(sel="h2", nth=0, n=1),
        dict(sel="h2", nth=1, n=2),
    ]),
]


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1360, "height": 1000},
                                device_scale_factor=2)
        for shot in SHOTS:
            page.goto(BASE + shot["url"], wait_until="networkidle")
            page.wait_for_timeout(350)
            RAW_p = RAW / f"{shot['name']}.png"
            page.screenshot(path=str(RAW_p), full_page=True)
            if shot.get("annos"):
                page.evaluate(OVERLAY_JS, shot["annos"])
                page.wait_for_timeout(150)
            page.screenshot(path=str(OUT / f"{shot['name']}.png"), full_page=True)
            print("captured", shot["name"])
        browser.close()


if __name__ == "__main__":
    main()
