#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class State:
    messages: list[dict] = []
    expo_status = 200
    lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesMobileFake/1.0"

    def log_message(self, _format, *_args):
        return

    def _json(self, status: int, payload: dict):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        return json.loads(
            self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}"
        )

    def do_GET(self):
        if self.path.endswith("/models"):
            self._json(
                200,
                {"object": "list", "data": [{"id": "mock-model", "object": "model"}]},
            )
        elif self.path == "/messages":
            with State.lock:
                payload = list(State.messages)
            self._json(200, {"messages": payload})
        elif self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self):
        if self.path.endswith("/chat/completions"):
            body = self._body()
            content = "Hermes real respondió mediante el proveedor de pruebas."
            payload = {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model", "mock-model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 8,
                    "total_tokens": 20,
                },
            }
            if body.get("stream"):
                chunks = [
                    {
                        "id": payload["id"],
                        "object": "chat.completion.chunk",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": content},
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": payload["id"],
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    },
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self._json(200, payload)
        elif self.path == "/--/api/v2/push/send":
            body = self._body()
            with State.lock:
                State.messages.append(body)
                status = State.expo_status
            if status >= 500:
                self._json(status, {"errors": [{"message": "temporary"}]})
            else:
                self._json(
                    200, {"data": {"status": "ok", "id": f"ticket-{uuid.uuid4().hex}"}}
                )
        elif self.path == "/--/api/v2/push/getReceipts":
            body = self._body()
            self._json(
                200,
                {"data": {ticket: {"status": "ok"} for ticket in body.get("ids", [])}},
            )
        elif self.path.startswith("/mode/"):
            with State.lock:
                State.expo_status = int(self.path.rsplit("/", 1)[1])
            self._json(200, {"status": State.expo_status})
        elif self.path == "/reset":
            with State.lock:
                State.messages.clear()
                State.expo_status = 200
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not_found"})


if __name__ == "__main__":
    mode = sys.argv[1]
    port = 8081 if mode == "llm" else 8082
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
