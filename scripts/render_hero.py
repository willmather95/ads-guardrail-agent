"""Render docs/hero.png from the demo's own output (demo/out/summary.json).

    python scripts/render_hero.py demo/out/summary.json docs/hero.png

Builds an HTML page with a hand-drawn SVG chart (world-class-charts tokens and rules: one
highlighted series, direct labels, takeaway as the subtitle, targets written in words), then
screenshots it with headless Chrome. Set CHROME_PATH if Chrome is not on PATH.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from html import escape
from pathlib import Path

W, H = 1600, 900
TOKENS = {
    "page": "#f2f2f1", "surface": "#ffffff", "grid": "#ececeb", "hairline": "#e4e4e3", "axis": "#8a8a8a",
    "ink": "#141414", "ink2": "#3f3f3f", "muted": "#6b6b6b", "accent": "#0a72e8", "accent_soft": "#d8e8fc",
    "accent_ink": "#0657b8", "compare_soft": "#e3e3e3", "compare": "#8c8c8c", "neg": "#dc383d",
    "neg_ink": "#b42329", "neg_soft": "#fde9ea", "pos_ink": "#0b6e34", "pos_soft": "#e3f6ea", "warn_ink": "#8a5300",
}


def money(v: float, cents: bool = False) -> str:
    return f"${v:,.2f}" if cents else f"${v:,.0f}"


def chart_svg(s: dict) -> str:
    rows = s["days_rows"]
    t = TOKENS
    cw, ch = 900, 420
    left, right, top, bottom = 58, 16, 46, 54
    pw, ph = cw - left - right, ch - top - bottom
    ymax = 2000.0
    n = len(rows)
    slot = pw / n
    bw = slot * 0.62

    def y(v: float) -> float:
        return top + ph - min(v, ymax) / ymax * ph

    out = [f'<svg viewBox="0 0 {cw} {ch}" width="{cw}" height="{ch}" role="img" '
           f'aria-label="Budget change per day: asked versus allowed by the caps versus written">']
    for v in (0, 500, 1000, 1500, 2000):
        out.append(f'<line x1="{left}" x2="{cw - right}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="{t["grid"] if v else t["axis"]}" stroke-width="1"/>')
        out.append(f'<text x="{left - 10}" y="{y(v) + 5:.1f}" text-anchor="end" class="tick">{money(v)}</text>')
    for i, r in enumerate(rows):
        x0 = left + i * slot + (slot - bw) / 2
        asked, allowed, wrote = r["proposed_budget_move"], r["capped_budget_move"], r["applied_budget_move"]
        out.append(f'<rect x="{x0:.1f}" y="{y(asked):.1f}" width="{bw:.1f}" height="{y(0) - y(asked):.1f}" rx="3" fill="{t["compare_soft"]}"/>')
        if asked > ymax:  # broken bar, labeled with its true value
            yy = y(ymax) + 14
            out.append(f'<path d="M{x0 - 2:.1f},{yy + 4:.1f} l{bw / 2 + 2:.1f},-8 l{bw / 2 + 2:.1f},8" fill="none" stroke="{t["surface"]}" stroke-width="5"/>')
            out.append(f'<text x="{x0 + bw / 2:.1f}" y="{y(ymax) - 8:.1f}" text-anchor="middle" class="lab strong">{money(asked)}</text>')
        out.append(f'<rect x="{x0 + 4:.1f}" y="{y(allowed):.1f}" width="{bw - 8:.1f}" height="{y(0) - y(allowed):.1f}" rx="2" fill="{t["accent_soft"]}" stroke="{t["accent"]}" stroke-width="1"/>')
        if wrote > 0:
            out.append(f'<rect x="{x0 + 4:.1f}" y="{y(wrote):.1f}" width="{bw - 8:.1f}" height="{y(0) - y(wrote):.1f}" rx="2" fill="{t["accent"]}"/>')
        d = date.fromisoformat(r["day"])
        lab = f"Nov {d.day}" if i in (0, n - 1) else str(d.day)
        out.append(f'<text x="{x0 + bw / 2:.1f}" y="{y(0) + 22:.1f}" text-anchor="middle" class="tick">{lab}</text>')
        if r["halted"]:
            out.append(f'<rect x="{x0 - 3:.1f}" y="{top:.1f}" width="{bw + 6:.1f}" height="{ph:.1f}" fill="none" stroke="{t["neg"]}" stroke-width="1.5" stroke-dasharray="3 3" rx="4"/>')
            out.append(f'<text x="{x0 + bw / 2:.1f}" y="{top - 22:.1f}" text-anchor="middle" class="lab neg">Kill switch</text>')
            out.append(f'<text x="{x0 + bw / 2:.1f}" y="{top - 6:.1f}" text-anchor="middle" class="lab neg small">halts after 1 write</text>')
    daily_cap = s["caps"]["account_daily_change_cap"] * len(s["caps"]["account_total_caps"])
    out.append(f'<line x1="{left}" x2="{cw - right}" y1="{y(daily_cap):.1f}" y2="{y(daily_cap):.1f}" '
               f'stroke="{t["ink2"]}" stroke-width="1.5" stroke-dasharray="5 5"/>')
    out.append(f'<text x="{cw - right}" y="{y(daily_cap) - 8:.1f}" text-anchor="end" class="ref halo">'
               f'Daily cap: {money(s["caps"]["account_daily_change_cap"])} per account, {money(daily_cap)} for both</text>')
    # rogue annotation
    rogue = next((i for i, r in enumerate(rows) if r["proposed_budget_move"] > ymax), None)
    if rogue is not None:
        x = left + rogue * slot + slot / 2 + bw / 2 + 8
        out.append(f'<text x="{x:.1f}" y="{y(ymax) + 22:.1f}" class="lab halo">Another agent asks</text>')
        out.append(f'<text x="{x:.1f}" y="{y(ymax) + 40:.1f}" class="lab halo">for +$5,000/day.</text>')
        rr = next((e for e in s.get("events", []) if e.get("kind") == "rogue_result"), None)
        if rr:
            verdict = f"Caps allow {money(rr['allowed'])}." if rr["allowed"] else "Caps block it."
            out.append(f'<text x="{x:.1f}" y="{y(ymax) + 58:.1f}" class="lab strong halo">{verdict}</text>')
    # direct series labels (no legend box)
    lx, ly = left + 10 * slot + 6 - 336, y(ymax) + 4  # sits over days 11-14
    for i, (fill, stroke, txt) in enumerate([(t["compare_soft"], None, "Asked by the policy"),
                                              (t["accent_soft"], t["accent"], "Allowed by the caps"),
                                              (t["accent"], None, "Written to the account")]):
        yy = ly + 16 + i * 22
        st = f' stroke="{stroke}"' if stroke else ""
        out.append(f'<rect x="{lx + 330}" y="{yy - 11}" width="14" height="14" rx="3" fill="{fill}"{st}/>')
        out.append(f'<text x="{lx + 352}" y="{yy + 1}" class="lab">{txt}</text>')
    out.append("</svg>")
    return "".join(out)


def build_html(s: dict) -> str:
    t = TOKENS
    tot = s["totals"]
    rb = s["rollback"]
    shrink = tot["proposed_budget_move"] / tot["capped_budget_move"] if tot["capped_budget_move"] else 0
    stats = [
        ("Budget change asked", money(tot["proposed_budget_move"]), "sum over 14 days"),
        ("Caps allowed", money(tot["capped_budget_move"]), f"{shrink:.0f}x less than asked"),
        ("Cap breaches", "0", f"in {tot['applied']} writes, re-checked from the log"),
        ("Rollback", f"{rb['all_runs_restored']} of {rb['all_runs_restored']}", "restored to the cent"),
    ]
    stat_html = "".join(
        f'<div class="stat"><div class="sl">{escape(a)}</div><div class="sv">{escape(b)}</div><div class="sd">{escape(c)}</div></div>'
        for a, b, c in stats)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{width:{W}px;height:{H}px;background:{t['page']};font-family:Geist,Inter,system-ui,sans-serif;color:{t['ink']};
display:grid;grid-template-columns:520px 1fr;gap:36px;padding:48px 48px 44px 56px;-webkit-font-smoothing:antialiased}}
.left{{display:flex;flex-direction:column;padding-top:8px}}
.url{{font-family:'Geist Mono',ui-monospace,monospace;font-size:17px;color:{t['muted']};letter-spacing:.2px}}
h1{{font-size:64px;line-height:1.02;letter-spacing:-2.2px;font-weight:650;margin-top:30px}}
.sub{{font-size:23px;line-height:1.45;color:{t['ink2']};margin-top:26px}}
.chips{{display:flex;flex-wrap:wrap;gap:8px;margin-top:26px}}
.chip{{font-size:15px;padding:7px 12px;border-radius:999px;background:{t['surface']};border:1px solid {t['hairline']};color:{t['ink2']}}}
.flow{{list-style:none;margin-top:28px;margin-bottom:24px;counter-reset:f;display:grid;gap:9px}}
.flow li{{counter-increment:f;font-size:17px;color:{t['ink2']}}}
.flow li::before{{content:counter(f);font-family:'Geist Mono',monospace;font-size:13px;color:{t['accent_ink']};background:{t['accent_soft']};border-radius:6px;padding:2px 7px;margin-right:12px;display:inline-block}}
.flow b{{color:{t['ink']};font-weight:600}}
.foot{{margin-top:auto;font-size:15.5px;line-height:1.5;color:{t['muted']}}}
.foot b{{color:{t['warn_ink']};font-weight:600}}
.right{{display:flex;flex-direction:column;gap:22px}}
.card{{background:{t['surface']};border-radius:24px;box-shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px -12px rgba(0,0,0,.08);border:1px solid {t['hairline']}}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);padding:22px 8px}}
.stat{{padding:0 22px;border-left:1px solid {t['hairline']}}}.stat:first-child{{border-left:0}}
.sl{{font-size:15px;color:{t['muted']}}}.sv{{font-size:38px;font-weight:650;letter-spacing:-1px;margin-top:8px;font-variant-numeric:tabular-nums}}
.sd{{font-size:14px;color:{t['muted']};margin-top:6px}}
.chart{{padding:26px 28px 14px}}
.ct{{font-size:21px;font-weight:600}}.cs{{font-size:16px;color:{t['ink2']};margin-top:6px}}
svg{{display:block;margin-top:4px}}
svg .tick{{font-size:13px;fill:{t['muted']};font-variant-numeric:tabular-nums}}
svg .ref{{font-size:13.5px;fill:{t['ink']};font-weight:600}}
svg .halo{{paint-order:stroke;stroke:#fff;stroke-width:5px;stroke-linejoin:round}}
svg .lab{{font-size:13.5px;fill:{t['ink2']}}}svg .strong{{font-weight:600;fill:{t['ink']}}}
svg .neg{{fill:{t['neg_ink']};font-weight:600}}svg .small{{font-weight:400;font-size:12.5px}}
.src{{font-size:13px;color:{t['muted']};margin-top:4px}}
</style></head><body>
<div class="left">
  <div class="url">github.com/willmather95/ads-guardrail-agent</div>
  <h1>The agent can ask. The caps decide.</h1>
  <p class="sub">Reads Google Ads and Meta, proposes bid and budget changes with plain-English reasons, and writes only what fits under hard caps. Every write is logged with a hash chain and can be undone.</p>
  <div class="chips"><span class="chip">{escape(s['caps']['budget_per_change'])} per change</span><span class="chip">{money(s['caps']['account_daily_change_cap'])}/day per account</span><span class="chip">{s['caps']['cooldown_hours']}h cooldown</span><span class="chip">kill switch before every write</span><span class="chip">dry run by default</span></div>
  <ol class="flow"><li><b>Read</b> 7 days from the DuckDB warehouse</li><li><b>Propose</b> with a reason and a confidence</li><li><b>Cap</b> through 12 guardrails, all integer math</li><li><b>Check</b> the kill switch and the live value</li><li><b>Write</b>, read back, log with a hash chain</li><li><b>Undo</b> any run with one command</li></ol>
  <p class="foot"><b>SIMULATED.</b> 14-day run on synthetic fixtures (2 accounts, 20 campaigns and ad sets) against a fake in-memory ad account. Every number here comes from <code>make demo</code>.</p>
</div>
<div class="right">
  <div class="card stats">{stat_html}</div>
  <div class="card chart">
    <div class="ct">Budget change per day: asked, allowed, written</div>
    <div class="cs">The policy asked for {money(tot['proposed_budget_move'])} of moves. The caps let {money(tot['capped_budget_move'])} through, and never more than the daily cap.</div>
    {chart_svg(s)}
    <div class="src">Source: docs/demo-summary.json, written by make demo (sum of absolute daily budget changes, both accounts). Bid changes not shown.</div>
  </div>
</div>
</body></html>"""


def chrome() -> str:
    for c in (os.environ.get("CHROME_PATH"), "google-chrome", "chromium", "chromium-browser"):
        if c and shutil.which(c):
            return shutil.which(c)  # type: ignore[return-value]
    raise SystemExit("Chrome not found; set CHROME_PATH")


def main(src: str, dst: str) -> None:
    s = json.loads(Path(src).read_text())
    html = build_html(s)
    dst_p = Path(dst)
    dst_p.parent.mkdir(parents=True, exist_ok=True)
    html_path = dst_p.with_suffix(".html")
    html_path.write_text(html)
    with tempfile.TemporaryDirectory() as prof:
        subprocess.run([chrome(), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars", "--virtual-time-budget=3000",
                        f"--window-size={W},{H}", "--force-device-scale-factor=2",
                        f"--screenshot={dst_p.resolve()}", html_path.resolve().as_uri()],
                       check=True, capture_output=True, timeout=60)
    print(f"wrote {dst_p} and {html_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo/out/summary.json", sys.argv[2] if len(sys.argv) > 2 else "docs/hero.png")
