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
import sys
import hmac
import time
import hashlib
import html as _html
import threading
import subprocess
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from hr_report import build_report, _conn
try:
    from scorr_auth import _is_authed   # cc#655: page-scoped mint gated on the browser login session
except Exception:   # pragma: no cover — keep the module importable if auth wiring moves
    def _is_authed(_request):
        return True

router = APIRouter()

_SECRET = (os.getenv("ADMIN_TOKEN") or os.getenv("DATABASE_URL") or "hr-pdf-fallback-secret").encode()
_PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "https://scorr.in").rstrip("/")
_TOKEN_TTL = 600   # 10 minutes


# ── cc#662: crash-isolated PDF render ────────────────────────────────────────────
# The reported symptom: SAVE PDF mints fine, then /report_pdf dies with "Site wasn't available"
# (connection reset), deterministically. Root cause shape: WeasyPrint's NATIVE stack
# (pango/cairo/gdk-pixbuf) can segfault or the render can OOM the ~85%-full Railway box — either
# way it kills the uvicorn WORKER mid-response, so the client sees a reset, NOT the lazy-import 503.
# Fix: render in a memory-capped SUBPROCESS (a crash there isolates -> the parent returns a clean
# JSON 503), serialized to concurrency 1 via a module lock (no parallel render spikes).
_render_lock = threading.Lock()
RENDER_TIMEOUT = 45
_MEM_LIMIT_BYTES = int(os.getenv("PDF_RENDER_MEM_MB", "1400")) * 1024 * 1024

_RENDER_SNIPPET = (
    "import sys\n"
    "from weasyprint import HTML\n"
    "data = sys.stdin.buffer.read().decode('utf-8')\n"
    "sys.stdout.buffer.write(HTML(string=data).write_pdf())\n"
)


def _rss_mb():
    """Current process RSS in MB (Linux /proc). None if unavailable — diagnostics only, never raises."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except Exception:
        pass
    return None


def _rlimit_preexec():   # pragma: no cover — runs in the child only
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (_MEM_LIMIT_BYTES, _MEM_LIMIT_BYTES))
    except Exception:
        pass


def _render_pdf_isolated(html_str, timeout=RENDER_TIMEOUT):
    """Render HTML->PDF in a memory-capped subprocess. Returns (pdf_bytes, None) or (None, error).
    NEVER raises and NEVER lets a native crash reach the uvicorn worker."""
    if not _render_lock.acquire(timeout=25):
        return None, "PDF renderer busy — another report is rendering, retry shortly"
    proc = None
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _RENDER_SNIPPET],
            input=html_str.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
            preexec_fn=(_rlimit_preexec if os.name == "posix" else None))
    except subprocess.TimeoutExpired:
        return None, f"PDF render timed out after {timeout}s (likely render hang / low memory)"
    except Exception as e:
        return None, f"PDF subprocess launch failed: {str(e)[:200]}"
    finally:
        _render_lock.release()
    if proc.returncode != 0 or not proc.stdout:
        stderr = (proc.stderr or b"").decode("utf-8", "replace")[-400:].strip()
        if proc.returncode is not None and proc.returncode < 0:
            reason = f"renderer killed by signal {-proc.returncode} (native segfault or OOM)"
        else:
            reason = f"renderer exited {proc.returncode}"
        return None, f"PDF render failed: {reason}{(' — ' + stderr) if stderr else ''}"[:400]
    return proc.stdout, None


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

# ── cc#1217: the pay-in / payout annexure and the XIRR line ────────────────────────────────

def _ledger_for_pdf(pid):
    """Counted rows, internal rows and totals for one portfolio, plus its XIRR.

    Returns a shape the renderer can use unconditionally. EVERY failure path lands on the same
    "no ledger" shape rather than raising, because a report that will not render is worse than a
    report with one honest line saying there is nothing to show. A client waiting on a PDF does
    not care which of our tables was missing.
    """
    out = {"counted": [], "internal": [], "payin": 0.0, "payout": 0.0, "net": None,
           "xirr": None, "nifty_xirr": None, "alpha_xirr": None}
    if not pid:
        return out
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT txn_date, segment, txn_type, amount, is_internal
                           FROM hr_ledger WHERE portfolio_id=%s
                           ORDER BY txn_date ASC, id ASC""", (pid,))
            for d, seg, ttype, amt, internal in cur.fetchall():
                row = {"d": d, "seg": seg, "type": ttype, "amt": float(amt or 0)}
                if internal:
                    out["internal"].append(row)
                else:
                    out["counted"].append(row)
                    if ttype == "PAYIN":
                        out["payin"] += row["amt"]
                    else:
                        out["payout"] += row["amt"]
            if out["counted"]:
                out["net"] = out["payin"] - out["payout"]
        try:
            # cc#1216 owns this computation. Read it, never recompute it - two implementations of
            # one rate is exactly how a PDF ends up disagreeing with the dashboard it came from.
            import hr_xirr
            x = hr_xirr.compute_xirr(pid)
            out["xirr"] = x.get("xirr")
            out["nifty_xirr"] = x.get("nifty_xirr")
            out["alpha_xirr"] = x.get("alpha_xirr")
        except Exception:
            pass
    except Exception:
        return {"counted": [], "internal": [], "payin": 0.0, "payout": 0.0, "net": None,
                "xirr": None, "nifty_xirr": None, "alpha_xirr": None}
    return out


