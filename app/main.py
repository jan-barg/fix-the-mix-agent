import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import runs
from app.agents import lead, llm
from app.api import router as api_router
from app.config import ROOT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# --- Config ---

# A session_id that names a run goes to the Lead (app/agents/lead.py). Any other session is a plain
# conversation without tools, as in the starter.
SYSTEM_PROMPT = (
    "You are Fix the Mix, a feedback agent for electronic music producers. In the web app, producers upload "
    "their own track and one to three reference tracks, and a team of agents measures and compares kick, low "
    "end, tonal balance, loudness and stereo. This conversation is not attached to any uploaded tracks, so you "
    "cannot measure anything here: answer general mixing questions, say plainly that you have not heard their "
    "track when asked about it, and never invent measurements."
)

# --- Session Store ---

# session_id -> list of messages, for sessions that are not runs.
sessions: dict[str, list] = {}


def _run_id(session_id: str | None) -> str | None:
    if not session_id:
        return None
    try:
        runs.get(session_id)
    except runs.RunNotFound:
        return None
    return session_id

# --- FastAPI App ---


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Preload LiteLLM once the server is listening, so the first model call does not pay for the import.
    threading.Thread(target=llm.load, daemon=True).start()
    yield


app = FastAPI(title="Fix the Mix", lifespan=lifespan)
app.include_router(api_router)


class RevalidatingStatic(StaticFiles):
    """Static files that the browser re-checks on every load, so an edit or deploy shows up at once."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatingStatic(directory=ROOT / "static"), name="static")


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-cache"})


# Not /healthz: Cloud Run's front end reserves paths ending in "z" on run.app URLs.
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    # A run: the Lead answers, and tool_calls lists the trace events of this request (DESIGN.md §3.1).
    if (run_id := _run_id(request.session_id)) is not None:
        response, events = await lead.chat(run_id, request.message)
        return ChatResponse(response=response, session_id=run_id, tool_calls=events)

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
async def clear(session_id: str | None = None):
    if (run_id := _run_id(session_id)) is not None:
        try:
            await lead.clear(run_id)
        except RuntimeError:
            raise HTTPException(409, "The analysis is running. Wait for it to finish first.") from None
        return {"status": "ok"}
    sessions.pop(session_id, None)
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
