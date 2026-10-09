"""Development-only fake inference endpoint, NOT a speech or translation model.

Run: python -m nastya_worker.mock_api
Only binds loopback. Returns canned output to validate API wiring locally.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 8 * 1024 * 1024 + 4096:
            self._send(413, {"error": "invalid request size"})
            return
        body = self.rfile.read(length)
        if self.path == "/v1/audio/transcriptions":
            if b'name="file"' not in body:
                self._send(400, {"error": "file field required"})
                return
            self._send(200, {"text": "Mock transcript: no speech recognition performed."})
        elif self.path == "/v1/translate":
            try:
                data = json.loads(body)
                src = data["source_language"]
                dst = data["target_language"]
                text = data["text"]
                if src not in ("ru", "vi") or dst not in ("ru", "vi"):
                    raise ValueError("invalid language")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("invalid text")
            except (ValueError, KeyError, TypeError):
                self._send(400, {"error": "invalid payload"})
                return
            self._send(200, {"translated_text": f"[MOCK {src}->{dst}] {text}"})
        else:
            self._send(404, {"error": "not found"})

    def _send(self, status: int, data: dict[str, str]) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        # Avoid recording request data; no transcript or audio logs.
        return


def main() -> None:
    with ThreadingHTTPServer(("127.0.0.1", 8765), Handler) as server:
        print("MOCK only: http://127.0.0.1:8765 (NOT a real model)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()