def _xirr_line_html(lg):
    """Line 2 of the RETURN card. Line 1 is untouched - the founder's amendment is append-only.

    When it cannot be computed the line says WHAT IS MISSING rather than showing a dash. A reader
    holding a printed report cannot hover a tooltip, so the words have to carry it.
    """
    def sgn(v):
        return ("+" if v >= 0 else "&minus;") + ("%.1f%%" % abs(v))
    if lg.get("xirr") is None:
        return ('<div style="font-size:9.5px;color:#5B667D;margin-top:6px;">'
                'XIRR: needs pay-in / payout ledger</div>')
    parts = "XIRR " + sgn(lg["xirr"])
    if lg.get("nifty_xirr") is not None:
        parts += " vs Nifty 50 " + sgn(lg["nifty_xirr"])
    if lg.get("alpha_xirr") is not None:
        parts += " (" + sgn(lg["alpha_xirr"]) + " a year)"
    return ('<div style="font-size:9.5px;color:#5B667D;margin-top:6px;">%s</div>' % parts)


def _annexure_html(lg, invested):
    """The final page: every counted flow, its totals, and the net against page 1's Invested.

    NET INVESTED IS RECONCILED AGAINST PAGE 1 RATHER THAN ASSUMED TO MATCH. When the two differ
    the annexure prints BOTH and says the figure was set by hand. A client comparing the two
    pages will find the difference in seconds, and a report that shows one number twice with a
    quiet discrepancy between them is worse than one that names it.
    """
    if not lg["counted"] and not lg["internal"]:
        return ('<div class="sec" style="page-break-before:always;"><h3>Annexure &mdash; Pay-in / Payout</h3>'
                '<div class="note">No pay-in / payout ledger on record; '
                'return shown is simple P&amp;L on invested.</div></div>')

    def row(r, grey=False):
        style = ' style="color:#9098A8;"' if grey else ''
        label = ("Opening" if r["seg"] == "OPENING"
                 else ("Pay-in" if r["type"] == "PAYIN" else "Payout"))
        is_in = (r["type"] == "PAYIN")
        return ("<tr%s><td>%s</td><td>%s</td><td style='text-align:right'>%s</td>"
                "<td style='text-align:right'>%s</td></tr>"
                % (style, r["d"].strftime("%d %b %Y"), label,
                   _money2(r["amt"]) if is_in else "", "" if is_in else _money2(r["amt"])))

    body = "".join(row(r) for r in lg["counted"])
    foot = ("<tr style='font-weight:700;border-top:1px solid #E4E8EF;'>"
            "<td colspan='2'>Total</td><td style='text-align:right'>%s</td>"
            "<td style='text-align:right'>%s</td></tr>" % (_money2(lg["payin"]), _money2(lg["payout"])))
    net = lg["net"]
    mismatch = (invested is not None and net is not None and abs(float(invested) - net) > 1.0)
    foot += ("<tr style='font-weight:700;'><td colspan='2'>Net invested</td>"
             "<td colspan='2' style='text-align:right'>%s</td></tr>" % _money2(net))
    if mismatch:
        foot += ("<tr><td colspan='4' style='color:#5B667D;font-size:9px;padding-top:6px;'>"
                 "Invested on page 1 is %s. Invested is set manually for this account.</td></tr>"
                 % _money2(invested))

    grey = ""
    if lg["internal"]:
        grey = ("<h3 class='lbl' style='margin:18px 0 6px;color:#9098A8;'>Earlier history / internal "
                "transfers &mdash; for reference, not counted</h3>"
                "<table><tbody>%s</tbody></table>"
                % "".join(row(r, grey=True) for r in lg["internal"]))

    return ('<div class="sec" style="page-break-before:always;"><h3>Annexure &mdash; Pay-in / Payout</h3>'
            '<table><thead><tr><th>Date</th><th>Type</th>'
            '<th style="text-align:right">Pay-in</th><th style="text-align:right">Payout</th></tr></thead>'
            '<tbody>%s%s</tbody></table>%s</div>' % (body, foot, grey))


