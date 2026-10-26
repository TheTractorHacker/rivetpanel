"""Dependency floors and compatibility. Run: python3 tests/test_dependencies.py
Set RIVETPANEL_STRICT_DEPS=1 (CI does) to also require the INSTALLED versions to meet the floors."""
import importlib.metadata as md, os, re, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
FLOORS = {"pillow": (13, 0, 0), "requests": (2, 34, 2), "cryptography": (50, 0, 2), "pyinstaller": (6, 22, 3)}
def vt(v): return tuple(int(x) for x in re.findall(r"\d+", v)[:3])
fails = []
for plat in ("Windows", "Linux", "macOS"):
    reqs = {}
    for line in open(f"{ROOT}/{plat}/requirements.txt"):
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*(==|>=)?\s*([\d.]+)?", line.strip())
        if m and m.group(1) and not line.startswith("#"): reqs[m.group(1).lower()] = (m.group(2), m.group(3))
    for pkg, floor in FLOORS.items():
        op, ver = reqs.get(pkg, (None, None))
        if not ver or op not in ("==", ">=") or vt(ver) < floor:
            fails.append(f"{plat}/requirements.txt: {pkg} must be pinned/floored at >= {'.'.join(map(str, floor))} (found {op}{ver})")
if os.environ.get("RIVETPANEL_STRICT_DEPS") == "1":
    for pkg, floor in FLOORS.items():
        if pkg == "pyinstaller": continue
        try:
            if vt(md.version(pkg)) < floor: fails.append(f"installed {pkg} {md.version(pkg)} is below the floor")
        except md.PackageNotFoundError:
            fails.append(f"{pkg} not installed")
# compatibility: the APIs we rely on still exist in the installed versions
import requests, cryptography  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: E402
from PIL import Image, ImageDraw, ImageGrab  # noqa: E402
assert hasattr(Ed25519PublicKey, "from_public_bytes") and hasattr(requests, "request")
Image.new("RGBA", (4, 4)).resize((2, 2)); ImageDraw.Draw(Image.new("RGB", (2, 2)))
if fails:
    print("\n".join("FAIL " + x for x in fails)); sys.exit(1)
print("dependency floors and compatibility ok")
