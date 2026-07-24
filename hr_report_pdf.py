"""
hr_report_pdf.py — cc#652 Portfolio Health Report PDF (server-side, white-label, WeasyPrint / Option B).

GET  /api/health/report_pdf/{pid}?exp=&sig=  -> application/pdf of the WHITE-LABEL Health Report.
GET  /api/health/report_pdf_link/{pid}       -> {url, expires_in}: mints a short-lived (~10 min) signed
                                                token and returns the absolute public URL. Admin-gated
                                                (X-Admin-Token) — the MCP tool Scorr:hr_report_pdf calls
                                                it so Claude web can web_fetch the PDF with no login.

Design notes:
 - ALWAYS white-label (zero Scorr branding) — this endpoint exists for client sharing.
 - The PDF URL is protected by an HMAC(pid+expiry) token, not a login session, and is never permanently
   open (expires in _TOKEN_TTL seconds).
 - WeasyPrint is imported LAZILY inside the render path: a missing native lib (pango/cairo/gdk-pixbuf)
   returns HTTP 503 with a clear message instead of crashing app boot. requirements.txt + nixpacks.toml
   carry the runtime deps; Claude-web verifies the Railway build per the task closure clause.
"""
import os
import hmac
import time
import hashlib
import html as _html
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from hr_report import build_report, _conn

router = APIRouter()

_SECRET = (os.getenv("ADMIN_TOKEN") or os.getenv("DATABASE_URL") or "hr-pdf-fallback-secret").encode()
_PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "https://scorr.in").rstrip("/")
_TOKEN_TTL = 600   # 10 minutes


