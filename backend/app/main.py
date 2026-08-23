"""HTTP layer: SSE chat, action confirmation, and the operations dashboard."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.actions.store import ACTIONS
from app.agent.agent import Agent
from app.agent.llm import LLMConfig
from app.agent.tools import Toolbox
from app.corpus.search import superseded_pairs
from app.config import ASSUMPTION_NOTES
from app.data.store import load_store
from app.domain import insights, precedence
from app.security.session import DEMO_PRINCIPALS, AccessDenied, Principal

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="ParcelPilot AI Support", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def principal_or_404(session: str) -> Principal:
    p = DEMO_PRINCIPALS.get(session)
    if p is None:
        raise HTTPException(404, f"unknown session {session!r}")
    return p


class ChatRequest(BaseModel):
    session: str = Field(description="key from /api/sessions")
    message: str
    history: list[dict[str, Any]] = Field(default_factory=list)


class ConfirmRequest(BaseModel):
    session: str


@app.get("/api/health")
def health() -> dict:
    cfg = LLMConfig.from_env()
    store = load_store()
    return {
        "ok": True,
        "model_configured": cfg.configured,
        "provider": cfg.provider,
        "model": cfg.model,
        "reference_time": store.now.strftime("%A %d %B %Y, %H:%M"),
        "accounts": len(store.accounts),
        "orders": len(store.orders),
        "tickets": len(store.tickets),
    }


@app.get("/api/sessions")
def sessions() -> dict:
    store = load_store()
    out = []
    for key, p in DEMO_PRINCIPALS.items():
        acc = store.accounts.get(p.account_id) if p.account_id else None
        out.append({
            "key": key,
            "role": p.role.value,
            "display_name": p.display_name,
            "account_id": p.account_id,
            "plan": acc.plan if acc else None,
            "is_internal": p.is_internal,
            "capabilities": sorted(p.capabilities),
            "tools": Toolbox(p).names(),
        })
    return {"sessions": out}


@app.get("/api/context/{session}")
def context(session: str) -> dict:
    """Everything the UI needs to render sidebars for this identity."""
    p = principal_or_404(session)
    store = load_store()
    tb = Toolbox(p)

    accounts = []
    for acc in tb.store.accounts():
        accounts.append({
            "account_id": acc.account_id,
            "account_name": acc.account_name,
            "plan": acc.plan,
            "has_agreement": acc.has_contract,
            "governs": precedence.explain_for_account(acc, store.now)["topics"],
        })

    return {
        "reference_time": store.now.strftime("%A %d %B %Y, %H:%M"),
        "snapshot_note": store.snapshot.notes,
        "orders": [
            {"order_id": o.order_id, "account_id": o.account_id,
             "status": o.status, "carrier": o.carrier}
            for o in tb.store.orders()
        ],
        "tickets": [
            {"ticket_id": t.ticket_id, "account_id": t.account_id,
             "status": t.status, "subject": t.subject}
            for t in tb.store.tickets()
        ],
        "accounts": accounts,
        # Withheld server-side for customers, not merely hidden in the nav.
        "documents": superseded_pairs() if p.is_internal else [],
        "assumptions": ASSUMPTION_NOTES if p.is_internal else {},
        "tools": [
            {"name": s["function"]["name"],
             "description": s["function"]["description"]}
            for s in tb.schemas()
        ],
    }


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


@app.post("/api/chat")
def chat(req: ChatRequest) -> StreamingResponse:
    principal = principal_or_404(req.session)
    agent = Agent(principal)

    def stream() -> Iterator[str]:
        try:
            for event in agent.run(req.message, req.history):
                payload = event.to_dict()
                if event.type == "done":
                    for aid in payload.get("record", {}).get("pending_action_ids", []):
                        action = ACTIONS.get(aid)
                        if action:
                            payload.setdefault("pending_actions", []).append(action.public())
                yield _sse(payload)
        except Exception as exc:  # never leave the client hanging
            yield _sse({"type": "error", "kind": type(exc).__name__, "detail": str(exc)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Confirmation lives here, NOT in the model's tool surface.
@app.get("/api/actions/{session}")
def list_actions(session: str) -> dict:
    p = principal_or_404(session)
    return {
        "actions": [a.public() for a in ACTIONS.visible(p)],
        "audit": ACTIONS.audit(p),
    }


@app.post("/api/actions/{action_id}/confirm")
def confirm_action(action_id: str, req: ConfirmRequest) -> dict:
    p = principal_or_404(req.session)
    action = ACTIONS.get(action_id)
    if action is None:
        raise HTTPException(404, "unknown action")
    try:
        token = ACTIONS.confirmation_token(action_id)
        committed = ACTIONS.commit(p, action_id, token, now=load_store().now)
    except AccessDenied as exc:
        raise HTTPException(403, str(exc)) from exc
    except (ValueError, PermissionError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "committed": True,
        "action": committed.public(),
        "message": (
            f"{committed.kind.value.replace('_', ' ').title()} created as "
            f"{committed.reference}."
        ),
    }


@app.post("/api/actions/{action_id}/cancel")
def cancel_action(action_id: str, req: ConfirmRequest) -> dict:
    p = principal_or_404(req.session)
    if ACTIONS.get(action_id) is None:
        raise HTTPException(404, "unknown action")
    return {"cancelled": True, "action": ACTIONS.cancel(p, action_id).public()}


@app.get("/api/dashboard/{session}")
def dashboard(session: str) -> dict:
    p = principal_or_404(session)
    if not p.can("view_operations_dashboard"):
        raise HTTPException(403, "the operations view is for ParcelPilot staff only")
    return insights.detect(load_store())


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
