"""GitHub workflow security checks. Run: python3 tests/test_workflow_security.py"""
import glob, os, re, sys
import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SHA = re.compile(r"^[0-9a-f]{40}$")
files = sorted(glob.glob(f"{ROOT}/.github/workflows/*.y*ml"))
assert files, "no workflows found"
fails = []

def check(cond, msg):
    if not cond: fails.append(msg)

def steps(wf):
    for jname, job in (wf.get("jobs") or {}).items():
        for st in job.get("steps") or []:
            yield jname, job, st

for f in files:
    name = os.path.basename(f)
    text = open(f).read()
    wf = yaml.safe_load(text)
    on = wf.get("on", wf.get(True)) or {}
    # 1. every third-party action is pinned to a full commit SHA (with a version comment)
    for line in text.splitlines():
        m = re.search(r"uses:\s*([^\s#]+)(\s*#\s*(\S+))?", line)
        if not m or m.group(1).startswith("./"): continue
        ref = m.group(1).split("@", 1)
        check(len(ref) == 2 and SHA.match(ref[1]), f"{name}: action not pinned to a commit SHA: {m.group(1)}")
        check(m.group(3) and m.group(3).startswith("v"), f"{name}: pinned action lacks a version comment: {m.group(1)}")
    # 2. least privilege: read-only default; no write-all; write only at job level
    perms = wf.get("permissions")
    check(isinstance(perms, dict) and perms.get("contents") == "read", f"{name}: top-level permissions must be contents: read")
    check("write-all" not in text, f"{name}: write-all permissions")
    for jname, job in (wf.get("jobs") or {}).items():
        jp = job.get("permissions") or {}
        for k, v in jp.items():
            check(v in ("read", "write", "none"), f"{name}/{jname}: odd permission {k}: {v}")
            check(k in ("contents",), f"{name}/{jname}: unexpected permission scope {k}")
    # 3. checkout never persists credentials
    for jname, job, st in steps(wf):
        if str(st.get("uses", "")).startswith("actions/checkout@"):
            check((st.get("with") or {}).get("persist-credentials") is False, f"{name}/{jname}: checkout without persist-credentials: false")
    # 4. no dangerous triggers / script injection
    check("pull_request_target" not in (on if isinstance(on, dict) else {}), f"{name}: pull_request_target trigger")
    for jname, job, st in steps(wf):
        run = st.get("run") or ""
        check(not re.search(r"\$\{\{\s*github\.(event|head_ref)", run), f"{name}/{jname}: untrusted github context interpolated in run:")
        check("curl" not in run or "|" not in run.split("curl", 1)[1].split("\n", 1)[0] or "bash" not in run, f"{name}/{jname}: curl | bash")
    # 5. secrets only reach the steps that need them, never echoed
    for jname, job, st in steps(wf):
        run = st.get("run") or ""
        check(not re.search(r"echo[^\n]*secrets\.", run), f"{name}/{jname}: secret echoed")
    # 6. release signing job must not run on pull requests
    for jname, job in (wf.get("jobs") or {}).items():
        if "sign" in jname:
            check("refs/tags/v" in str(job.get("if", "")), f"{name}/{jname}: signing must be tag-only")

# Dependabot keeps the pins maintainable
dep = yaml.safe_load(open(f"{ROOT}/.github/dependabot.yml"))
eco = {u["package-ecosystem"] for u in dep["updates"]}
check({"github-actions", "pip"} <= eco, "dependabot must cover github-actions and pip")

if fails:
    print("\n".join("FAIL " + x for x in fails)); sys.exit(1)
print(f"workflow security ok ({len(files)} workflow file(s))")
