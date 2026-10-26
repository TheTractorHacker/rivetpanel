"""Path traversal, unauthorized file access, archive and updater safety.
Run: xvfb-run -a python3 tests/test_path_security.py   (core imports tkinter)"""
import hashlib, io, json, os, stat, subprocess, sys, tarfile, tempfile, zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
import core, credstore, updater_security as usec  # noqa: E402

def raises(exc, fn, *a, **k):
    try: fn(*a, **k)
    except exc: return True
    except Exception as e: raise AssertionError(f"wrong exception {type(e).__name__}: {e}")
    raise AssertionError(f"{fn.__name__}{a} did not raise {exc.__name__}")

tmp = tempfile.mkdtemp(); os.chmod(tmp, 0o700)
secret = os.path.join(tmp, "secret.txt"); open(secret, "w").write("TOPSECRET")

# --- release asset names (path traversal on download) ---
for bad in ("../evil", "..\\evil", "/etc/passwd", "a/b", "x..y", "", ".", "..", "a\x00b", "name with space", None, 5):
    raises(usec.UnsafeArchive, usec.safe_asset_name, bad)
for good in ("RivetPanelSetup.exe", "rivetpanel-linux.tar.gz", "rivetpanel-macos.zip"):
    assert usec.safe_asset_name(good) == good

# --- tar: link/dev/traversal members refused, exact member read from memory ---
def make_tar(path, members):
    with tarfile.open(path, "w:gz") as t:
        for name, kind, data in members:
            ti = tarfile.TarInfo(name)
            if kind == "file": ti.size = len(data); t.addfile(ti, io.BytesIO(data))
            elif kind == "sym": ti.type = tarfile.SYMTYPE; ti.linkname = data; t.addfile(ti)
            elif kind == "hard": ti.type = tarfile.LNKTYPE; ti.linkname = data; t.addfile(ti)
            elif kind == "dev": ti.type = tarfile.CHRTYPE; t.addfile(ti)
good = os.path.join(tmp, "good.tar.gz"); make_tar(good, [("./RivetPanel", "file", b"BIN"), ("./install.sh", "file", b"#!/bin/sh")])
assert usec.read_tar_file(good, "RivetPanel") == b"BIN"
for label, members in {
    "traversal": [("../../etc/evil", "file", b"x"), ("./RivetPanel", "file", b"BIN")],
    "absolute": [("/etc/evil", "file", b"x"), ("./RivetPanel", "file", b"BIN")],
    "symlink": [("./link", "sym", "/etc/shadow"), ("./RivetPanel", "file", b"BIN")],
    "hardlink": [("./hl", "hard", "RivetPanel"), ("./RivetPanel", "file", b"BIN")],
    "device": [("./dev", "dev", b""), ("./RivetPanel", "file", b"BIN")],
    "symlink-as-binary": [("./RivetPanel", "sym", "/etc/shadow")],
    "missing": [("./other", "file", b"x")],
}.items():
    p = os.path.join(tmp, label + ".tar.gz"); make_tar(p, members)
    raises(usec.UnsafeArchive, usec.read_tar_file, p, "RivetPanel")
assert not os.path.exists("/etc/evil")
big = os.path.join(tmp, "big.tar.gz"); make_tar(big, [("./RivetPanel", "file", b"x" * 2048)])
raises(usec.UnsafeArchive, usec.read_tar_file, big, "RivetPanel", 1024)

# --- zip: traversal and symlink entries refused ---
def make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as z:
        for name, data, mode in entries:
            zi = zipfile.ZipInfo(name); zi.external_attr = (mode << 16); z.writestr(zi, data)
z1 = os.path.join(tmp, "ok.zip"); make_zip(z1, [("RivetPanel.app/Contents/MacOS/RivetPanel", b"b", 0o100755)])
usec.check_zip(z1, "RivetPanel.app")
for label, entries in {
    "traversal": [("../evil", b"x", 0o100644), ("RivetPanel.app/a", b"b", 0o100644)],
    "absolute": [("/evil", b"x", 0o100644), ("RivetPanel.app/a", b"b", 0o100644)],
    "symlink": [("RivetPanel.app/l", "/etc/passwd", 0o120777)],
    "missing": [("other/a", b"b", 0o100644)],
}.items():
    p = os.path.join(tmp, label + ".zip"); make_zip(p, entries)
    raises(usec.UnsafeArchive, usec.check_zip, p, "RivetPanel.app")

# --- update scripts: hash must be hex; values are shell-quoted ---
raises(ValueError, usec.linux_update_script, 1, "/a", "/b", "not-a-hash")
raises(ValueError, usec.macos_update_script, "/a", "/b", "$(rm -rf /)")
hexh = "a" * 64
evil = "/tmp/x'; touch /tmp/PWNED; '"
script = usec.linux_update_script(4242, evil, "/opt/r p/RivetPanel", hexh)
subprocess.run(["bash", "-n", "/dev/stdin"], input=script.encode(), check=True)
assert not os.path.exists("/tmp/PWNED")

# --- the Linux update script really installs only bytes that match the signed hash ---
def run_update(payload, expected):
    d = tempfile.mkdtemp(); src = os.path.join(d, "new"); dest = os.path.join(d, "RivetPanel")
    open(src, "wb").write(payload); open(dest, "wb").write(b"OLD")
    sh = usec.linux_update_script(2 ** 22 + os.getpid() * 0 + 99999, src, dest, expected)  # PID that does not exist
    sh = sh.replace('nohup "$DEST" >/dev/null 2>&1 &\ndisown\n', "")                       # do not launch
    sh = sh.replace('"sudo -n"', '"env"')                                                   # never escalate in a test
    r = subprocess.run(["bash", "-c", sh], capture_output=True, timeout=30)
    return open(dest, "rb").read(), r.returncode
