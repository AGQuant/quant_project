"""image_assets_endpoints.py -- cc#2140 IMAGE HOSTING (founder direct, 16-Sep-2026).

Claude / Fable can draw a chart image (matplotlib, SVG) in their own sandbox but had nowhere to put
it: polished_news has no image column and nothing on the app served bytes. This file is the hosting
half -- the table, the reader, and the serving endpoint. It draws nothing itself.

  GET /api/images/status        -> counts, latest id, bytes on disk -- a check without a download
  GET /api/images/{id}          -> the bytes, with the stored Content-Type. An image is IMMUTABLE
                                   (never updated in place; a new chart is a new row), so the
                                   response is Cache-Control public / 1 year / immutable and the
                                   ETag is the id; If-None-Match answers 304 without a DB read of
                                   the bytes.
  GET /api/images/{id}/meta     -> content_type, size, width/height, purpose, who/when, and the
                                   polished_news ids the image is attached to

STORAGE is Postgres bytea, the card's design_recommendation: a handful of tens-of-KB chart images a
day, no new external dependency or credential, and the images ride along with the DB backups.
Revisit S3/R2 only if this grows past chart snapshots (flagged, not built -- YAGNI).

THE WRITE PATH IS SQL, NOT AN ENDPOINT (the card's how_claude_will_use_this): Claude inserts through
run_sql. ONE statement stores the image and attaches it to an article:

    WITH img AS (
      INSERT INTO image_assets (content_type, image_bytes, purpose, width_px, height_px, created_by)
      VALUES ('image/png', decode('<png hex>', 'hex'), 'stock_view_chart', 1200, 640, 'claude_chat')
      RETURNING id)
    INSERT INTO polished_news_images (news_id, image_id, attached_by)
    SELECT <polished_news.id>, id, 'claude_chat' FROM img
    ON CONFLICT (news_id) DO UPDATE
      SET image_id = EXCLUDED.image_id, attached_at = now(), attached_by = EXCLUDED.attached_by
    RETURNING news_id, image_id;

WHY A LINK TABLE AND NOT THE chart_image_id COLUMN THE CARD NAMES: a new column on polished_news is
an ALTER TABLE, which MAINTENANCE_LOCK_RULE (cc#351) holds for a weekend Railway-console run and the
run_sql path hard-blocks. cc#1519 precedent (the screeners `source` column): build the shape that
works today without the ALTER. polished_news_images has news_id as its PRIMARY KEY, so it is exactly
one-image-per-article -- the same semantics as a nullable FK column, with FK integrity in both
directions (article deleted -> attachment goes; image row cannot vanish under an attachment). When
the column lands at a console run, the reader becomes COALESCE(p.chart_image_id, l.image_id);
`chart_image_ids(cur, ids)` below is the ONE reader every news endpoint should use, so that swap is
one function, not a hunt. Decision logged on the card (Fable Room) before it was built.

THE CHECKs ARE THE GUARD RAILS: content_type must be image/* (this is an image host, never a general
file server -- the endpoint refuses anything else with 415 even if a row slipped past the CHECK);
bytes 1 .. 4 MB (a chart is tens of KB -- anything bigger is a mistake, not a chart); width/height
positive when given. nosniff on every response; an SVG is served with a script-less CSP so a direct
navigation to it can never run code.

Ownership: CC (backend). Wiring: one import + one include_router in main.py (rule 5).
"""
import logging
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
import psycopg

log = logging.getLogger("scorr.images")
router = APIRouter(tags=["images"])

MAX_BYTES = 4 * 1024 * 1024   # mirrors the CHECK below; stated once in /status so a writer can see it

