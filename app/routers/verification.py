"""The user's side of identity verification (services/verification.py).

GET /verification says where each document stands; POST /verification sends
one. A new submission of a kind replaces a pending one of that kind, so the
admin queue only ever holds the latest. An approved identity is final: it is
not replaced from here.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from psycopg.errors import UndefinedTable
from pydantic import BaseModel, Field

from .. import db
from ..security import AuthUser, current_user
from ..services import events, profiles, verification

router = APIRouter(tags=["verification"])


class Upload(BaseModel):
    side: str = Field(min_length=1, max_length=40)
    data: str = Field(min_length=100)                 # a data URL: data:image/jpeg;base64,...


class Submission(BaseModel):
    kind: Literal["identity", "address"]
    doc_type: Literal["id", "passport", "licence", "utility", "bank"]
    files: list[Upload] = Field(min_length=1, max_length=2)


@router.get("/verification")
async def get_verification(user: AuthUser = Depends(current_user)):
    p = await profiles.for_user(user)
    out = await verification.state(user.id)
    out["required"] = not p.get("is_vip")
    return out


@router.post("/verification", status_code=201)
async def submit(body: Submission, user: AuthUser = Depends(current_user)):
    await profiles.for_user(user)
    sides = verification.DOCS[body.kind].get(body.doc_type)
    if not sides:
        raise HTTPException(status_code=400, detail="That document does not prove this. Choose another.")
    if len(body.files) != len(sides):
        raise HTTPException(status_code=400, detail=f"Send {' and '.join(sides).lower()} of the document.")
    files = [(f.side, *verification.decode(f.data)) for f in body.files]

    try:
        async with db.tx() as conn:
            cur = await conn.execute(
                "select 1 from app.verifications where user_id = %s and kind = %s and status = 'approved' limit 1",
                (user.id, body.kind))
            if body.kind == "identity" and await cur.fetchone():
                raise HTTPException(status_code=409, detail="Your identity is already verified.")
            await conn.execute(
                "delete from app.verifications where user_id = %s and kind = %s and status = 'pending'",
                (user.id, body.kind))
            cur = await conn.execute(
                "insert into app.verifications (user_id, kind, doc_type) values (%s, %s, %s) returning id",
                (user.id, body.kind, body.doc_type))
            vid = (await cur.fetchone())["id"]
            for side, mime, raw in files:
                await conn.execute(
                    "insert into app.verification_files (verification_id, side, mime, size, data) values (%s, %s, %s, %s, %s)",
                    (vid, side, mime, len(raw), raw))
    except UndefinedTable:
        raise HTTPException(status_code=503, detail="Verification is not open yet. Try again later.")
    await events.record("verification.submit", True, provider=body.kind, ref=str(vid), user_id=user.id,
                        detail=f"{verification.DOC_NAME[body.doc_type]}, {sum(len(f[2]) for f in files) // 1024} KB")
    return await verification.state(user.id)
