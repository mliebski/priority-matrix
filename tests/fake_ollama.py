"""A stand-in for the Ollama HTTP API, for tests and for trying the coach without a model.

    python tests/fake_ollama.py [port]      # default 11434

Implements /api/version, /api/tags and non-streaming /api/chat with a JSON schema in
"format". Tests set State.replies; otherwise it plays a short believable conversation.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = [{"name": "llama3.2:3b"}, {"name": "qwen3:8b"}]


class State:
    requests: list[dict] = []
    replies: list[str | dict] = []   # answers to give, in order; a str is sent as is
    delay = 0.0
    fail_with = ""
    models = MODELS


def conversation(messages: list[dict]) -> dict:
    answers = [m["content"].lower() for m in messages if m["role"] == "user"][1:]
    if not answers:
        return {"important": "unsure", "urgent": "unsure", "reason": "",
                "question": "Which of your goals does this move forward, if any?"}
    if len(answers) == 1:
        return {"important": "unsure", "urgent": "unsure", "reason": "",
                "question": "What actually happens, and when, if this waits two weeks?"}
    important = "no" if any(w in answers[0] for w in ("none", "nothing", "no ")) else "yes"
    urgent = "yes" if any(w in answers[1] for w in ("tomorrow", "friday", "week", "fine")) else "no"
    return {"important": important, "urgent": urgent, "question": "",
            "reason": "You said what's at stake and when, so here's how I read it."}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/version":
            return self._json({"version": "0.12.6-fake"})
        if self.path == "/api/tags":
            return self._json({"models": State.models})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        State.requests.append(req)
        if self.path != "/api/chat":
            return self._json({"error": "not found"}, 404)
        if State.fail_with:
            return self._json({"error": State.fail_with}, 500)
        if req["model"] not in [m["name"] for m in State.models]:
            return self._json({"error": f"model '{req['model']}' not found"}, 404)
        time.sleep(State.delay)
        reply = State.replies.pop(0) if State.replies else conversation(req["messages"])
        content = reply if isinstance(reply, str) else json.dumps(reply)
        self._json({"model": req["model"], "message": {"role": "assistant", "content": content}, "done": True})

    def log_message(self, *args):
        pass


def start(port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    State.delay = 1.5  # make the "thinking" state visible when trying it out
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 11434
    print(f"Fake Ollama on http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
