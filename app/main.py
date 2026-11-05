import logging
import os
import uuid

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.agents import llm
from app.config import ROOT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# --- Config ---

# Day-1 placeholder Lead: the analysis tools are not connected yet (DESIGN.md §13).
SYSTEM_PROMPT = (
    "You are the Lead of Fix the Mix, a feedback agent for electronic music producers. Producers upload "
    "their own track and one to three reference tracks, and you plan an evidence-based comparison of "
    "kick, low end, tonal balance, loudness and stereo. The audio analysis tools are not connected yet, "
    "so you cannot measure anything: say so plainly when asked about a specific track, and never invent "
    "measurements."
)

# --- Session Store ---

# session_id -> list of messages. In-memory for now; runs move to durable storage with the run model.
sessions: dict[str, list] = {}

# --- FastAPI App ---

app = FastAPI(title="Fix the Mix")


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


# Not /healthz: Cloud Run's front end reserves paths ending in "z" on run.app URLs.
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    # Get or create the session
    session_id = request.session_id or str(uuid.uuid4())
    if session_id not in sessions:
        sessions[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Append user's message to the context
    sessions[session_id] += [{"role": "user", "content": request.message}]

    try:
        result = await llm.complete("lead", sessions[session_id])
        sessions[session_id] += [result.message]
        response, tool_calls = result.text, []
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        sessions[session_id].pop()  # drop the unanswered user turn so the history stays valid
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", []

    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


@app.post("/clear")
def clear(session_id: str | None = None):
    sessions.pop(session_id, None)
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
