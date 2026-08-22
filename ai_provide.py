#!/usr/bin/env python3
"""
ai_provide.py — OpenAI-compatible proxy backed by FREE web chats
(deepseek / copilot / chatgpt), reusing the ai_driver.py machinery.

Goal: replace paid LLM API calls with the free web-chat flow we built
(open web chat -> ask -> read the answer from the page).

Any OpenAI-compatible client can use it, including OpenClaw:

  POST http://localhost:8765/v1/chat/completions
  {"model":"any","messages":[{"role":"user","content":"2+2?"}]}

  -> {"choices":[{"message":{"role":"assistant","content":"4"}}]}

USAGE:
  .venv/bin/python3 ai_provide.py --port 8765 --provider deepseek
  # quick test:
  curl http://localhost:8765/v1/chat/completions \
       -H 'Content-Type: application/json' \
       -d '{"model":"x","messages":[{"role":"user","content":"xin chao"}]}'
  # contract test without a browser/profile:
  .venv/bin/python3 ai_provide.py --mock

NOTES / HONEST CAVEATS:
- The web-chat session (real Firefox profile with logins) must be reachable
  from the machine running this proxy (run it on your host, same machine
  where ai_driver.py works).
- Unofficial, breaks on UI changes, rate limits, ToS risk. Use for testing.
- Non-streaming by default; naive SSE streaming when stream=true.
"""

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

ad = None  # ai_driver imported lazily (only when not --mock)

_state = {"lock": threading.Lock(), "mock": False, "provider": "deepseek",
          "pw": None, "ctx": None, "page": None, "headful": False}


def ask(prompt):
    """One web-chat round-trip. Returns answer text or None."""
    global ad
    if _state["mock"]:
        return f"MOCK answer to: {prompt[:80]}"
    if ad is None:
        import ai_driver as ad
    with _state["lock"]:
        try:
            if _state["pw"] is None:
                from playwright.sync_api import sync_playwright
                _state["pw"] = sync_playwright().start()
                user_dir = ad.prepare_real_profile() or ad.PROFILE
                ctx = _state["pw"].firefox.launch_persistent_context(
                    user_data_dir=str(user_dir), headless=not _state["headful"],
                    viewport={"width": 1200, "height": 800})
                _state["ctx"] = ctx
                _state["page"] = ctx.pages[0] if ctx.pages else ctx.new_page()
                print(f"[ai_provide] browser ready (provider={_state['provider']})")
            page = _state["page"]
            marker = f"ai_provide {int(time.time())}"
            reply = ad.ask_provider(page, _state["provider"], None, "",
                                    prompt, attach=False, marker=marker)
            if reply and reply.strip():
                # strip the round marker if the provider echoed it
                return reply.replace(marker, "").strip()
            return None
        except Exception as e:
            print(f"[ai_provide] error: {e}")
            return None


class Handler(BaseHTTPRequestHandler):
    server_version = "ai_provide/1.0"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlparse(self.path).path == "/v1/models":
            self._json(200, {"object": "list", "data": [
                {"id": "web-chat", "object": "model", "owned_by": "ai_provide"}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/v1/chat/completions":
            self._json(404, {"error": {"message": "not found"}})
            return
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
            req = json.loads(self.rfile.read(ln) or b"{}")
        except Exception as e:
            self._json(400, {"error": {"message": f"bad request: {e}"}})
            return
        messages = req.get("messages", [])
        parts = []
        for m in messages:
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                parts.append(f"{m.get('role','user')}: {c}")
        prompt = "\n".join(parts)
        if not prompt:
            self._json(400, {"error": {"message": "no messages"}})
            return

        answer = ask(prompt)
        if answer is None:
            self._json(502, {"error": {
                "message": "web chat failed — sign-in needed? rate limited? "
                           "run ai_driver.py once to verify, or use --mock to test the contract"}})
            return

        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(0, len(answer), 40):
                chunk = answer[i:i + 40]
                payload = json.dumps({"choices": [{"delta": {"content": chunk}}]})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.01)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        self._json(200, {
            "id": "chatcmpl-ai-provide",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.get("model", "web-chat"),
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": answer},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })


def main():
    ap = argparse.ArgumentParser(description="Free web-chat -> OpenAI-compatible proxy")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind address (0.0.0.0 = reachable from other machines / Docker; "
                         "127.0.0.1 = local only)")
    ap.add_argument("--provider", choices=["deepseek", "chatgpt", "copilot"],
                    default="deepseek")
    ap.add_argument("--mock", action="store_true",
                    help="return canned answers (no browser) — tests the API contract")
    ap.add_argument("--headful", action="store_true",
                    help="open a visible browser window (web chats sometimes block headless)")
    args = ap.parse_args()
    _state["mock"] = args.mock
    _state["provider"] = args.provider
    _state["headful"] = args.headful
    if not args.mock:
        print("[ai_provide] mode: real web chat via ai_driver machinery")
    print(f"[ai_provide] listening on http://{args.host}:{args.port}/v1/chat/completions")
    HTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
