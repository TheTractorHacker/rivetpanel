"""Authentication, token handling and credential-leak checks for the Panel-to-Core API client.
Run: python3 tests/test_auth_credentials.py"""
import json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
import credstore  # noqa: E402
from psa.panel import PanelProvider, PanelError  # noqa: E402
from psa.rivet_select import make_provider  # noqa: E402
from psa.rivet import RivetProvider  # noqa: E402

CRED = "rvpd_" + "b" * 64
ENROLL = "rvtp1." + "c" * 12 + "." + "d" * 40
seen = []
mode = {"status": 200, "redirect": None, "leak": False}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _go(self):
        n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n).decode() if n else ""
        seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "key": self.headers.get("X-Api-Key"), "body": body})
        if mode["redirect"]:
            self.send_response(302); self.send_header("Location", mode["redirect"]); self.end_headers(); return
        ep = self.path.split("?")[0].rsplit("/", 1)[-1]
        if ep == "panel_enroll":
            out, code = {"device_id": 1, "device_token": CRED, "can_create_tickets": True, "limits": {}}, 201
        elif self.headers.get("Authorization") != "Bearer " + CRED:
            out, code = {"error": "nope", "code": "invalid_token"}, 401
        elif mode["status"] != 200:
            out, code = {"error": f"boom {CRED} {ENROLL}" if mode["leak"] else "boom", "code": "internal"}, mode["status"]
        else:
            out, code = {"categories": [], "tickets": [], "messages": []}, 200
        d = json.dumps(out).encode()
        self.send_response(code); self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)
    do_GET = do_POST = _go

def serve():
    s = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_port}"
srv, base = serve(); other, other_base = serve()
def folder(): d = tempfile.mkdtemp(); os.chmod(d, 0o700); return d
def raises(exc, fn, *a, **k):
    try: fn(*a, **k)
    except exc as e: return e
    raise AssertionError(f"{fn.__name__} did not raise {exc.__name__}")

# 1. no credential, no enrollment token -> no authenticated request ever leaves the machine
seen.clear(); p = PanelProvider({"rivet_base_url": base}, data_folder=folder())
e = raises(PanelError, p.list_tickets); assert e.code == "not_enrolled" and not seen

# 2. enrollment sends the token in the body and NO Authorization header; identity is never client-supplied
seen.clear(); f = folder()
cfg = {"rivet_base_url": base, "enrollment_token": ENROLL, "client_id": 999, "contact_id": 777, "api_key": "LEGACY"}
p = PanelProvider(cfg, data_folder=f); p.list_categories()
p.list_tickets(); p.get_ticket(5); p.poll_chat(5, 0); p.send_chat_message(5, "hi")
p.create_ticket("s", "d", category_id=3, request_id="r", files=[("files[]", "a.png", b"x", "image/png")])
enroll = seen[0]; assert enroll["auth"] is None and ENROLL in enroll["body"] and enroll["key"] is None
for req in seen:
    blob = req["path"] + req["body"]
    assert "client_id" not in blob and "contact_id" not in blob, blob       # spoofing impossible by construction
    assert req["key"] is None                                                # legacy key never sent by the panel provider
assert seen[1]["auth"] == "Bearer " + CRED

# 3. the credential is never written to config/log-like places and lives only in the 0600 store
assert CRED not in json.dumps(cfg) and CRED not in repr(p.__dict__.get("config")) and ENROLL in repr(cfg)
rec_file = open(credstore._path(f), "rb").read().decode()
assert CRED in rec_file and oct(os.stat(credstore._path(f)).st_mode & 0o777) == "0o600"

# 4. provider selection can't be tricked into sending the legacy key when a token is configured
assert isinstance(make_provider({"rivet_base_url": base, "enrollment_token": ENROLL, "api_key": "K"}), PanelProvider)
assert isinstance(make_provider({"rivet_base_url": base, "api_key": "K"}), RivetProvider)

# 5. the bearer token is never sent to another host: not on redirect, not after a base_url change
seen.clear(); mode["redirect"] = other_base + "/steal"
e = raises(Exception, PanelProvider({"rivet_base_url": base}, data_folder=f).list_tickets)
assert not any(r["path"].endswith("/steal") for r in seen) and "redirect" in str(e).lower()
mode["redirect"] = None
seen.clear(); p2 = PanelProvider({"rivet_base_url": other_base}, data_folder=f)
e = raises(PanelError, p2.list_tickets); assert e.code == "not_enrolled" and not seen   # stored cred belongs to `base`

# 6. plain http to a non-local host is refused before any request (no token over cleartext)
for url in ("http://example.invalid", "https://u:p@example.invalid", "https://example.invalid/?a=1", "ftp://example.invalid"):
    raises(ValueError, PanelProvider({"rivet_base_url": url, "enrollment_token": ENROLL}, data_folder=folder()).list_tickets)

# 7. a rejected/revoked credential is dropped locally and surfaced as an auth error (no silent retry loop)
seen.clear(); f2 = folder(); credstore.save(f2, {"base_url": base, "device_token": "rvpd_" + "e" * 64})
p3 = PanelProvider({"rivet_base_url": base}, data_folder=f2)
e = raises(PanelError, p3.list_tickets); assert e.code == "invalid_token" and e.status == 401
assert credstore.load(f2, base) is None and len(seen) == 1

# 8. server error text can never put credentials in front of the user or a log
import core  # noqa: E402  (needs a display; run under xvfb-run)
mode["status"] = 500; mode["leak"] = True
f3 = folder(); p4 = PanelProvider({"rivet_base_url": base, "enrollment_token": ENROLL}, data_folder=f3)
try: p4.list_tickets()
except Exception as exc: shown = core.redact(exc)
else: shown = ""
for secret in (CRED, ENROLL): assert secret not in shown
assert "[redacted]" in core.redact(f"Authorization: Bearer {CRED} and X-Api-Key: abc123 token {ENROLL}")
assert "abc123" not in core.redact("X-Api-Key: abc123") and "supersecret" not in core.redact("api_key=supersecret")

# 9. device credential must be bound to the base URL it was issued by (record tampering)
f4 = folder(); credstore.save(f4, {"base_url": "https://good.example", "device_token": CRED})
assert credstore.load(f4, "https://evil.example") is None
print("auth and credential handling ok")
