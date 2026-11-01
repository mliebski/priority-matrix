import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import fake_ollama  # noqa: E402
from app import ai, coach  # noqa: E402
from app.main import app  # noqa: E402

MODEL = "llama3.2:3b"


def judged(important="yes", urgent="no", reason="It serves your goal.", question=""):
    return {"important": important, "urgent": urgent, "reason": reason, "question": question}


@pytest.fixture
def client(tmp_path, monkeypatch):
    server = fake_ollama.start()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(ai, "URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(ai, "ENV_MODEL", "")
    state = fake_ollama.State
    state.requests, state.replies, state.fail_with, state.models = [], [], "", fake_ollama.MODELS
    with TestClient(app) as c:
        c.put("/api/settings", json={"goals": "Finish my certification", "model": MODEL})
        yield c
    server.shutdown()


def add(client, title="Book the dentist") -> int:
    client.post("/api/items", json={"text": title})
    return client.get("/api/state").json()["items"][-1]["id"]


def test_pasted_list_loses_its_markers(client):
    text = "- [ ] Call Sam\n* Pay rent\n\n1. Renew passport\n  [x] Water plants\nPlain item\n"
    assert client.post("/api/items", json={"text": text}).json() == {"added": 5}
    titles = [i["title"] for i in client.get("/api/state").json()["items"]]
    assert titles == ["Call Sam", "Pay rent", "Renew passport", "Water plants", "Plain item"]


def test_empty_add_is_refused(client):
    assert client.post("/api/items", json={"text": " \n "}).status_code == 422


def test_first_reply_is_a_question_even_if_the_model_is_sure(client):
    item_id = add(client)
    fake_ollama.State.replies = [judged(question="Which goal does this serve?")]
    item = client.post(f"/api/items/{item_id}/coach", json={}).json()
    assert item["suggestion"] is None
    assert item["chat"] == [{"role": "coach", "text": "Which goal does this serve?"}]


def test_silent_model_still_gets_a_question(client):
    item_id = add(client)
    fake_ollama.State.replies = [judged("unsure", "unsure", "", "")]
    item = client.post(f"/api/items/{item_id}/coach", json={}).json()
    assert item["chat"][0]["text"] == coach.QUESTIONS["important"]


@pytest.mark.parametrize("important,urgent,quadrant", [
    ("yes", "yes", "do"), ("yes", "no", "schedule"), ("no", "yes", "delegate"), ("no", "no", "drop"),
])
def test_the_server_maps_the_judgement_to_the_quadrant(client, important, urgent, quadrant):
    item_id = add(client)
    fake_ollama.State.replies = [judged("unsure", "unsure", "", "Why?"), judged(important, urgent)]
    client.post(f"/api/items/{item_id}/coach", json={})
    item = client.post(f"/api/items/{item_id}/coach", json={"text": "Because."}).json()
    assert item["suggestion"] == {"quadrant": quadrant, "reason": "It serves your goal."}
    assert item["quadrant"] is None  # a suggestion never moves the item by itself


def test_coach_stops_asking_and_leaves_the_choice_to_the_person(client):
    item_id = add(client)
    fake_ollama.State.replies = [judged("unsure", "unsure", "", f"Question {n}?") for n in range(4)]
    client.post(f"/api/items/{item_id}/coach", json={})
    for n in range(coach.MAX_ANSWERS):
        item = client.post(f"/api/items/{item_id}/coach", json={"text": f"Answer {n}"}).json()
    assert item["suggestion"] is None
    assert item["chat"][-1]["text"] == coach.STUCK


def test_goals_date_and_due_reach_the_model(client):
    item_id = add(client)
    due = date.today() + timedelta(days=3)
    client.patch(f"/api/items/{item_id}", json={"due": due.isoformat()})
    client.post(f"/api/items/{item_id}/coach", json={})
    request = fake_ollama.State.requests[-1]
    system = request["messages"][0]["content"]
    assert "Finish my certification" in system
    assert date.today().isoformat() in system
    assert "in 3 days" in system
    assert request["format"] == coach.SCHEMA


def test_history_is_sent_back_in_order(client):
    item_id = add(client)
    client.post(f"/api/items/{item_id}/coach", json={})
    client.post(f"/api/items/{item_id}/coach", json={"text": "It keeps my teeth healthy"})
    roles = [m["role"] for m in fake_ollama.State.requests[-1]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


def test_failed_turn_saves_nothing_so_it_can_be_resent(client):
    item_id = add(client)
    client.post(f"/api/items/{item_id}/coach", json={})
    fake_ollama.State.fail_with = "out of memory"
    res = client.post(f"/api/items/{item_id}/coach", json={"text": "My answer"})
    assert res.status_code == 502
    assert "out of memory" in res.json()["detail"]
    fake_ollama.State.fail_with = ""
    item = client.post(f"/api/items/{item_id}/coach", json={"text": "My answer"}).json()
    assert [m["text"] for m in item["chat"] if m["role"] == "you"] == ["My answer"]


def test_unreadable_answer_is_retried_once_then_reported(client):
    item_id = add(client)
    fake_ollama.State.replies = ["not json", judged("unsure", "unsure", "", "Why?")]
    assert client.post(f"/api/items/{item_id}/coach", json={}).status_code == 200
    item_id = add(client, "Second")
    fake_ollama.State.replies = ["not json", "still not json"]
    assert client.post(f"/api/items/{item_id}/coach", json={}).status_code == 502


def test_think_tags_are_ignored(client):
    item_id = add(client)
    fake_ollama.State.replies = ['<think>hmm</think>{"important":"unsure","urgent":"unsure","reason":"","question":"Why?"}']
    assert client.post(f"/api/items/{item_id}/coach", json={}).json()["chat"][0]["text"] == "Why?"


def test_ollama_down_is_a_clear_error_and_the_list_still_works(client, monkeypatch):
    monkeypatch.setattr(ai, "URL", "http://127.0.0.1:9")
    item_id = add(client)
    assert client.get("/api/ai").json()["ok"] is False
    res = client.post(f"/api/items/{item_id}/coach", json={})
    assert res.status_code == 503
    assert "not reachable" in res.json()["detail"]
    assert client.patch(f"/api/items/{item_id}", json={"quadrant": "do"}).json()["quadrant"] == "do"


def test_model_choice(client):
    client.put("/api/settings", json={"model": ""})
    status = client.get("/api/ai").json()
    assert not status["ok"] and "Pick a model" in status["detail"]
    fake_ollama.State.models = [{"name": "only:1b"}]
    assert client.get("/api/ai").json()["model"] == "only:1b"
    client.put("/api/settings", json={"model": "gone:1b"})
    assert "not installed" in client.get("/api/ai").json()["detail"]


def test_reason_is_kept_only_when_the_suggestion_is_followed(client):
    item_id = add(client)
    fake_ollama.State.replies = [judged("unsure", "unsure", "", "Why?"), judged("yes", "no")]
    client.post(f"/api/items/{item_id}/coach", json={})
    client.post(f"/api/items/{item_id}/coach", json={"text": "Because."})
    item = client.patch(f"/api/items/{item_id}", json={"quadrant": "schedule"}).json()
    assert item["reason"] == "It serves your goal."
    item = client.patch(f"/api/items/{item_id}", json={"quadrant": "drop"}).json()
    assert item["quadrant"] == "drop" and item["reason"] == ""


def test_done_undo_due_clear_and_delete(client):
    item_id = add(client)
    assert client.patch(f"/api/items/{item_id}", json={"done": True}).json()["done_at"]
    assert client.patch(f"/api/items/{item_id}", json={"done": False}).json()["done_at"] is None
    client.patch(f"/api/items/{item_id}", json={"due": "2030-01-02"})
    assert client.patch(f"/api/items/{item_id}", json={"due": None}).json()["due"] is None
    assert client.delete(f"/api/items/{item_id}").status_code == 204
    assert client.patch(f"/api/items/{item_id}", json={"done": True}).status_code == 404


def test_restart_clears_the_conversation(client):
    item_id = add(client)
    client.post(f"/api/items/{item_id}/coach", json={})
    item = client.delete(f"/api/items/{item_id}/coach").json()
    assert item["chat"] == [] and item["suggestion"] is None


def test_page_and_health_are_served(client):
    assert client.get("/health").json() == {"ok": True}
    assert "Priority Matrix" in client.get("/").text
