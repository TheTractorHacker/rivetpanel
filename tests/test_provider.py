"""RivetProvider against a local mock of the RivetIT/RivetMSP /api/v1 surface.
Run: python3 tests/test_provider.py"""
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from psa.rivet import RivetProvider  # noqa: E402

SEEN = []

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        SEEN.append(("GET", self.path, self.headers.get("X-Api-Key")))
        if self.path.startswith("/api/v1/ticket-categories"):
            return self._send(200, [{"id": 1, "name": "Hardware"}])
        self._send(200, {"success": True, "data": []})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); self.rfile.read(n)
        SEEN.append(("POST", self.path, self.headers.get("X-Api-Key")))
        self._send(200, {"id": 7, "number": 1001})

srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
for key in ("rivet_base_url", "itflow_base_url"):  # new key and legacy fallback
    SEEN.clear()
    p = RivetProvider({key: base + "/", "api_key": "K", "client_id": 1, "contact_id": 0, "priority": "Low"})
    p.list_tickets(); p.list_categories(); p.poll_chat(7); p.create_ticket("s", "d")
    assert SEEN and all(k == "K" for _, _, k in SEEN), SEEN
    assert any(m == "POST" and path.startswith("/api/v1/tickets") for m, path, _ in SEEN), SEEN
    print(key, "ok", len(SEEN), "requests")
