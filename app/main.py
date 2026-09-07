"""FastAPI app.

Deliberately thin. The brief says to stop if you are building CRUD or styling
components, so there is no auth, no accounts, no persistence beyond the page index,
and the templates are three files.

The one piece of UI that earns real effort is the Context Inspector (design spec
§8): a reviewer must be able to see exactly what the model received without opening
MLflow.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import agent, db, retrieval, scanner, tracing
from app.config import ANTHROPIC_API_KEY, ASSISTANT_MODEL, FIXTURES_DIR, ROOT
from app.context import FIRST_TURN_REQUEST, select_recipe

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Accessibility & Performance Fix Assistant")
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))

# Fixtures are served over HTTP rather than read from disk: file:// gives no
# Resource Timing, and therefore no LCP data.
app.mount("/fixtures", StaticFiles(directory=str(FIXTURES_DIR)), name="fixtures")

SELF_ORIGIN = "http://127.0.0.1:8000"


@app.on_event("startup")
def _startup() -> None:
    db.init()
    tracing.init()
    # ~22s on CPU. Start it now, in the background, so it is not paid by whoever
    # clicks "explain and fix" first. Turn 0 does not wait for it either way -
    # see retrieval._embedder.
    retrieval.warm_encoder()


def _fixture_names() -> list[str]:
    return sorted(p.name for p in FIXTURES_DIR.glob("*.html"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "pages": db.latest_pages(),
        "fixtures": _fixture_names(),
        "has_key": bool(ANTHROPIC_API_KEY),
        "model": ASSISTANT_MODEL,
        "tracing": tracing.enabled(),
    })


@app.post("/scan")
def do_scan(target: str = Form(...)):
    target = target.strip()
    if not target:
        raise HTTPException(400, "No target given.")
    if not target.startswith(("http://", "https://")):
        if not (FIXTURES_DIR / target).exists():
            raise HTTPException(404, f"No fixture named {target}")
        target = f"{SELF_ORIGIN}/fixtures/{target}"
    try:
        payload = scanner.scan(target)
    except scanner.ScannerError as exc:
        raise HTTPException(502, f"Scan failed: {exc}") from exc
    page_id = db.ingest_scan(payload)
    return RedirectResponse(f"/page/{page_id}", status_code=303)


@app.get("/page/{page_id}", response_class=HTMLResponse)
def page_view(request: Request, page_id: int):
    page = db.get_page(page_id)
    if not page:
        raise HTTPException(404, "Unknown page")
    issues = db.get_issues(page_id)
    for i in issues:
        i["recipe"] = select_recipe(i["rule"])
        i["sc_tags"] = json.loads(i["sc_tags_json"] or "[]")
    return templates.TemplateResponse(request, "page.html", {
        "page": page, "issues": issues,
        "perf": json.loads(page["perf_json"] or "{}"),
    })


@app.get("/issue/{issue_id}", response_class=HTMLResponse)
def issue_view(request: Request, issue_id: int):
    issue = db.get_issue(issue_id)
    if not issue:
        raise HTTPException(404, "Unknown issue")
    page = db.get_page(issue["page_id"])
    conv_id = f"conv-{issue_id}"
    db.create_conversation(conv_id, issue_id, datetime.now(timezone.utc).isoformat())
    messages = db.get_messages(conv_id)
    return templates.TemplateResponse(request, "issue.html", {
        "issue": issue, "page": page,
        "recipe": select_recipe(issue["rule"]),
        "sc_tags": json.loads(issue["sc_tags_json"] or "[]"),
        "conv_id": conv_id,
        "messages": messages,
        "first_turn_request": FIRST_TURN_REQUEST,
        "has_key": bool(ANTHROPIC_API_KEY),
    })


@app.post("/issue/{issue_id}/reset")
def reset_conversation(issue_id: int):
    conv_id = f"conv-{issue_id}"
    with db.connect() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
    return RedirectResponse(f"/issue/{issue_id}", status_code=303)


@app.get("/issue/{issue_id}/stream")
def stream(issue_id: int, message: str = ""):
    """SSE endpoint. Turn 0 sends no tools and starts streaming immediately."""
    issue = db.get_issue(issue_id)
    if not issue:
        raise HTTPException(404, "Unknown issue")
    if not ANTHROPIC_API_KEY:
        raise HTTPException(400, "ANTHROPIC_API_KEY is not set. Add it to .env.")

    conv_id = f"conv-{issue_id}"
    stored = db.get_messages(conv_id)
    history = [{"role": m["role"], "content": m["content"]} for m in stored]
    user_message = message.strip() or FIRST_TURN_REQUEST

    def event_stream():
        turn = len(history)
        db.add_message(conv_id, turn, "user", user_message,
                       created_at=datetime.now(timezone.utc).isoformat())
        final_event = None
        try:
            for event in agent.run_turn(issue["page_id"], issue, history, user_message):
                if event["type"] == "done":
                    final_event = event
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:  # noqa: BLE001
            log.exception("turn failed")
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
            return

        if final_event:
            db.add_message(
                conv_id, turn + 1, "assistant", final_event["answer"],
                bundle_json=json.dumps(final_event["bundle"]),
                citations_json=json.dumps(final_event["citations"]),
                tool_calls_json=json.dumps(final_event["tools"]),
                created_at=datetime.now(timezone.utc).isoformat())
        yield "data: {\"type\": \"end\"}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/page/{page_id}/rescan")
def rescan(page_id: int):
    """Full re-scan is a user action, not a tool (design spec §6.2)."""
    page = db.get_page(page_id)
    if not page:
        raise HTTPException(404, "Unknown page")
    payload = scanner.scan(page["resolved_url"] or page["url"])
    new_id = db.ingest_scan(payload)
    return RedirectResponse(f"/page/{new_id}", status_code=303)
