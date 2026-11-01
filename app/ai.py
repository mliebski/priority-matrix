"""Client for the local Ollama server. To-do items are only ever sent to this address.

Ollama runs natively on the box; from inside the container the host is reachable
as host.docker.internal (see docker-compose.yml and the README).
"""
import json
import os
import re
import urllib.error
import urllib.request

URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/")
ENV_MODEL = os.environ.get("OLLAMA_MODEL", "")
TIMEOUT = 300          # a CPU-only box can take minutes to load a model and answer
STATUS_TIMEOUT = 2
KEEP_ALIVE = "15m"     # keep the model in memory while working through a list

THINK = re.compile(r"<think>.*?</think>", re.S)


class AiError(Exception):
    pass


def _open(path: str, payload: dict | None = None, timeout: float = STATUS_TIMEOUT):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{URL}{path}", data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    # Never go through a proxy: the server is on the home network.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except ValueError:
            pass
        raise AiError(f"Ollama said: {detail[:300]}")
    except (OSError, ValueError) as e:
        raise AiError(f"Ollama is not reachable at {URL} ({getattr(e, 'reason', e)}).")


def models() -> list[str]:
    with _open("/api/tags") as r:
        return sorted(m["name"] for m in json.load(r).get("models", []))


def status(saved_model: str) -> dict:
    """What the page needs to know: can we reach Ollama, and which model will be used?"""
    try:
        names = models()
    except AiError as e:
        return {"ok": False, "detail": str(e), "models": [], "model": ""}
    model = saved_model or ENV_MODEL or (names[0] if len(names) == 1 else "")
    if model and model not in names:
        return {"ok": False, "detail": f"Model {model} is not installed. Pick another in Settings.",
                "models": names, "model": ""}
    if not names:
        return {"ok": False, "detail": "Ollama has no models yet. Run: ollama pull llama3.2",
                "models": [], "model": ""}
    if not model:
        return {"ok": False, "detail": "Pick a model in Settings.", "models": names, "model": ""}
    return {"ok": True, "detail": f"Using {model}", "models": names, "model": model}


def chat_json(model: str, messages: list[dict], schema: dict) -> dict:
    """One complete answer, constrained to a JSON schema. A bad answer is retried once."""
    payload = {"model": model, "messages": messages, "stream": False, "keep_alive": KEEP_ALIVE,
               "format": schema, "think": False,  # coaching needs a quick reply, not long reasoning
               "options": {"temperature": 0.2, "num_ctx": 8192}}
    for _ in range(2):
        with _open("/api/chat", payload, timeout=TIMEOUT) as r:
            answer = json.load(r)
        text = THINK.sub("", answer.get("message", {}).get("content", "")).strip()
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise AiError("The model's answer could not be read. Try again, or place the item yourself.")
