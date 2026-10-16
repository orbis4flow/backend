"""Live chat: the support assistant answers here (services/assistant.py).

The page keeps the conversation and sends the recent turns with each
message, so nothing is stored. Each user gets a fair number of messages
per window, which keeps the free Groq quota for everyone.
"""
import time
from collections import defaultdict, deque
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..security import AuthUser, current_user
from ..services import assistant

router = APIRouter(prefix="/support", tags=["support"])

PER_WINDOW, WINDOW_S = 20, 600
_sent: dict[str, deque] = defaultdict(deque)


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


class Chat(BaseModel):
    messages: list[Turn] = Field(min_length=1, max_length=40)


def _allow(user_id: str) -> bool:
    now, q = time.monotonic(), _sent[user_id]
    while q and now - q[0] > WINDOW_S:
        q.popleft()
    if len(q) >= PER_WINDOW:
        return False
    q.append(now)
    return True


@router.post("/chat")
async def chat(body: Chat, user: AuthUser = Depends(current_user)):
    if body.messages[-1].role != "user":
        raise HTTPException(status_code=400, detail="The last message must be yours.")
    if not _allow(user.id):
        raise HTTPException(status_code=429, detail="That is a lot of messages. Give it a few minutes.")
    try:
        text = await assistant.reply([m.model_dump() for m in body.messages[-12:]])
    except assistant.AssistantError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"reply": text}
