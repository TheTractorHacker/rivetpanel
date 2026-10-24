"""PanelProvider against a mock of the RivetCore Panel API (docs/panel/PANEL_API.md).
Run: python3 tests/test_panel_provider.py"""
import json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
import credstore  # noqa: E402
from psa.panel import PanelProvider, PanelError  # noqa: E402
from psa.rivet_select import make_provider  # noqa: E402
from psa.rivet import RivetProvider  # noqa: E402

ST = {"tokens_left": 1, "revoked": False, "requests": [], "tickets": {}, "ids": {}, "created": 0}
CRED = "rvpd_" + "a" * 64
LIMITS = {"attachment_extensions": ["png", "txt"]}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body):
        body = {**body, "server_time": "2026-10-23T00:00:00Z"}
        d = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)
    def _auth(self):
        if self.headers.get("Authorization") != "Bearer " + CRED:
            self._send(401, {"error": "bad", "code": "invalid_token"}); return False
        if ST["revoked"]:
            self._send(401, {"error": "revoked", "code": "revoked"}); return False
        return True
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query); ST["requests"].append(("GET", u.path, dict(self.headers)))
        if not self._auth(): return
        ep = u.path.rsplit("/", 1)[-1]
        if ep == "panel_categories": return self._send(200, {"categories": [{"category_id": 5, "name": "Hard&lt;ware"}]})
        if ep == "panel_tickets":
            return self._send(200, {"tickets": [{"ticket_id": t, "subject": s, "status": "Open", "created_at": "2026-10-01T09:00:00Z"} for t, s in ST["tickets"].items()]})
        if ep == "panel_ticket_chat":
            after = int(q.get("after", ["0"])[0])
            msgs = [m for m in [{"message_id": 1, "from": "staff", "author": "Tom", "body": "Hi", "created_at": "x"},
                                {"message_id": 2, "from": "you", "author": "You", "body": "Thanks", "created_at": "y"}] if m["message_id"] > after]
            return self._send(200, {"ticket": {"ticket_id": 7, "subject": "s", "status": "In Progress"}, "messages": msgs})
        self._send(404, {"error": "nf", "code": "not_found"})
    def do_POST(self):
        u = urlparse(self.path); body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        ST["requests"].append(("POST", u.path, dict(self.headers), body)); ep = u.path.rsplit("/", 1)[-1]
        if ep == "panel_enroll":
            if ST["tokens_left"] < 1 or body.get("enrollment_token") != "rvtp1.good":
                return self._send(403, {"error": "used", "code": "forbidden"})
            ST["tokens_left"] -= 1
            return self._send(201, {"device_id": 9, "device_token": CRED, "can_create_tickets": True, "limits": LIMITS, "client": {"client_id": 1, "name": "A"}, "contact": None})
        if not self._auth(): return
        if ep == "panel_tickets":
            rid = body["client_request_id"]
            if rid in ST["ids"]: return self._send(200, {"ticket_id": ST["ids"][rid], "replayed": True})
            ST["created"] += 1; tid = 100 + ST["created"]
            ST["tickets"][tid] = body["subject"]; ST["ids"][rid] = tid
            return self._send(201, {"ticket_id": tid, "replayed": False})
        if ep == "panel_ticket_chat": return self._send(201, {"ticket_id": 7, "message_id": 3})

srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
folder = tempfile.mkdtemp()
cfg = {"rivet_base_url": base, "enrollment_token": "rvtp1.good"}

p = PanelProvider(cfg, data_folder=folder)
cats = p.list_categories()                       # first call enrolls
assert cats[0].name == "Hard<ware", cats          # entity decoding
enroll = [r for r in ST["requests"] if r[1].endswith("panel_enroll")][0]
assert "Authorization" not in enroll[2] and "client_id" not in json.dumps(enroll[3])
assert oct(os.stat(os.path.join(folder, "device.json")).st_mode & 0o777) == "0o600"

# a new process (fresh provider) reuses the stored credential: the one-time token is already spent
p2 = PanelProvider(cfg, data_folder=folder); assert p2.list_categories()
assert ST["tokens_left"] == 0

# idempotent creation: same request_id twice -> one ticket
t1 = p2.create_ticket("Printer", "jam", request_id="r-1"); t2 = p2.create_ticket("Printer", "jam", request_id="r-1")
assert t1.id == t2.id and ST["created"] == 1
assert len(p2.list_tickets()) == 1
ch = p2.poll_chat(7, 0); assert [m.sender_type for m in ch] == ["agent", "contact"]
assert len(p2.poll_chat(7, 1)) == 1
p2.send_chat_message(7, "ok"); assert p2.get_ticket(7).status == "In Progress"

# attachments: base64 and extension allow-list from the server limits
t = p2.create_ticket("With file", "d", files=[("files[]", "shot.png", b"\x89PNG", "image/png")])
sent = [r for r in ST["requests"] if r[0] == "POST" and r[1].endswith("panel_tickets")][-1][3]
assert sent["attachments"][0]["data_base64"] and sent["attachments"][0]["filename"] == "shot.png"
try: p2.create_ticket("x", "y", files=[("files[]", "a.exe", b"MZ", "application/octet-stream")]); raise SystemExit("exe accepted")
except PanelError as e: assert e.code == "attachment_rejected"

# credential is bound to the server URL: another base URL must not receive it
other = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=other.serve_forever, daemon=True).start()
p3 = PanelProvider({"rivet_base_url": f"http://127.0.0.1:{other.server_port}"}, data_folder=folder)
assert not p3.enrolled()
try: p3.list_categories(); raise SystemExit("sent without enrollment")
except PanelError as e: assert e.code == "not_enrolled"

# revocation clears the local credential and surfaces the code
ST["revoked"] = True
try: p2.list_tickets(); raise SystemExit("revoked ignored")
except PanelError as e: assert e.code == "revoked"
assert credstore.load(folder, base) is None

# selector: legacy api_key installs keep the old provider
assert isinstance(make_provider({"rivet_base_url": base, "api_key": "k"}), RivetProvider)
assert isinstance(make_provider({"rivet_base_url": base, "enrollment_token": "t"}), PanelProvider)
assert isinstance(make_provider({"rivet_base_url": base, "api_key": "k", "enrollment_token": "t"}), PanelProvider)
print("panel provider tests ok")