# ── signed short-lived token ────────────────────────────────────────────────────
def _sign(pid, exp):
    return hmac.new(_SECRET, f"{pid}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def _token_valid(pid, exp, sig):
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(sig or "", _sign(pid, exp))


def _admin_ok(request):
    want = os.getenv("ADMIN_TOKEN")
    return (not want) or (request.headers.get("X-Admin-Token") == want)


# ── formatting helpers ──────────────────────────────────────────────────────────
def _esc(s):
    return _html.escape("" if s is None else str(s))


def _money(n):
    if n is None:
        return "—"
    try:
        return "&#8377;" + format(int(round(float(n))), ",d")
    except (TypeError, ValueError):
        return "—"


def _pct(n, dp=1, signed=True):
    if n is None:
        return "—"
    try:
        v = float(n)
        s = ("+" if v >= 0 else "&#8722;") if signed else ("&#8722;" if v < 0 else "")
        return s + f"{abs(v):.{dp}f}%"
    except (TypeError, ValueError):
        return "—"


def _f2(n):
    if n is None:
        return "—"
    try:
        return f"{float(n):.2f}"
    except (TypeError, ValueError):
        return "—"


def _gcol(g):
    try:
        g = float(g)
    except (TypeError, ValueError):
        return "#5B667D"
    return "#0B6E42" if g >= 7 else ("#8F5C07" if g >= 5 else "#B52432")


def _chip_cls(call):
    return {"Strong": "g", "Good": "g", "Excellent": "g", "Neutral": "b",
            "Weak": "r", "Caution": "a", "Moderate": "a"}.get(call, "b")


# ── the white-label print HTML ──────────────────────────────────────────────────
def render_report_html(rep):
    """Build a self-contained, white-label, print-friendly HTML string from a build_report() dict.
    Simplified block/table layout (no CSS grid) so WeasyPrint renders it reliably."""
    port = rep.get("portfolio") or {}
    snap = rep.get("snapshot") or {}
    rt = rep.get("ratings") or {}
    bench = rep.get("benchmark") or {}
    val = rep.get("valuation") or {}
    yld = rep.get("yield") or {}
    sector = rep.get("sector") or {}
    name = port.get("name") or "Portfolio"
    today = date.today().strftime("%d %b %Y")

    def _bar(label, score, colr):
        w = max(0, min(100, (float(score) * 10) if score is not None else 0))
        return (f'<tr><td class="rl">{_esc(label)}</td>'
                f'<td><div class="track"><div class="fill" style="width:{w:.0f}%;background:{colr};"></div></div></td>'
                f'<td class="rv">{_f2(score)}</td></tr>')

    # movers
    def _mover_rows(items, pos):
        if not items:
            return '<div class="muted">—</div>'
        out = []
        for h in items:
            col = "#0B6E42" if pos else "#B52432"
            out.append(f'<div class="mv"><span class="mv-s">{_esc(h.get("symbol"))}</span>'
                       f'<span class="mv-v" style="color:{col};">{_pct(h.get("pnl_pct"))}</span></div>')
        return "".join(out)

    # sector table
    sec_rows = ""
    for s in (sector.get("table") or []):
        sec_rows += (f'<tr><td class="l">{_esc(s.get("segment"))}</td>'
                     f'<td>{_pct(s.get("weight"), 0, signed=False)}</td>'
                     f'<td>{_f2(s.get("score"))}</td>'
                     f'<td><span class="chip {_chip_cls(s.get("call"))}">{_esc(s.get("call"))}</span></td></tr>')

    # holdings table
    hold_rows = ""
    for h in (rep.get("holdings") or []):
        gcol = _gcol(h.get("gvm"))
        pcol = "#0B6E42" if (h.get("pnl_pct") or 0) >= 0 else "#B52432"
        hold_rows += (
            f'<tr><td class="l" style="font-weight:700;">{_esc(h.get("symbol"))}</td>'
            f'<td>{_money(h.get("cmp"))}</td>'
            f'<td>{_esc(h.get("qty"))}</td>'
            f'<td>{_pct(h.get("weight"), 1, signed=False)}</td>'
            f'<td style="color:{pcol};">{_pct(h.get("pnl_pct"))}</td>'
            f'<td style="color:{gcol};font-weight:700;">{_f2(h.get("gvm"))}</td>'
            f'<td>{_pct(h.get("from_ath"), 0)}</td>'
            f'<td><span class="chip {_chip_cls((h.get("verdict")))}">{_esc(h.get("action") or h.get("verdict") or "—")}</span></td></tr>')

    # result analysis
    ra_rows = ""
    for r in (rep.get("result_analysis") or []):
        ra_rows += (f'<div class="flag"><b>{_esc(r.get("symbol"))}</b> '
                    f'<span class="chip {_chip_cls(r.get("chip"))}">{_esc(r.get("chip") or "")}</span><br>'
                    f'{_esc(r.get("analysis"))}</div>')
    if not ra_rows:
        ra_rows = '<div class="muted">No fresh results in the last 45 days.</div>'

    # red flags
    rf_rows = ""
    for f in (rep.get("red_flags") or []):
        rf_rows += (f'<div class="flag"><b>{_esc(f.get("symbol"))}</b> — '
                    f'{_esc(f.get("flag"))}: {_esc(f.get("detail"))}</div>')
    if not rf_rows:
        rf_rows = '<div class="muted">No pledge / FII-exit / deep-drawdown flags.</div>'

    # replacements
    rep_rows = ""
    for r in (rep.get("replacements") or []):
        peers = ", ".join(f'{_esc(p.get("symbol"))} ({_f2(p.get("gvm"))})' for p in (r.get("peers") or [])[:3])
        rep_rows += (f'<div class="flag"><b>{_esc(r.get("holding"))}</b> ({_esc(r.get("segment"))}) — '
                     f'consider: {peers}</div>')
    if not rep_rows:
        rep_rows = '<div class="muted">No Avoid-Exit holdings — nothing to replace.</div>'

    # highlights
    hl = "".join(f'<li>{_esc(x)}</li>' for x in (rep.get("highlights") or []) if x)

    pnl_col = "#0B6E42" if (snap.get("pnl_abs") or 0) >= 0 else "#B52432"
    overall = rt.get("overall")

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 12mm 12mm 14mm; }}
* {{ box-sizing: border-box; }}
body {{ font-family: 'DejaVu Sans Mono', monospace; color:#07111F; font-size:9.5px; margin:0; }}
h1,h2,h3 {{ margin:0; }}
.mast {{ background:#07111F; color:#fff; padding:16px 18px; }}
.mast .k {{ font-size:7.5px; letter-spacing:1px; color:rgba(255,255,255,.4); text-transform:uppercase; }}
.mast .nm {{ font-size:22px; font-weight:800; margin-top:6px; }}
.mast .sub {{ font-size:8.5px; color:rgba(255,255,255,.5); margin-top:4px; }}
.mast .rate {{ float:right; text-align:right; margin-top:-40px; }}
.mast .rate .big {{ font-size:40px; font-weight:800; line-height:1; }}
.strip {{ display:table; width:100%; border-collapse:collapse; margin-top:0; }}
.strip .c {{ display:table-cell; width:25%; padding:10px 14px; border-right:1px solid #E5E8EF; border-bottom:1px solid #E5E8EF; }}
.lbl {{ font-size:7.5px; font-weight:700; letter-spacing:.6px; text-transform:uppercase; color:#9098A8; }}
.big {{ font-size:15px; font-weight:700; margin-top:3px; }}
.sec {{ padding:12px 16px; border-bottom:1px solid #E5E8EF; }}
.sec h3 {{ font-size:8px; letter-spacing:.7px; text-transform:uppercase; color:#9098A8; margin-bottom:8px; }}
table {{ width:100%; border-collapse:collapse; }}
th {{ font-size:7.5px; text-transform:uppercase; letter-spacing:.4px; color:#9098A8; text-align:right; padding:4px 6px; border-bottom:1px solid #E5E8EF; }}
th:first-child, td.l {{ text-align:left; }}
td {{ font-size:9px; padding:5px 6px; border-bottom:1px solid #EEF0F4; text-align:right; white-space:nowrap; }}
.track {{ height:3px; background:#E5E8EF; border-radius:2px; overflow:hidden; }}
.fill {{ height:3px; }}
td.rl {{ color:#5B667D; width:90px; }} td.rv {{ font-weight:700; width:40px; }}
.chip {{ font-size:7.5px; font-weight:600; padding:1px 6px; border-radius:2px; }}
.chip.g {{ background:rgba(11,110,66,.12); color:#0B6E42; }}
.chip.r {{ background:rgba(181,36,50,.12); color:#B52432; }}
.chip.a {{ background:rgba(143,92,7,.12); color:#8F5C07; }}
.chip.b {{ background:rgba(24,71,223,.12); color:#1847DF; }}
.mv {{ display:inline-block; margin-right:14px; }} .mv-s {{ font-weight:700; }} .mv-v {{ margin-left:5px; }}
.flag {{ font-size:9px; color:#5B667D; margin:5px 0; line-height:1.5; }} .flag b {{ color:#07111F; }}
.muted {{ font-size:9px; color:#9098A8; }}
.cols {{ display:table; width:100%; }} .col {{ display:table-cell; width:50%; vertical-align:top; padding:12px 16px; }}
.col:first-child {{ border-right:1px solid #E5E8EF; }}
ul {{ margin:0; padding-left:16px; }} li {{ font-size:9px; color:#5B667D; margin:3px 0; line-height:1.5; }}
.note {{ font-size:8.5px; color:#5B667D; margin-top:6px; }}
.take {{ background:#07111F; color:rgba(255,255,255,.85); padding:14px 16px; font-size:9.5px; line-height:1.7; }}
</style></head><body>
<div class="mast">
  <div class="k">Portfolio Health Report &middot; {today}</div>
  <div class="nm">{_esc(name)}</div>
  <div class="sub">{snap.get("holdings_count") or 0} holdings &middot; NSE/BSE &middot; as of {today}</div>
  <div class="rate"><div class="lbl" style="color:rgba(255,255,255,.4);">Overall Rating</div>
    <div class="big" style="color:{_gcol(overall)};">{_f2(overall)}</div>
    <div><span class="chip {_chip_cls(rt.get('verdict'))}">{_esc(rt.get('verdict') or '')}</span></div></div>
</div>
<div class="strip">
  <div class="c"><div class="lbl">Invested</div><div class="big">{_money(snap.get('invested'))}</div></div>
  <div class="c"><div class="lbl">Current Value</div><div class="big">{_money(snap.get('current'))}</div></div>
  <div class="c"><div class="lbl">P&amp;L</div><div class="big" style="color:{pnl_col};">{_money(snap.get('pnl_abs'))}</div>
    <div style="font-size:9px;color:{pnl_col};">{_pct(snap.get('pnl_pct'))}</div></div>
  <div class="c" style="border-right:none;"><div class="lbl">Alpha vs Nifty 500 &middot; 1yr</div>
    <div class="big" style="color:#1847DF;">{_pct(snap.get('alpha'))}</div></div>
</div>
<div class="cols">
  <div class="col">
    <h3 class="lbl" style="margin-bottom:8px;">Rating Parameters</h3>
    <table>{_bar('Growth', rt.get('growth'), '#0B6E42')}{_bar('Value', rt.get('value'), '#1A9070')}{_bar('Momentum', rt.get('momentum'), '#1847DF')}{_bar('Quality mix', rt.get('quality'), '#5340C2')}</table>
    <div class="note">{_esc(rt.get('insight'))}</div>
  </div>
  <div class="col">
    <h3 class="lbl" style="margin-bottom:8px;">Valuation &amp; Yield</h3>
    <table><tr><td class="l">Portfolio PE</td><td>{_f2(val.get('portfolio_pe'))}</td><td class="l">Yield</td><td>{_pct(yld.get('portfolio_yield'),2,signed=False)}</td></tr>
    <tr><td class="l">Sector PE</td><td>{_f2(val.get('sector_pe'))}</td><td class="l">Sector Yld</td><td>{_pct(yld.get('sector_yield'),2,signed=False)}</td></tr>
    <tr><td class="l">Nifty PE</td><td>{_f2(val.get('nifty_pe'))}</td><td class="l">1yr Port</td><td>{_pct(bench.get('portfolio_1y'))}</td></tr></table>
    <div class="note">{_esc(val.get('insight'))}</div>
  </div>
</div>
<div class="sec"><h3>Sector Allocation</h3>
  <table><tr><th>Sector</th><th>Weight</th><th>Score</th><th>Call</th></tr>{sec_rows or '<tr><td class="l muted">No sector data</td></tr>'}</table>
  <div class="note">{_esc(sector.get('insight'))}</div></div>
<div class="cols">
  <div class="col"><h3 class="lbl" style="margin-bottom:8px;">Top Gainers</h3>{_mover_rows(rep.get('gainers'), True)}</div>
  <div class="col"><h3 class="lbl" style="margin-bottom:8px;">Top Losers</h3>{_mover_rows(rep.get('losers'), False)}</div>
</div>
<div class="sec"><h3>Holdings — Full Detail</h3>
  <table><tr><th>Stock</th><th>CMP</th><th>Qty</th><th>Weight</th><th>P&amp;L %</th><th>Rating</th><th>From ATH</th><th>Verdict</th></tr>{hold_rows}</table></div>
<div class="cols">
  <div class="col"><h3 class="lbl" style="margin-bottom:8px;">Latest Result Analysis</h3>{ra_rows}</div>
  <div class="col"><h3 class="lbl" style="margin-bottom:8px;">Red Flags</h3>{rf_rows}</div>
</div>
<div class="sec"><h3>Replacement Ideas</h3>{rep_rows}
  <div class="note">{_esc(rep.get('replacement_note'))}</div></div>
<div class="sec"><h3>Key Highlights</h3><ul>{hl}</ul></div>
<div class="take"><div class="lbl" style="color:rgba(255,255,255,.35);margin-bottom:8px;">Expert Take</div>{_esc(rep.get('expert_take'))}
  <div style="font-size:7.5px;color:rgba(255,255,255,.3);margin-top:12px;">Data as of report date &middot; Research only, not investment advice.</div></div>
</body></html>"""


def _pdf_filename(name):
    safe = "".join(c if (c.isalnum() or c in " -_") else "" for c in (name or "Portfolio")).strip().replace(" ", "_")
    return f"Portfolio_Health_{safe or 'Portfolio'}_{date.today().strftime('%d%b%Y')}.pdf"


@router.get("/api/health/report_pdf_link/{pid}")
def report_pdf_link(pid: int, request: Request):
    """cc#652: mint a short-lived signed PDF URL for the MCP bridge (admin-gated). Claude web fetches it."""
    if not _admin_ok(request):
        return JSONResponse({"error": "admin token required"}, status_code=403)
    exp = int(time.time()) + _TOKEN_TTL
    sig = _sign(pid, exp)
    url = f"{_PUBLIC_BASE}/api/health/report_pdf/{pid}?exp={exp}&sig={sig}"
    return {"portfolio_id": pid, "url": url, "expires_in": _TOKEN_TTL}


@router.get("/api/health/report_pdf/{pid}")
def report_pdf(pid: int, exp: str = "", sig: str = ""):
    """cc#652: white-label Portfolio Health PDF. Requires a valid HMAC(pid+exp) token (~10 min TTL)."""
    if not _token_valid(pid, exp, sig):
        return JSONResponse({"error": "invalid or expired token"}, status_code=403)
    try:
        with _conn() as conn, conn.cursor() as cur:
            rep = build_report(cur, pid)
    except Exception as e:
        return JSONResponse({"error": f"report engine failed: {str(e)[:200]}"}, status_code=500)
    if not rep or rep.get("error"):
        return JSONResponse({"error": (rep or {}).get("error", "report unavailable")}, status_code=404)
    html_str = render_report_html(rep)
    try:
        from weasyprint import HTML   # lazy — a missing native lib 503s here, never at app boot
    except Exception as e:
        return JSONResponse({"error": f"PDF renderer unavailable: {str(e)[:200]}"}, status_code=503)
    try:
        pdf_bytes = HTML(string=html_str).write_pdf()
    except Exception as e:
        return JSONResponse({"error": f"PDF render failed: {str(e)[:200]}"}, status_code=500)
    fname = _pdf_filename((rep.get("portfolio") or {}).get("name"))
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{fname}"'})
