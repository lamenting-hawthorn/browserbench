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
  GET  /health                -> health check and fixture identity
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from btb.app import db


class DraftIn(BaseModel):
    subject: str
    body: str


class SendIn(BaseModel):
    draft_id: int
    send_uid: str | None = None


def _canonical_database_path(database_path: Path | str) -> Path:
    return Path(database_path).expanduser().resolve()


def create_app(
    database_path: Path | str,
    *,
    run_id: str | None = None,
    ui_token: str | None = None,
) -> FastAPI:
    """Build an app bound permanently to one explicit SQLite database.

    Request handlers close over the canonical path instead of consulting
    mutable environment or module state. This lets multiple fixture apps run
    concurrently without observing or resetting each other's databases.
    """
    canonical_database = _canonical_database_path(database_path)
    capability_token = ui_token or secrets.token_urlsafe(32)
    if not capability_token:
        raise ValueError("ui_token must not be empty")

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        # Create schema + a seed user once, before the server reports ready.
        db.init_db(canonical_database, seed_user="alice")
        yield

    application = FastAPI(
        title="BrowserTransactionBench message fixture",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.database_path = canonical_database
    application.state.run_id = run_id
    application.state.ui_token = capability_token

    def require_visible_control(
        supplied_token: str | None = Header(default=None, alias="X-BTB-UI-Token"),
    ) -> None:
        if not secrets.compare_digest(supplied_token or "", capability_token):
            raise HTTPException(
                status_code=403,
                detail="fixture API is available only through visible page controls",
            )

    @application.get("/health")
    def health() -> dict[str, object]:
        return {"ok": True, "db": str(canonical_database), "run_id": run_id}

    @application.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        template = (Path(__file__).parent / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        return HTMLResponse(template.replace("__BTB_UI_TOKEN__", capability_token))

    @application.get("/api/drafts", dependencies=[Depends(require_visible_control)])
    def list_drafts() -> list[dict]:
        return db.get_drafts(canonical_database)

    @application.post("/api/drafts", dependencies=[Depends(require_visible_control)])
    def create_draft(payload: DraftIn) -> dict:
        return db.create_draft(
            canonical_database,
            user_id=1,
            subject=payload.subject,
            body=payload.body,
        )

    @application.post(
        "/api/drafts/{draft_id}/save",
        dependencies=[Depends(require_visible_control)],
    )
    def save_draft(draft_id: int) -> dict:
        try:
            return db.save_draft(canonical_database, draft_id=draft_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="draft not found") from exc

    @application.get("/api/messages", dependencies=[Depends(require_visible_control)])
    def list_messages() -> list[dict]:
        return db.messages(canonical_database)

    @application.post(
        "/api/messages/send",
        dependencies=[Depends(require_visible_control)],
    )
    def send_message(payload: SendIn) -> dict:
        """Commit the durable send before FastAPI serializes the response."""
        try:
            return db.send_message(
                canonical_database,
                draft_id=payload.draft_id,
                send_uid=payload.send_uid,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="draft not found") from exc
        except db.DraftNotSavedError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"draft {exc.draft_id} must be saved before sending",
            ) from exc

    return application


# Keep import-based ASGI hosting and ``python btb/app/server.py`` behavior. The
# environment is read once; factory-created apps never consult these globals.
DB_PATH = _canonical_database_path(os.environ.get("BTB_DB", str(db.DEFAULT_DB)))
RUN_ID = os.environ.get("BTB_RUN_ID") or None
app = create_app(DB_PATH, run_id=RUN_ID)


def main() -> None:
    import uvicorn

    host = os.environ.get("BTB_HOST", "127.0.0.1")
    port = int(os.environ.get("BTB_PORT", "7788"))
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
