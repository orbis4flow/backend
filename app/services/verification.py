"""Identity verification (sql/015_verification.sql).

A submission is one document (an ID's front and back, a passport page, a
utility bill), sent as data URLs from the Verification page and kept as bytes.
The admin site approves or rejects it. Withdrawals need an approved proof of
identity; VIPs are demonstration accounts and are not asked.
"""
import base64
import binascii
import logging
import re

from fastapi import HTTPException
from psycopg.errors import UndefinedTable

from .. import db

log = logging.getLogger("obisflow.verification")

DOCS = {
    "identity": {"id": ("Front", "Back"), "passport": ("Photo page",), "licence": ("Front", "Back")},
    "address": {"utility": ("Document",), "bank": ("Document",)},
}
DOC_NAME = {"id": "National ID", "passport": "Passport", "licence": "Driving licence",
            "utility": "Utility bill", "bank": "Bank statement"}
MIMES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
MAX_BYTES = 6 * 1024 * 1024
_DATA_URL = re.compile(r"^data:([\w/+.-]+);base64,(.*)$", re.S)


def decode(data_url: str) -> tuple[str, bytes]:
    m = _DATA_URL.match(data_url or "")
    if not m:
        raise HTTPException(status_code=400, detail="That file could not be read. Choose it again.")
    mime = m.group(1).lower()
    if mime not in MIMES:
        raise HTTPException(status_code=400, detail="Use a JPG, PNG or PDF.")
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="That file could not be read. Choose it again.")
    if len(raw) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="That file is too large. The limit is 6 MB.")
    if len(raw) < 1024:
        raise HTTPException(status_code=400, detail="That file looks empty. Choose it again.")
    return mime, raw


async def state(user_id: str) -> dict:
    """The latest submission of each kind, and whether withdrawals are open."""
    try:
        rows = await db.many(
            """select distinct on (kind) id, kind, doc_type, status, reason, created_at, decided_at
                 from app.verifications where user_id = %s order by kind, created_at desc""", (user_id,))
    except UndefinedTable:
        return {"ready": False, "identity": None, "address": None, "verified": True}
    out: dict = {"ready": True, "identity": None, "address": None}
    for r in rows:
        out[r["kind"]] = {"id": str(r["id"]), "docType": r["doc_type"], "docName": DOC_NAME.get(r["doc_type"], r["doc_type"]),
                          "status": r["status"], "reason": r["reason"],
                          "createdAt": r["created_at"].isoformat(),
                          "decidedAt": r["decided_at"].isoformat() if r["decided_at"] else None}
    out["verified"] = bool(out["identity"] and out["identity"]["status"] == "approved")
    return out


async def identity_approved(user_id: str) -> bool:
    """True once a proof of identity is approved. Before sql/015 is run there is
    nothing to check against, so withdrawals are not held for it."""
    try:
        row = await db.one(
            "select 1 from app.verifications where user_id = %s and kind = 'identity' and status = 'approved' limit 1",
            (user_id,))
    except UndefinedTable:
        log.warning("sql/015_verification.sql not run: withdrawals are not checked for verification")
        return True
    return row is not None
