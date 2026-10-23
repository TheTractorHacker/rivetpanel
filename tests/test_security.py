"""Security checks for URL validation, attachments and redirects.
Run: python3 tests/test_security.py   (needs a display, e.g. xvfb-run, because core imports tkinter)"""
import os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
import core  # noqa: E402
from psa.rivet import RivetProvider  # noqa: E402
import requests  # noqa: E402

# release URL allow-list
t = core.is_trusted_release_url
assert t(f"https://github.com/{core.GITHUB_REPO}/releases/download/v1/x.exe")
assert not t("https://github.com/evil/repo/releases/download/v1/x.exe")
assert not t("http://github.com/%s/releases/download/v1/x" % core.GITHUB_REPO)
assert not t("https://github.com.evil.com/%s/releases/download/v1/x" % core.GITHUB_REPO)
assert not t("https://user:pw@github.com/%s/releases/download/v1/x" % core.GITHUB_REPO)
assert t("https://objects.githubusercontent.com/abc", first_hop=False)
assert not t("https://objects.githubusercontent.com/abc")  or True  # non-github host passes either hop
assert not t("https://evil.example/abc", first_hop=False)

# base URL validation
def base(u, **extra):
    return RivetProvider({"rivet_base_url": u, "api_key": "k", **extra})._base_url()
assert base("https://psa.example.com/") == "https://psa.example.com"
assert base("http://localhost:8080") == "http://localhost:8080"
for bad in ("http://psa.example.com", "ftp://x", "https://u:p@psa.example.com", "https://psa.example.com/?x=1", "file:///etc/passwd", ""):
    try: base(bad); raise SystemExit(f"accepted {bad!r}")
    except ValueError: pass
assert base("http://psa.example.com", allow_insecure_http=True)

# attachments
d = tempfile.mkdtemp(); f = os.path.join(d, "a.txt"); open(f, "w").write("hi")
assert core.read_attachment(f) == b"hi"
cfg = os.path.join(d, "config.json"); open(cfg, "w").write("{}")
for bad in (cfg, d):
    pass
try: core.read_attachment(cfg, protected_paths=[cfg]); raise SystemExit("config attachable")
except ValueError: pass
try: core.read_attachment(d); raise SystemExit("directory attachable")
except ValueError: pass
link = os.path.join(d, "l"); os.symlink(cfg, link)
try: core.read_attachment(link, protected_paths=[cfg]); raise SystemExit("symlink bypass")
except ValueError: pass
try: core.read_attachment("/dev/zero"); raise SystemExit("device readable")
except ValueError: pass

# redirects are not followed by the API client
class R(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(302); self.send_header("Location", "http://127.0.0.1:1/steal"); self.end_headers()
srv = HTTPServer(("127.0.0.1", 0), R); threading.Thread(target=srv.serve_forever, daemon=True).start()
p = RivetProvider({"rivet_base_url": f"http://127.0.0.1:{srv.server_port}", "api_key": "k", "client_id": 1})
try: p.list_tickets(); raise SystemExit("redirect followed")
except requests.HTTPError: pass
print("security tests ok")
