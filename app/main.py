"""The web app: a small JSON API plus the single page in app/static."""
import json
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ai, coach, db

Conn = Annotated[sqlite3.Connection, Depends(db.session)]
Quadrant = Literal["do", "schedule", "delegate", "drop"]
Title = Annotated[str, Field(min_length=1, max_length=500)]

# "- [ ] Call Sam", "* Call Sam" and "1. Call Sam" are all just "Call Sam".
LIST_MARKER = re.compile(r"^\s*(?:[-*•]\s+)?(?:\[[ xX]?\]\s+)?(?:\d+[.)]\s+)?")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title="Priority Matrix", lifespan=lifespan)


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
    return {"items": [db.to_item(r) for r in rows],
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
    titles = [LIST_MARKER.sub("", line).strip()[:500] for line in body.text.splitlines()]
    titles = [t for t in titles if t]
    if not titles:
        raise HTTPException(422, "Type at least one item.")
    conn.executemany("INSERT INTO items (title) VALUES (?)", [(t,) for t in titles])
    return {"added": len(titles)}


@app.patch("/api/items/{item_id}")
def change_item(item_id: int, body: ItemChange, conn: Conn):
    item = require_item(conn, item_id)
    sent = body.model_fields_set
    if "title" in sent and body.title and body.title.strip():
        conn.execute("UPDATE items SET title = ? WHERE id = ?", (body.title.strip(), item_id))
    if "due" in sent:
        conn.execute("UPDATE items SET due = ? WHERE id = ?",
                     (body.due.isoformat() if body.due else None, item_id))
    if "quadrant" in sent:
        # Only keep the coach's reason if this is where the coach suggested it goes.
        suggestion = item["suggestion"]
        reason = suggestion["reason"] if suggestion and suggestion["quadrant"] == body.quadrant else ""
        conn.execute("UPDATE items SET quadrant = ?, reason = ? WHERE id = ?", (body.quadrant, reason, item_id))
    if "done" in sent:
        conn.execute("UPDATE items SET done_at = CASE WHEN ? THEN datetime('now') END WHERE id = ?",
                     (bool(body.done), item_id))
    return db.get_item(conn, item_id)


@app.delete("/api/items/{item_id}", status_code=204)
def delete_item(item_id: int, conn: Conn):
    require_item(conn, item_id)
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))


@app.post("/api/items/{item_id}/coach")
def coach_turn(item_id: int, body: Answer, conn: Conn):
    """Send the person's answer (empty to start) and get the coach's next reply.

    Nothing is saved unless the model answers, so a failed turn can simply be sent again.
    """
    item = require_item(conn, item_id)
    status = ai.status(db.get_setting(conn, "model"))
    if not status["ok"]:
        raise HTTPException(503, status["detail"])
    chat = item["chat"]
    answer = body.text.strip()
    if answer:
        chat = [*chat, {"role": "you", "text": answer}]
    elif chat:
        raise HTTPException(422, "Type an answer first.")
    goals = db.get_setting(conn, "goals")
    try:
        chat, suggestion = coach.turn(status["model"], goals, item, chat, date.today())
    except ai.AiError as e:
        raise HTTPException(502, str(e))
    conn.execute("UPDATE items SET chat = ?, suggestion = ? WHERE id = ?",
                 (json.dumps(chat), json.dumps(suggestion) if suggestion else None, item_id))
    return require_item(conn, item_id)  # it may have been deleted while the model was thinking


@app.delete("/api/items/{item_id}/coach")
def restart_coach(item_id: int, conn: Conn):
    require_item(conn, item_id)
    conn.execute("UPDATE items SET chat = '[]', suggestion = NULL WHERE id = ?", (item_id,))
    return db.get_item(conn, item_id)


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
