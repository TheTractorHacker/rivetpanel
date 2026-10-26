# RivetPanel test results

Environment: Ubuntu, Python 3.14 locally (Xvfb for tkinter) and Python 3.12 in CI. Local results from this machine;
CI results from GitHub Actions run `40332256884` on the commit that carries these documents' code changes
(Pillow 13.0.0, requests 2.34.2, cryptography 50.0.2 installed fresh; `RIVETPANEL_STRICT_DEPS=1`).
No credential values appear in any test output.

## Suites

| Suite | Covers | Local | CI |
|---|---|---|---|
| `test_provider.py` | legacy provider: ticket/category/chat calls, legacy config key fallback | pass | pass |
| `test_panel_provider.py` | enrollment, stored credential reuse, idempotent create, attachments, URL binding, revocation, provider selection | pass | pass |
| `test_workflow_security.py` | **GitHub workflow security** (below) | pass | pass |
| `test_container_security.py` | **container privileges** (below) | pass (0 Dockerfiles, 0 compose files) | pass |
| `test_dependencies.py` | **dependency compatibility** floors in all three requirement files, installed versions (strict in CI), APIs still present | pass | pass |
| `test_security.py` | release-URL allow-list, base-URL validation, attachments, no redirect following | pass | pass |
| `test_path_security.py` | **path traversal, unauthorized file access**, archives, update script, queue, config, credstore | pass | pass |
| `test_auth_credentials.py` | **authentication bypass, credential handling** | pass | pass |

CI also runs `pip-audit` on the pinned Windows requirements and on the resolved environment: **"No known
vulnerabilities found"** for both. The three platform build jobs (Windows, Linux, macOS) succeed on the SHA-pinned actions.

## What each security suite asserts

**Workflow security**: every `uses:` is a 40-hex commit SHA with a version comment; top-level `contents: read`; no
`write-all`; no permission scope other than `contents`; every checkout has `persist-credentials: false`; no
`pull_request_target`; no untrusted `github.event`/`head_ref` interpolation in `run:`; no `curl | bash`; secrets never
echoed; the signing job runs on tags only; Dependabot covers actions and pip.

**Path traversal / file access**: bad asset names (`../x`, absolute, backslash, NUL, empty, spaces, non-strings) are refused;
tar archives with traversal, absolute, symlink, hardlink, device, symlink-as-binary, missing-member and oversize cases are
refused and nothing is written outside; zips with traversal/absolute/symlink/missing-bundle refused; queue manifests with
`../`, absolute, nested, empty, backslash and symlink names refused; attachments cannot be the app's config or anything under
protected folders, symlinks to them, devices or directories; configs that are group/world-writable, non-object or oversized
are refused; logo paths outside small image files are ignored; credstore ignores loose-permission files, symlinks (including
to a valid, correctly-permissioned record), a symlinked or shared folder, and a record issued for another server; a planted
symlink at the temp name cannot redirect a write.

**Update script (executed for real, with `sudo` replaced by `env` so the test never escalates)**: a payload matching the
signed hash is installed; a tampered payload is refused and the old binary is left byte-for-byte untouched; shell
metacharacters in paths do not execute; non-hex hashes are rejected.

**Authentication / credentials**: with no enrollment and no credential, zero authenticated requests leave the machine;
enrollment carries no `Authorization` header and the token goes in the body; across enroll, list, get, chat read/write and
create, no request ever contains `client_id` or `contact_id` (even when the config tries to supply them) or the legacy key;
the credential is not in the config and the store is mode 0600; a configured enrollment token always beats a legacy key;
the bearer is never sent after a redirect or to another base URL; plain http to a non-local host, URLs with userinfo, query
strings or other schemes are refused before any request; a 401 drops the local credential and surfaces an auth error with no
retry loop; server error text containing a credential is redacted before display, and redaction covers bearer, `X-Api-Key`
and `api_key=`; a record is bound to its issuing server.

**Container privileges**: scans for Dockerfiles/compose files and fails on root user, `privileged`, host networking,
missing `cap_drop: ALL`, missing `no-new-privileges`, all-interface port publishing, secrets in ENV/ARG, unpinned base
images and `ADD <url>`. None exist today, so this suite passes vacuously and is a guard for the future.

## Are the tests meaningful? Mutation check

Each fix was reverted in a throwaway copy of the code and the relevant suite re-run. **13 of 13 mutations are caught**:
unpinned action, `persist-credentials: true`, `write-all`, credstore following symlinks, credstore without permission
check, queue manifest traversal, update script skipping the hash check, tar symlinks allowed, asset-name validation removed,
bearer followed across redirects, `client_id` added to requests, redaction removed, config permission check removed.
**Two mutations initially survived** (symlink-follow and client-id spoofing) because the tests were too weak; the tests
were strengthened (symlink to a valid record; spoofing checked on create/list/get/chat, not just enrollment) and both are
now caught.

## Not tested (be aware)

* **macOS update script** and the **Windows DPAPI** path: written but never executed (no macOS/Windows host here; CI only
  compiles/builds them).
* **Inno Setup installer** (`/EnrollmentToken`, picker-page skip): builds in CI, never run on Windows.
* **Linux/macOS `install.sh` token branch** and `lock_config`: shell syntax checked only (needs root and a real install).
* **Real RivetIT/RivetMSP server**: the Panel API client is tested against a spec-faithful mock; the edition-side
  adoption is not done yet.
* The release `sign-release` job (tag-only) has never run; the signing secret is not set yet.
* No dynamic/penetration test and no fuzzing were performed.
