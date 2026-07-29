"""
client_index_security.py — cc#758: encryption + hard access-denylist for client_index credentials.

client_index stores broker login passwords + TOTP seed keys for ~175 client accounts inside the DB
the public FastAPI app queries. This module is the ONE place that:
  1. Encrypts `password` / `totp_key` at rest (Fernet / AES-128-CBC+HMAC), key from a Railway env var
     (CLIENT_INDEX_ENC_KEY) that is NEVER committed to GitHub.
  2. Provides scrub_row() — the HARD column denylist every serializer of client_index rows MUST call,
     so the two secret columns can never leave the server regardless of per-endpoint SELECT lists.
  3. Provides an admin-only, single-field, audit-logged decrypt path.

Design:
  - Encrypted values carry an `enc:v1:` prefix so migration is idempotent and plaintext/ciphertext are
    trivially distinguishable.
  - `cryptography` is imported LAZILY inside the crypto functions only — scrub_row (the safety-critical
    denylist) has zero third-party dependency and can never fail to strip the columns.
"""

import os
import json
from datetime import datetime, timezone

# THE denylist — these columns must never appear in any client_index API response.
SECRET_COLUMNS = ("password", "totp_key")

_ENC_PREFIX = "enc:v1:"
_ENV_KEY = "CLIENT_INDEX_ENC_KEY"


def _utc_now():
    return datetime.now(timezone.utc)


# ── the hard denylist (crypto-free — always available) ─────────────────────────────────────────────
def scrub_row(row):
    """Return a COPY of a client_index row dict with the secret columns removed. Call this on EVERY
    client_index row before it leaves the server (cc#758 hard rule). No-op on non-dicts."""
    if not isinstance(row, dict):
        return row
    return {k: v for k, v in row.items() if k not in SECRET_COLUMNS}


def scrub_rows(rows):
    return [scrub_row(r) for r in (rows or [])]


def is_encrypted(v):
    return isinstance(v, str) and v.startswith(_ENC_PREFIX)


def key_present():
    return bool(os.getenv(_ENV_KEY, "").strip())


# ── crypto (lazy cryptography import) ──────────────────────────────────────────────────────────────
def _fernet():
    key = os.getenv(_ENV_KEY, "").strip()
    if not key:
        raise RuntimeError(
            f"{_ENV_KEY} env var is not set — client_index credential encryption is unavailable. "
            f"Generate one with  python -c \"from cryptography.fernet import Fernet;"
            f"print(Fernet.generate_key().decode())\"  and set it in Railway (never commit it).")
    from cryptography.fernet import Fernet
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext):
    """Encrypt a plaintext string -> 'enc:v1:<token>'. Empty/None passes through unchanged (nothing to
    hide). Already-encrypted values pass through (idempotent)."""
    if plaintext is None or plaintext == "":
        return plaintext
    if is_encrypted(plaintext):
        return plaintext
    token = _fernet().encrypt(str(plaintext).encode("utf-8")).decode("ascii")
    return _ENC_PREFIX + token


def decrypt(value):
    """Decrypt an 'enc:v1:<token>' value -> plaintext. A value with no prefix is returned as-is (legacy
    plaintext that hasn't been migrated yet)."""
    if value is None or value == "":
        return value
    if not is_encrypted(value):
        return value
    token = value[len(_ENC_PREFIX):]
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


# ── in-place migration (idempotent) ────────────────────────────────────────────────────────────────
def migrate_encrypt(conn):
    """Encrypt every still-plaintext password/totp_key in client_index IN PLACE. Idempotent — rows
    already carrying the enc: prefix are skipped, so this is safe to run on every deploy. Returns a
    per-column count of rows encrypted this pass + how much plaintext (if any) remains."""
    if not key_present():
        return {"skipped": True, "reason": f"{_ENV_KEY} not set — nothing encrypted"}
    out = {"encrypted": {}, "plaintext_remaining": {}}
    with conn.cursor() as cur:
        for col in SECRET_COLUMNS:
            cur.execute(
                f"SELECT account_id, {col} FROM client_index "
                f"WHERE {col} IS NOT NULL AND {col} <> '' AND {col} NOT LIKE %s",
                (_ENC_PREFIX + "%",))
            todo = cur.fetchall()
            n = 0
            for account_id, plain in todo:
                try:
                    enc = encrypt(plain)
                    cur.execute(f"UPDATE client_index SET {col}=%s WHERE account_id=%s", (enc, account_id))
                    n += 1
                except Exception:
                    conn.rollback()
                    raise
            out["encrypted"][col] = n
            cur.execute(
                f"SELECT COUNT(*) FROM client_index WHERE {col} IS NOT NULL AND {col} <> '' AND {col} NOT LIKE %s",
                (_ENC_PREFIX + "%",))
            out["plaintext_remaining"][col] = int(cur.fetchone()[0])
    conn.commit()
    return out


def security_status(conn):
    """Non-secret counts for the admin status endpoint — never returns any credential value."""
    out = {}
    with conn.cursor() as cur:
        for col in SECRET_COLUMNS:
            cur.execute(
                f"SELECT COUNT(*) FILTER (WHERE {col} LIKE %s) AS enc, "
                f"COUNT(*) FILTER (WHERE {col} IS NOT NULL AND {col} <> '' AND {col} NOT LIKE %s) AS plain "
                f"FROM client_index", (_ENC_PREFIX + "%", _ENC_PREFIX + "%"))
            enc, plain = cur.fetchone()
            out[col] = {"encrypted": int(enc), "plaintext": int(plain)}
    out["key_present"] = key_present()
    return out


# ── admin single-field decrypt (audit-logged) ──────────────────────────────────────────────────────
def decrypt_field(conn, account_id, field, actor="admin"):
    """Admin-only, SINGLE (account_id, field) decrypt. Writes an audit row to session_log BEFORE
    returning the value. `field` must be one of SECRET_COLUMNS. Raises ValueError otherwise."""
    if field not in SECRET_COLUMNS:
        raise ValueError(f"field must be one of {SECRET_COLUMNS}, got {field!r}")
    with conn.cursor() as cur:
        cur.execute(f"SELECT {field} FROM client_index WHERE account_id=%s", (account_id,))
        r = cur.fetchone()
    if not r:
        raise KeyError(f"account_id {account_id!r} not found")
    value = decrypt(r[0])
    _audit(conn, {"account_id": account_id, "field": field, "actor": actor,
                  "ts": _utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")})
    return value


def _audit(conn, details):
    """Best-effort security audit row (session_log). Never raises — a failed audit must not corrupt the
    surrounding txn — but a failure is logged to ops_log as a fallback trail."""
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO session_log (category, title, details) VALUES (%s,%s,%s::jsonb)",
                        ("security_audit", "CLIENT_INDEX_DECRYPT", json.dumps(details)))
        conn.commit()
        return
    except Exception:
        try:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("INSERT INTO ops_log (category, title, details) VALUES (%s,%s,%s::jsonb)",
                            ("security_audit", "CLIENT_INDEX_DECRYPT", json.dumps(details)))
            conn.commit()
        except Exception:
            try: conn.rollback()
            except Exception: pass