# Regex, not LIKE, on purpose: no '%' in this text, so the same DDL runs unchanged through
# cur.execute() here and through the run_sql MCP path.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS image_assets (
    id            BIGSERIAL   PRIMARY KEY,
    content_type  TEXT        NOT NULL,
    image_bytes   BYTEA       NOT NULL,
    purpose       TEXT,
    width_px      INTEGER,
    height_px     INTEGER,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by    TEXT,
    CONSTRAINT image_assets_content_type_is_image CHECK (content_type ~ '^image/'),
    CONSTRAINT image_assets_size_1b_to_4mb CHECK (octet_length(image_bytes) BETWEEN 1 AND 4194304),
    CONSTRAINT image_assets_dims_positive CHECK ((width_px IS NULL OR width_px > 0) AND (height_px IS NULL OR height_px > 0))
);
CREATE TABLE IF NOT EXISTS polished_news_images (
    news_id      BIGINT      PRIMARY KEY REFERENCES polished_news(id) ON DELETE CASCADE,
    image_id     BIGINT      NOT NULL REFERENCES image_assets(id),
    attached_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    attached_by  TEXT
);
CREATE INDEX IF NOT EXISTS polished_news_images_image_id_idx ON polished_news_images (image_id);
"""

_IMMUTABLE = "public, max-age=31536000, immutable"
_NO_STORE = "no-store, no-cache, must-revalidate, max-age=0"


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


_SCHEMA_DONE = False


def ensure_schema(conn=None):
    """Idempotent DDL (CREATE ... IF NOT EXISTS only -- never an ALTER). Runs once per process."""
    global _SCHEMA_DONE
    if _SCHEMA_DONE:
        return
    own = conn is None
    if own:
        conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
        _SCHEMA_DONE = True
    finally:
        if own:
            conn.close()


def chart_image_ids(cur, news_ids):
    """{polished_news.id: image_id} for the given article ids -- THE reader for the attachment.

    A news endpoint calls this once per page of rows and sets row['chart_image_id'] from it (None
    when absent). Raises on a DB error like any query: wrap it and rollback, as the news overlays
    already do, so a hosting problem never blanks the news page.
    """
    ids = sorted({int(i) for i in news_ids if i is not None})
    if not ids:
        return {}
    cur.execute("SELECT news_id, image_id FROM polished_news_images WHERE news_id = ANY(%s)", (ids,))
    return {int(n): int(i) for n, i in cur.fetchall()}


def _etag(image_id: int) -> str:
    return '"img-%d"' % int(image_id)


def _headers(image_id: int, ctype: str) -> dict:
    h = {"Cache-Control": _IMMUTABLE, "ETag": _etag(image_id),
         "X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"}
    if ctype == "image/svg+xml":
        h["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    return h


# Registered BEFORE /api/images/{image_id}: routes match in order, and "status" would otherwise be
# handed to the int path parameter and fail with a 422 instead of reaching this.
@router.get("/api/images/status")
def images_status():
    """Counts + latest row, no bytes. The check a writer runs after an insert."""
    try:
        ensure_schema()
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*), MAX(id), MAX(created_at),
                                  COALESCE(SUM(octet_length(image_bytes)), 0)
                           FROM image_assets""")
            n, latest_id, latest_at, total = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM polished_news_images")
            attached = cur.fetchone()[0]
            cur.execute("SELECT COALESCE(purpose, ''), COUNT(*) FROM image_assets GROUP BY 1 ORDER BY 2 DESC, 1")
            by_purpose = {p: int(c) for p, c in cur.fetchall()}
        return {"status": "ok", "images": int(n or 0), "latest_id": latest_id,
                "latest_created_at": str(latest_at) if latest_at else None,
                "total_bytes": int(total or 0), "attached_articles": int(attached or 0),
                "by_purpose": by_purpose, "max_bytes": MAX_BYTES}
    except Exception as e:
        raise HTTPException(500, f"images_status failed: {e}")


@router.get("/api/images/{image_id}/meta")
def image_meta(image_id: int):
    """Everything about one image except the bytes, plus where it is attached."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT id, content_type, octet_length(image_bytes), width_px, height_px,
                                  purpose, created_at, created_by
                           FROM image_assets WHERE id = %s""", (image_id,))
            row = cur.fetchone()
            att = []
            if row:
                cur.execute("""SELECT news_id, attached_at, attached_by FROM polished_news_images
                               WHERE image_id = %s ORDER BY news_id""", (image_id,))
                att = [{"news_id": n, "attached_at": str(a), "attached_by": b} for n, a, b in cur.fetchall()]
    except Exception as e:
        raise HTTPException(500, f"image_meta failed: {e}")
    if not row:
        raise HTTPException(404, f"image {image_id} not found", headers={"Cache-Control": _NO_STORE})
    return {"status": "ok", "id": row[0], "content_type": row[1], "bytes": row[2],
            "width_px": row[3], "height_px": row[4], "purpose": row[5],
            "created_at": str(row[6]) if row[6] else None, "created_by": row[7],
            "url": "/api/images/%d" % row[0], "attached_to": att}


@router.get("/api/images/{image_id}")
def image_get(image_id: int, request: Request):
    """The bytes. Immutable by contract, so cached for a year and revalidated by id."""
    if image_id <= 0:
        raise HTTPException(404, "no such image", headers={"Cache-Control": _NO_STORE})
    etag = _etag(image_id)
    inm = request.headers.get("if-none-match", "")
    if etag in [t.strip() for t in inm.split(",") if t.strip()]:
        # The id is the ETag and an image never changes, so a matching validator is a 304 without
        # touching the bytes. (An id that never existed cannot be in a client's cache.)
        return Response(status_code=304, headers=_headers(image_id, ""))
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT content_type, image_bytes FROM image_assets WHERE id = %s", (image_id,))
            row = cur.fetchone()
    except Exception as e:
        raise HTTPException(500, f"image_get failed: {e}")
    if not row:
        raise HTTPException(404, f"image {image_id} not found", headers={"Cache-Control": _NO_STORE})
    ctype = (row[0] or "").strip().lower()
    if not ctype.startswith("image/"):
        raise HTTPException(415, "stored row is not an image", headers={"Cache-Control": _NO_STORE})
    return Response(content=bytes(row[1]), media_type=ctype, headers=_headers(image_id, ctype))
