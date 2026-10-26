"""Container/deployment privilege checks. RivetPanel itself ships no Docker image today; this
guards the day one is added. Run: python3 tests/test_container_security.py"""
import glob, os, re, sys
import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
skip = {".git", "node_modules", "build", "dist"}
dockerfiles, composes = [], []
for d, dirs, names in os.walk(ROOT):
    dirs[:] = [x for x in dirs if x not in skip]
    for n in names:
        low = n.lower()
        if low == "dockerfile" or low.startswith("dockerfile.") or low.endswith(".dockerfile"):
            dockerfiles.append(os.path.join(d, n))
        if re.match(r"(docker-)?compose.*\.ya?ml$", low):
            composes.append(os.path.join(d, n))
fails = []
for f in dockerfiles:
    t = open(f).read()
    users = re.findall(r"^\s*USER\s+(\S+)", t, re.M)
    if not users or users[-1] in ("root", "0"): fails.append(f"{f}: must end with a non-root USER")
    if re.search(r"^\s*(ENV|ARG)\s+\S*(PASS|SECRET|TOKEN|KEY)\S*\s*=?\s*\S+", t, re.M | re.I): fails.append(f"{f}: secret in ENV/ARG (leaks into image layers)")
    if re.search(r"^\s*FROM\s+\S+(:latest)?\s*$", t, re.M) and not re.search(r"@sha256:", t): fails.append(f"{f}: base image not pinned by digest")
    if re.search(r"ADD\s+https?://", t): fails.append(f"{f}: ADD from URL")
for f in composes:
    c = yaml.safe_load(open(f)) or {}
    for name, svc in (c.get("services") or {}).items():
        if svc.get("privileged"): fails.append(f"{f}:{name}: privileged")
        if "ALL" not in [x.upper() for x in svc.get("cap_drop") or []]: fails.append(f"{f}:{name}: must cap_drop ALL")
        if svc.get("network_mode") == "host": fails.append(f"{f}:{name}: host networking")
        if not svc.get("user"): fails.append(f"{f}:{name}: no non-root user")
        if "no-new-privileges:true" not in [str(x).replace(" ", "") for x in svc.get("security_opt") or []]: fails.append(f"{f}:{name}: no-new-privileges missing")
        for p in svc.get("ports") or []:
            if not str(p).startswith("127.0.0.1:"): fails.append(f"{f}:{name}: port {p} published on all interfaces")
if fails:
    print("\n".join("FAIL " + x for x in fails)); sys.exit(1)
print(f"container security ok ({len(dockerfiles)} Dockerfile(s), {len(composes)} compose file(s) checked)")
