"""FastAPI server for the BrowserTransactionBench message fixture.

The server is intentionally trivial. It exposes the draft/message app the
browser drives. The AUTHORITATIVE state lives in SQLite (see db.py); FastAPI
just translates HTTP to durable db.py calls. The disconnect-after-send fault
is injected externally (at the Playwright/agent-driver layer and the harness),
NOT as app logic — so it is a genuine injected failure.

Endpoints:
  GET  /                      -> index.html (the app UI)
  GET  /api/drafts            -> list drafts
  POST /api/drafts            -> create a draft      {subject, body}
  POST /api/drafts/{id}/save  -> save a draft
  POST /api/messages/send     -> send a draft        {draft_id, send_uid?}
  GET  /health                -> health check
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from btb.app import db

logger = logging.getLogger("btb.app")

DB_PATH = Path(os.environ.get("BTB_DB", str(db.DEFAULT_DB)))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Create schema + a seed user ONCE at startup (main event-loop thread).
    db.init_db(DB_PATH, seed_user="alice")
    yield


app = FastAPI(
    title="BrowserTransactionBench message fixture",
    version="0.1.0",
    lifespan=lifespan,
)


class DraftIn(BaseModel):
    subject: str
    body: str


class SaveIn(BaseModel):
    draft_id: int


class SendIn(BaseModel):
    draft_id: int
    send_uid: Optional[str] = None


@app.get("/health")
def health() -> dict:
    return {"ok": True, "db": str(DB_PATH)}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "templates" / "index.html")


@app.get("/api/drafts")
def list_drafts() -> list[dict]:
    return db.get_drafts(DB_PATH)


@app.post("/api/drafts")
def create_draft(payload: DraftIn) -> dict:
    return db.create_draft(
        DB_PATH, user_id=1, subject=payload.subject, body=payload.body
    )


@app.post("/api/drafts/{draft_id}/save")
def save_draft(draft_id: int) -> dict:
    try:
        return db.save_draft(DB_PATH, draft_id=draft_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="draft not found")


@app.get("/api/messages")
def list_messages() -> list[dict]:
    return db.messages(DB_PATH)


@app.post("/api/messages/send")
def send_message(payload: SendIn) -> dict:
    """Send is durable BEFORE this returns: the DB commit happens in db.py and
    the row is committed before FastAPI serializes the response. An external
    injection may drop the response body — the DB already has the truth."""
    try:
        return db.send_message(DB_PATH, draft_id=payload.draft_id, send_uid=payload.send_uid)
    except KeyError:
        raise HTTPException(status_code=404, detail="draft not found")


def main() -> None:
    import uvicorn

    host = os.environ.get("BTB_HOST", "127.0.0.1")
    port = int(os.environ.get("BTB_PORT", "7788"))
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