ok_payload = b"#!/bin/sh\necho new\n"
data, rc = run_update(ok_payload, hashlib.sha256(ok_payload).hexdigest())
assert (data, rc) == (ok_payload, 0), (data, rc)
data, rc = run_update(b"tampered", hashlib.sha256(ok_payload).hexdigest())
assert data == b"OLD" and rc != 0, (data, rc)           # swapped file is refused, old binary untouched

# --- core: queue manifest cannot name files outside its folder ---
item = os.path.join(tmp, "queue_item"); os.makedirs(item, mode=0o700)
open(os.path.join(item, "ok.bin"), "wb").write(b"1")
assert core._queue_file_path(item, "ok.bin").endswith("ok.bin")
for bad in ("../secret.txt", secret, "..", "sub/ok.bin", "", "a\\b", None):
    raises(ValueError, core._queue_file_path, item, bad)
os.symlink(secret, os.path.join(item, "link.bin"))
raises(ValueError, core._queue_file_path, item, "link.bin")

# --- attachments: app config/data never attachable, symlinks resolved, devices refused ---
cfgdir = os.path.join(tmp, "etc"); os.makedirs(cfgdir, mode=0o755); cfg = os.path.join(cfgdir, "config.json"); open(cfg, "w").write("{}")
raises(ValueError, core.read_attachment, cfg, [cfg])
raises(ValueError, core.read_attachment, os.path.join(item, "link.bin"), [secret])
raises(ValueError, core.read_attachment, "/dev/zero")
raises(ValueError, core.read_attachment, tmp)
inside = os.path.join(cfgdir, "x.txt"); open(inside, "w").write("x")
raises(ValueError, core.read_attachment, inside, [cfgdir])   # anything under a protected dir

# --- config loading: refuse group/world-writable or foreign-owned configs ---
c = os.path.join(tmp, "config.json"); json.dump({"rivet_base_url": "https://x.example"}, open(c, "w")); os.chmod(c, 0o644)
assert core.load_config([c])["_source"] == c
os.chmod(c, 0o666); raises(PermissionError, core.load_config, [c])
os.chmod(c, 0o664); raises(PermissionError, core.load_config, [c])
os.chmod(c, 0o644); open(c, "w").write("[1,2]"); raises(ValueError, core.load_config, [c])
open(c, "w").write("x" * 70000); raises(ValueError, core.load_config, [c])

# --- logo path from config ---
png = os.path.join(tmp, "l.png"); open(png, "wb").write(b"x")
assert core.safe_image_path(png) and not core.safe_image_path(secret) and not core.safe_image_path("/etc/passwd")
assert core.safe_image_path(os.path.join(tmp, "nonexistent.png")) is None

# --- credstore: symlinks, wrong perms, wrong folder, atomicity ---
folder = os.path.join(tmp, "data"); rec = {"base_url": "https://x.example", "device_token": "rvpd_" + "a" * 64}
credstore.save(folder, rec)
assert credstore.load(folder, "https://x.example") == rec
assert stat.S_IMODE(os.stat(credstore._path(folder)).st_mode) == 0o600 and stat.S_IMODE(os.stat(folder).st_mode) == 0o700
assert credstore.load(folder, "https://other.example") is None                    # bound to the issuing server
os.chmod(credstore._path(folder), 0o644); assert credstore.load(folder, "https://x.example") is None   # loose perms: ignored
os.chmod(credstore._path(folder), 0o600)
os.remove(credstore._path(folder)); os.symlink(secret, credstore._path(folder))
assert credstore.load(folder, "https://x.example") is None                       # symlink not followed
# ...even when the target is a perfectly valid, correctly-permissioned record (only O_NOFOLLOW stops this)
good_rec = os.path.join(tmp, "planted.json"); open(good_rec, "w").write(json.dumps(rec)); os.chmod(good_rec, 0o600)
os.remove(credstore._path(folder)); os.symlink(good_rec, credstore._path(folder))
assert credstore.load(folder, "https://x.example") is None, "followed a symlink to a valid record"
idl = os.path.join(folder, "install_id"); planted_id = os.path.join(tmp, "planted_id"); open(planted_id, "w").write("11111111-2222-4333-8444-555555555555"); os.chmod(planted_id, 0o600)
if os.path.lexists(idl): os.remove(idl)
os.symlink(planted_id, idl); assert credstore.install_id(folder) != "11111111-2222-4333-8444-555555555555"; os.remove(idl)
credstore.clear(folder); assert os.path.islink(credstore._path(folder))          # clear() leaves non-regular files alone
os.remove(credstore._path(folder))
# a pre-planted symlink at a predictable tmp name does not redirect the write
victim = os.path.join(tmp, "victim"); open(victim, "w").write("KEEP")
os.symlink(victim, credstore._path(folder) + ".tmp")
credstore.save(folder, rec); assert open(victim).read() == "KEEP"
# symlinked or shared folder is refused
link = os.path.join(tmp, "linked"); os.symlink(folder, link)
raises(credstore.InsecureStore, credstore.save, link, rec); assert credstore.load(link, "https://x.example") is None
shared = os.path.join(tmp, "shared"); os.makedirs(shared, mode=0o777); os.chmod(shared, 0o777)
raises(credstore.InsecureStore, credstore.save, shared, rec)
# install id: stable, regular file only
i1 = credstore.install_id(folder); assert i1 == credstore.install_id(folder)
print("path security ok")
