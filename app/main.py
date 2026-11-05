"""The web app: a small JSON API plus the single page in app/static."""
import base64
import binascii
import hmac
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ai, coach, db

Conn = Annotated[sqlite3.Connection, Depends(db.session)]
Quadrant = Literal["do", "schedule", "delegate", "drop"]
MAX_TITLE = 500
Title = Annotated[str, Field(min_length=1, max_length=MAX_TITLE)]

# "- [ ] Call Sam", "* Call Sam" and "1. Call Sam" are all just "Call Sam".
LIST_MARKER = re.compile(r"^\s*(?:[-*•]\s+)?(?:\[[ xX]?\]\s+)?(?:\d+[.)]\s+)?")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title="Priority Matrix", lifespan=lifespan)


def _password_ok(header: str) -> bool:
    """HTTP Basic: any user name, the password must be APP_PASSWORD."""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        _, _, given = base64.b64decode(token, validate=True).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return hmac.compare_digest(given.encode(), os.environ["APP_PASSWORD"].encode())


@app.middleware("http")
async def require_password(request: Request, call_next):
    """Optional login: when APP_PASSWORD is set, everything but /health asks for it."""
    locked = os.environ.get("APP_PASSWORD") and request.url.path != "/health"
    if locked and not _password_ok(request.headers.get("authorization", "")):
        return Response("Password required.", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Priority Matrix"'})
    return await call_next(request)


class NewItems(BaseModel):
    text: Annotated[str, Field(max_length=50_000)]


class ItemChange(BaseModel):
    title: Title | None = None
    due: date | None = None
    quadrant: Quadrant | None = None
    done: bool | None = None


class Answer(BaseModel):
    text: Annotated[str, Field(max_length=2000)] = ""


class Settings(BaseModel):
    goals: Annotated[str, Field(max_length=4000)] = ""
    model: Annotated[str, Field(max_length=200)] = ""


def require_item(conn: sqlite3.Connection, item_id: int) -> dict:
    item = db.get_item(conn, item_id)
    if not item:
        raise HTTPException(404, "That item no longer exists.")
    return item


@app.get("/health")
def health(conn: Conn):
    conn.execute("SELECT 1")
    return {"ok": True}


@app.get("/api/state")
def state(conn: Conn):
    rows = conn.execute("SELECT * FROM items ORDER BY due IS NULL, due, id").fetchall()
    # Conversations are left out; GET /api/items/{id} has them. "today" is the server's, which is
    # the date the coach uses, so the page and the coach agree on what is overdue.
    return {"items": [db.to_item(r, with_chat=False) for r in rows], "today": date.today().isoformat(),
            "goals": db.get_setting(conn, "goals"), "model": db.get_setting(conn, "model")}


@app.get("/api/ai")
def ai_status(conn: Conn):
    """Separate from /api/state so the list still loads at once when Ollama is down."""
    return ai.status(db.get_setting(conn, "model"))


@app.put("/api/settings")
def save_settings(body: Settings, conn: Conn):
    db.set_setting(conn, "goals", body.goals.strip())
    db.set_setting(conn, "model", body.model)
    return {"ok": True}


@app.post("/api/items", status_code=201)
def add_items(body: NewItems, conn: Conn):
    """Takes one item, or a whole list pasted in with one item per line."""
    titles = [LIST_MARKER.sub("", line).strip() for line in body.text.splitlines()]
    titles = [t for t in titles if t]
    if not titles:
        raise HTTPException(422, "Type at least one item.")
    shortened = sum(len(t) > MAX_TITLE for t in titles)
    conn.executemany("INSERT INTO items (title) VALUES (?)", [(t[:MAX_TITLE],) for t in titles])
    return {"added": len(titles), "shortened": shortened}


@app.get("/api/items/{item_id}")
def get_item(item_id: int, conn: Conn):
    return require_item(conn, item_id)


@app.patch("/api/items/{item_id}")
def change_item(item_id: int, body: ItemChange, conn: Conn):
    item = require_item(conn, item_id)
    sent = body.model_fields_set
    changes: dict[str, str] = {}   # column -> SQL expression, with one ? for its value
    values: list = []

    def set_(column: str, value, expression: str = "?") -> None:
        changes[column] = expression
        values.append(value)

    if "title" in sent:
        title = (body.title or "").strip()
        if not title:
            raise HTTPException(422, "The title can't be empty.")
        set_("title", title)
    if "due" in sent:
        set_("due", body.due.isoformat() if body.due else None)
    if "quadrant" in sent:
        # Only keep the coach's reason if this is where the coach suggested it goes.
        suggestion = item["suggestion"]
        set_("quadrant", body.quadrant)
        set_("reason", suggestion["reason"] if suggestion and suggestion["quadrant"] == body.quadrant else "")
    if "done" in sent:
        set_("done_at", bool(body.done), "CASE WHEN ? THEN datetime('now') END")
    if not changes:
        raise HTTPException(422, "Nothing to change.")
    assignments = ", ".join(f"{column} = {expression}" for column, expression in changes.items())
    conn.execute(f"UPDATE items SET {assignments} WHERE id = ?", (*values, item_id))
    return db.get_item(conn, item_id)


@app.delete("/api/items/{item_id}", status_code=204)
def delete_item(item_id: int, conn: Conn):
    require_item(conn, item_id)
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))


@app.post("/api/items/{item_id}/coach")
def coach_turn(item_id: int, body: Answer, conn: Conn):
    """Send the person's answer (empty to start) and get the coach's next reply.

    Nothing is saved unless the model answers, so a failed turn can simply be sent again.
    If the conversation changed while the model was thinking (another window answered,
    or it was restarted), the reply is dropped with a 409 instead of overwriting it.
    """
    item = require_item(conn, item_id)
    model = db.get_setting(conn, "model") or ai.ENV_MODEL
    if not model:
        status = ai.status("")
        if not status["ok"]:
            raise HTTPException(503, status["detail"])
        model = status["model"]
    chat = item["chat"]
    answer = body.text.strip()
    if answer:
        chat = [*chat, {"role": "you", "text": answer}]
    elif chat:
        raise HTTPException(422, "Type an answer first.")
    goals = db.get_setting(conn, "goals")
    try:
        chat, suggestion = coach.turn(model, goals, item, chat, date.today())
    except ai.Unreachable as e:
        raise HTTPException(503, str(e))
    except ai.AiError as e:
        raise HTTPException(502, str(e))
    saved = conn.execute("UPDATE items SET chat = ?, suggestion = ?, version = version + 1 "
                         "WHERE id = ? AND version = ?",
                         (json.dumps(chat), json.dumps(suggestion) if suggestion else None,
                          item_id, item["version"]))
    if not saved.rowcount:
        require_item(conn, item_id)  # deleted while the model was thinking: 404
        raise HTTPException(409, "This item changed in another window while the coach was thinking. "
                                 "Its latest version is shown; send your answer again if you still want to.")
    return db.get_item(conn, item_id)


@app.delete("/api/items/{item_id}/coach")
def restart_coach(item_id: int, conn: Conn):
    require_item(conn, item_id)
    conn.execute("UPDATE items SET chat = '[]', suggestion = NULL, version = version + 1 WHERE id = ?",
                 (item_id,))
    return db.get_item(conn, item_id)


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