def _money2(n):
    """Two decimals for the annexure. A ledger reconciles to the paise or it does not reconcile,
    and page 1's whole-rupee formatting would hide a difference of a few paise per voucher."""
    if n is None:
        return "&mdash;"
    try:
        return "&#8377;" + format(float(n), ",.2f")
    except (TypeError, ValueError):
        return "&mdash;"


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

    # cc#662 / FORMAT_V1.1: Results This Quarter — compact table (Sales Gr. | Profit Gr. | FY27 Est.),
    # not analysis prose. Announced-but-not-extracted rows show a "pending" marker, never dropped.
    rtq = rep.get("results_this_quarter") or []
    if rtq:
        _rr = ""
        for r in rtq:
            if r.get("pending"):
                sg = pg = '<span class="muted">pending</span>'
            else:
                sg, pg = _pct(r.get("sales_growth")), _pct(r.get("profit_growth"))
            fy = _pct(r.get("fy27_est")) if r.get("fy27_est") is not None else "&#8212;"
            _rr += (f'<tr><td class="l" style="font-weight:700;">{_esc(r.get("symbol"))}</td>'
                    f'<td>{sg}</td><td>{pg}</td><td>{fy}</td></tr>')
        _ytr = rep.get("results_yet_to_report") or 0
        _foot = (f'<div class="note">{_ytr} of {len(rep.get("holdings") or [])} holdings yet to report.</div>'
                 if _ytr else "")
        ra_rows = (f'<table><tr><th>Symbol</th><th>Sales Gr.</th><th>Profit Gr.</th><th>FY27 Est.</th></tr>'
                   f'{_rr}</table>{_foot}')
    else:
        ra_rows = '<div class="muted">No holdings have reported results this quarter yet.</div>'

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
    # cc#654: realised / unrealised breakdown under the P&L tile (shown only when realised lots exist)
    _rp = snap.get("realised_pnl")
    pnl_split = (f'<div style="font-size:9.5px;color:#5B667D;margin-top:4px;">Realised {_money(_rp)} '
                 f'&middot; Unrealised {_money(snap.get("unrealised_pnl"))}</div>') if _rp not in (None, 0) else ""

    # cc#1213 scope item 1: the cash line under Current Value. Same 9.5px muted style as the
    # Realised/Unrealised line beside it, so the strip reads as one thing rather than two.
    #
    # Holdings is the Current Value AS PRINTED, not current minus cash. build_report sums the
    # holdings into total_current and adds cash on top of it - total_pnl = total_current + cash -
    # invested - so cash was never inside the number above. Subtracting it would print a Holdings
    # figure that disagrees with the P&L on the same strip, in a document that goes to a client.
    # _money already draws the distinction this line needs: 0 prints as a rupee zero, None prints
    # as an em-dash. "This account holds no cash" and "we have no meta row for it" stay different
    # statements without a conditional of my own second-guessing the module's own convention.
    cash_line = (f'<div style="font-size:9.5px;color:#5B667D;margin-top:4px;">'
                 f'Holdings {_money(snap.get("current"))} &middot; '
                 f'Cash {_money(snap.get("cash"))}</div>')

    # cc#656: dual money-return alpha (vs Nifty 50 + vs Nifty 500), each its own +/- colour, date once.
    def _alpha_line(lbl, v):
        col = "#5B667D" if v is None else ("#0B6E42" if v >= 0 else "#B52432")
        return (f'<div style="font-size:11px;font-weight:700;color:{col};margin-top:2px;">'
                f'<span style="font-size:7.5px;color:#9098A8;font-weight:600;">{lbl}</span> {_pct(v)}</div>')
    alpha_since_lbl = (' &middot; since ' + _esc(snap.get('alpha_label'))) if snap.get('alpha_label') else ''
    alpha_block = _alpha_line('vs Nifty 50', snap.get('alpha_n50')) + _alpha_line('vs Nifty 500', snap.get('alpha_n500'))

    # cc#1217: the ledger and its XIRR, read once for both the card line and the annexure.
    _lg = _ledger_for_pdf(port.get("id"))
    xirr_line = _xirr_line_html(_lg)
    annexure = _annexure_html(_lg, snap.get("invested"))

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
/* cc#662 / HEALTH_REPORT_CLIENT_FORMAT_V1.1: client docs use DejaVu SANS (not mono) at a 12.5px
   base — a readable client deliverable, not a terminal dump. */
