"""
cc#319: one-time NIFTY 50 constituent + official-industry sync from the NSE archives CSV.

NSE publishes the authoritative membership AND official Industry tag in one file
(archives.nseindia.com/content/indices/ind_nifty50list.csv, cols: Company Name, Industry,
Symbol, Series, ISIN Code). Fyers does NOT expose index-constituent membership, and the DB has
no NIFTY-50 flag — so this CSV is the only real source. Populated ONCE (no scheduler): NIFTY 50
rebalances semi-annually (Mar/Sep); re-run this manually after a rebalance.

Reusable: fetch_nifty50() parses the CSV; populate_nifty50(conn) upserts nifty50_constituents
(symbol PK) + removes dropped symbols + logs to ops_log. Idempotent.
"""

import csv as _csv
import json
import logging

import requests

log = logging.getLogger("nifty50_sync")

NSE_URL = "https://archives.nseindia.com/content/indices/ind_nifty50list.csv"
# niftyindices.com serves the same file when the archives path is unavailable.
NSE_FALLBACK_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
    "Accept": "text/csv,application/csv,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_nifty50(timeout: int = 40) -> list:
    """Return [{symbol, company_name, industry}] from the NSE NIFTY 50 constituent CSV.
    Tries the archives URL then the niftyindices.com fallback. Raises if neither yields a
    plausible (>=40-row) list, so a bad/blocked response never wipes the table."""
    last_err = None
    for url in (NSE_URL, NSE_FALLBACK_URL):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            r.raise_for_status()
            lines = [ln for ln in r.text.splitlines() if ln.strip()]
            if not lines:
                last_err = f"{url}: empty body"
                continue
            header = [h.strip().lower() for h in lines[0].split(",")]

            def _col(name):
                for i, h in enumerate(header):
                    if name in h:
                        return i
                return None

            i_co, i_ind, i_sym = _col("company"), _col("industry"), _col("symbol")
            if None in (i_co, i_ind, i_sym):
                last_err = f"{url}: unexpected header {header}"
                continue
            rows = []
            for parts in _csv.reader(lines[1:]):
                if len(parts) <= max(i_co, i_ind, i_sym):
                    continue
                sym = parts[i_sym].strip().upper()
                if not sym:
                    continue
                rows.append({"symbol": sym,
                             "company_name": parts[i_co].strip(),
                             "industry": parts[i_ind].strip()})
            if len(rows) >= 40:
                log.info(f"NIFTY 50 CSV: {len(rows)} constituents from {url}")
                return rows
            last_err = f"{url}: only {len(rows)} rows parsed"
        except Exception as e:
            last_err = f"{url}: {e}"
    raise RuntimeError(f"NIFTY 50 constituent fetch failed — {last_err}")


def populate_nifty50(conn) -> dict:
    """Fetch + upsert nifty50_constituents (symbol PK), remove symbols no longer in the index,
    and log a summary to ops_log. Idempotent. Returns the report dict."""
    rows = fetch_nifty50()
    with conn.cursor() as cur:
        for r in rows:
            # cc#337: the curated `theme` column (12 Scorr themes, the dashboard grouping key) is
            # INTENTIONALLY absent from both the INSERT column list and the DO UPDATE SET — so an
            # NSE re-fetch NEVER nulls or overwrites it. Existing rows keep their theme; a genuinely
            # new index constituent lands with theme=NULL and is curated manually (rare).
            cur.execute("""INSERT INTO nifty50_constituents (symbol, company_name, industry, updated_at)
                           VALUES (%s,%s,%s,NOW())
                           ON CONFLICT (symbol) DO UPDATE
                           SET company_name=EXCLUDED.company_name,
                               industry=EXCLUDED.industry, updated_at=NOW()""",
                        (r["symbol"], r["company_name"], r["industry"]))
        syms = [r["symbol"] for r in rows]
        cur.execute("DELETE FROM nifty50_constituents WHERE symbol <> ALL(%s)", (syms,))
        removed = cur.rowcount
        cur.execute("SELECT COUNT(*) FROM nifty50_constituents")
        total = int(cur.fetchone()[0])
        report = {"upserted": len(rows), "removed": removed, "total": total,
                  "industries": sorted(set(r["industry"] for r in rows))}
        cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                       VALUES (CURRENT_DATE, NOW(), 'nifty50_sync', %s, %s::jsonb)""",
                    (f"{len(rows)} constituents upserted, {removed} removed, {total} total",
                     json.dumps(report)))
        conn.commit()
    log.info(f"nifty50_sync: {report}")
    return report