@page {{ size: A4; margin: 14mm 14mm 16mm; }}
* {{ box-sizing: border-box; }}
body {{ font-family: 'DejaVu Sans', sans-serif; color:#07111F; font-size:12.5px; line-height:1.55; margin:0; }}
h1,h2,h3 {{ margin:0; }}
/* cc#655 print pagination: keep the holdings header repeating, don't split rows/small blocks, never
   orphan a section heading. Long tables still flow naturally (no forced page breaks -> no giant gaps). */
thead {{ display: table-header-group; }}
tr {{ page-break-inside: avoid; }}
h3 {{ page-break-after: avoid; }}
.strip, .take, .flag {{ page-break-inside: avoid; }}
.mast {{ background:#07111F; color:#fff; padding:16px 18px; }}
.mast .k {{ font-size:9px; letter-spacing:1px; color:rgba(255,255,255,.4); text-transform:uppercase; }}
.mast .nm {{ font-size:30px; font-weight:800; margin-top:6px; }}
.mast .sub {{ font-size:9.5px; color:rgba(255,255,255,.5); margin-top:4px; }}
.mast .rate {{ float:right; text-align:right; margin-top:-48px; }}
.mast .rate .big {{ font-size:52px; font-weight:800; line-height:1; }}
.strip {{ display:table; width:100%; border-collapse:collapse; margin-top:0; }}
.strip .c {{ display:table-cell; width:25%; padding:16px 18px; border-right:1px solid #E5E8EF; border-bottom:1px solid #E5E8EF; }}
.lbl {{ font-size:9.5px; font-weight:700; letter-spacing:.6px; text-transform:uppercase; color:#9098A8; }}
.big {{ font-size:21px; font-weight:700; margin-top:4px; }}
.sec {{ padding:18px 18px; border-bottom:1px solid #E5E8EF; }}
.sec h3 {{ font-size:11px; letter-spacing:.7px; text-transform:uppercase; color:#8B93A5; margin-bottom:10px; }}
table {{ width:100%; border-collapse:collapse; }}
th {{ font-size:9.5px; text-transform:uppercase; letter-spacing:.4px; color:#8B93A5; text-align:right; padding:7px 7px; border-bottom:1px solid #E5E8EF; }}
th:first-child, td.l {{ text-align:left; }}
td {{ font-size:11.5px; padding:8px 7px; border-bottom:1px solid #EEF0F4; text-align:right; white-space:nowrap; }}
.track {{ height:6px; background:#E5E8EF; border-radius:3px; overflow:hidden; }}
.fill {{ height:6px; }}
td.rl {{ color:#5B667D; width:110px; }} td.rv {{ font-weight:700; width:46px; }}
.chip {{ font-size:9.5px; font-weight:700; padding:3px 9px; border-radius:2px; }}
.chip.g {{ background:rgba(11,110,66,.12); color:#0B6E42; }}
.chip.r {{ background:rgba(181,36,50,.12); color:#B52432; }}
.chip.a {{ background:rgba(143,92,7,.12); color:#8F5C07; }}
.chip.b {{ background:rgba(24,71,223,.12); color:#1847DF; }}
.mv {{ display:inline-block; margin-right:14px; }} .mv-s {{ font-weight:700; }} .mv-v {{ margin-left:5px; }}
.flag {{ font-size:11.5px; color:#5B667D; margin:8px 0; line-height:1.65; }} .flag b {{ color:#07111F; }}
.muted {{ font-size:11.5px; color:#9098A8; }}
.cols {{ display:table; width:100%; }} .col {{ display:table-cell; width:50%; vertical-align:top; padding:18px 18px; }}
.col:first-child {{ border-right:1px solid #E5E8EF; }}
ul {{ margin:0; padding-left:18px; }} li {{ font-size:11.5px; color:#5B667D; margin:6px 0; line-height:1.65; }}
.note {{ font-size:11px; color:#5B667D; margin-top:8px; line-height:1.5; }}
.take {{ background:#07111F; color:rgba(255,255,255,.88); padding:20px 20px; font-size:13.5px; line-height:1.85; }}
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
  <div class="c"><div class="lbl">Current Value</div><div class="big">{_money(snap.get('current'))}</div>{cash_line}</div>
  <div class="c"><div class="lbl">P&amp;L</div><div class="big" style="color:{pnl_col};">{_money(snap.get('pnl_abs'))}</div>
    <div style="font-size:11px;color:{pnl_col};">{_pct(snap.get('pnl_pct'))}</div>{pnl_split}</div>
  <div class="c" style="border-right:none;"><div class="lbl">Alpha{alpha_since_lbl}</div>{alpha_block}{xirr_line}</div>
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
  <div class="col"><h3 class="lbl" style="margin-bottom:8px;">Results This Quarter</h3>{ra_rows}</div>
  <div class="col"><h3 class="lbl" style="margin-bottom:8px;">Red Flags</h3>{rf_rows}</div>
</div>
<div class="sec"><h3>Replacement Ideas</h3>{rep_rows}
  <div class="note">{_esc(rep.get('replacement_note'))}</div></div>
<div class="sec"><h3>Key Highlights</h3><ul>{hl}</ul></div>
<div class="take"><div class="lbl" style="color:rgba(255,255,255,.35);margin-bottom:8px;">Expert Take</div>{_esc(rep.get('expert_take'))}
  <div style="font-size:9px;color:rgba(255,255,255,.3);margin-top:14px;">Data as of report date &middot; Research only, not investment advice.</div></div>
{annexure}
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


@router.get("/api/health/report_pdf_self/{pid}")
def report_pdf_self(pid: int, request: Request):
    """cc#655: page-scoped PDF link for the logged-in /health report page. Same signed-token PDF as the
    admin/MCP path, but gated on the browser's login session (_is_authed) instead of the admin token, so
    the SAVE PDF button can mint it directly. Returns a RELATIVE URL (same-origin -> the anchor download
    attribute is honoured) + the filename."""
    if not _is_authed(request):
        return JSONResponse({"error": "login required"}, status_code=403)
    exp = int(time.time()) + _TOKEN_TTL
    sig = _sign(pid, exp)
    name = None
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT name FROM hr_portfolios WHERE id=%s", (pid,))
            row = cur.fetchone()
            name = row[0] if row else None
    except Exception:
        name = None
    return {"portfolio_id": pid, "url": f"/api/health/report_pdf/{pid}?exp={exp}&sig={sig}",
            "html_url": f"/api/health/report_html/{pid}?exp={exp}&sig={sig}&print=1",   # cc#662 fallback
            "filename": _pdf_filename(name), "expires_in": _TOKEN_TTL}


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
    # cc#662: crash-isolated render — a native segfault/OOM returns a clean 503 JSON (the frontend
    # then falls back to the print-HTML), never a connection reset that reads as "Site wasn't available".
    pdf_bytes, err = _render_pdf_isolated(html_str)
    if err:
        return JSONResponse({"error": err, "fallback": f"/api/health/report_html/{pid}"}, status_code=503)
    fname = _pdf_filename((rep.get("portfolio") or {}).get("name"))
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{fname}"'})


@router.get("/api/health/pdf_selftest")
def pdf_selftest(request: Request):
    """cc#662: one-call diagnosis of the server PDF env (admin-gated). Imports weasyprint and renders
    a 1-line HTML->PDF in the SAME isolated path the real endpoint uses, reporting version, timing,
    RSS before/after, and the exact failure (import error / signal / stderr) if any. Turns the black
    box into a diagnosis without a connection reset."""
    if not _admin_ok(request):
        return JSONResponse({"error": "admin token required"}, status_code=403)
    out = {"ok": False, "rss_mb_before": _rss_mb(), "weasyprint_version": None, "pango_ok": None}
    try:
        import weasyprint
        out["weasyprint_version"] = getattr(weasyprint, "__version__", "unknown")
    except Exception as e:
        out["stage"] = "import"; out["error"] = str(e)[:300]
        return JSONResponse(out, status_code=200)
    t0 = time.time()
    pdf, err = _render_pdf_isolated("<!DOCTYPE html><html><body><p>Health Report PDF selftest</p></body></html>", timeout=30)
    out["ms"] = int((time.time() - t0) * 1000)
    out["rss_mb_after"] = _rss_mb()
    out["pango_ok"] = bool(pdf)   # a successful render implies pango/cairo/gdk-pixbuf all resolved
    if err:
        out["stage"] = "render"; out["error"] = err
        return JSONResponse(out, status_code=200)
    out["ok"] = True; out["pdf_bytes"] = len(pdf)
    return JSONResponse(out, status_code=200)


@router.get("/api/health/report_html/{pid}")
def report_html(pid: int, exp: str = "", sig: str = "", print: str = ""):
    """cc#662: the white-label report as text/html (same signed token as the PDF). The SAVE PDF
    button falls back here when the PDF engine 503s, so the user can browser-print -> Save as PDF
    instead of hitting a silent Chrome download error. ?print=1 auto-opens the print dialog."""
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
    if str(print).strip() not in ("", "0", "false"):
        html_str = html_str.replace(
            "</body>",
            "<script>window.addEventListener('load',function(){setTimeout(function(){window.print();},350);});</script></body>")
    return Response(content=html_str, media_type="text/html; charset=utf-8")
