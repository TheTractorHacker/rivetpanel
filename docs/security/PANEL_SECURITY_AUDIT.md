# RivetPanel security audit

Scope: the `TheTractorHacker/rivetpanel` repository at commit `0aede10` (before this hardening pass). Method: manual
review of every file that touches paths, processes, the network, secrets or CI, followed by test-driven confirmation.
No credentials appear in this document or in the test output.

## Reported findings: status

| # | Reported finding | Verdict | Where |
|---|---|---|---|
| 1 | GitHub Actions third-party dependencies not pinned (High) | **Confirmed**, 5 actions referenced by mutable tag | `build.yml` |
| 2 | Python file inclusion / arbitrary file read (Medium) | **Confirmed** in 4 places, see F2-F5 | `core.py`, `credstore.py` |
| 3 | Checkout credential persistence (Low) | **Confirmed**, 4 checkouts persisted the token | `build.yml` |
| 4 | Docker security | **Not applicable**: the repository contains no Dockerfile, compose file or container deployment | n/a |

The report also named `core-conformance.yml` and "one/two additional workflows". **Those files do not exist in this
repository** (`.github/workflows/` contains only `build.yml`). The sibling repository `rivet-core` has `ci.yml`,
`endpoint-agent.yml` and `release.yml`; it has no Dockerfile either. It was outside this task and was **not** audited or
changed. If the scanner's findings 1 and 3 were produced against rivet-core (or another repo), those workflows still
need the same treatment.

## Detailed findings

### F1 Unpinned GitHub Actions (High) - fixed
`actions/checkout@v4`, `actions/setup-python@v5`, `actions/upload-artifact@v4`, `actions/download-artifact@v4` and
`softprops/action-gh-release@v2` were referenced by movable tags. A compromised tag would run attacker code in a job that
holds `contents: write` and (in `sign-release`) the release-signing key. The release-signing key is the trust root of the
self-update mechanism, so this was the highest-impact finding.

### F2 Release asset name used as a path (Medium) - fixed
`do_self_update` built the download path with `os.path.join(tmp_dir, asset["name"])` where `name` comes from the GitHub
release JSON. A name such as `../../x` wrote outside the temp directory *before* signature verification.

### F3 Weak archive extraction (Medium) - fixed
Archives were extracted with a `".." not in name` substring filter. That neither stops symlink/hardlink/device members
nor absolute-path tricks specific to each format, and it wrongly rejects legitimate names. (The archives are
Ed25519-signed, so this was defense in depth, but a signing-key or release compromise would have turned it into an
arbitrary file write.)

### F4 Offline-queue manifest could name any file (Medium) - fixed
`flush_queue` opened `os.path.join(item_dir, entry["stored_as"])` using a value read from `files.json` on disk. An
absolute path or `../` there made the app read an arbitrary file and upload it to the server as a ticket attachment.
Queue folders and filenames were also created with default permissions.

### F5 Credential store followed symlinks and trusted any file (Medium) - fixed
`credstore.py` opened and wrote files without `O_NOFOLLOW`, used a predictable `.tmp` name, created the folder with default
permissions and never checked ownership or mode. A planted symlink could redirect the credential write or substitute the
record that gets loaded. The same applied to the install-id file.

### F6 Checkout credential persistence (Low) - fixed
`actions/checkout` left the job token in `.git/config` for the whole job. No step runs authenticated git afterwards.

### F7 Privileged update copy had a time-of-check/time-of-use gap (Medium, found during review) - fixed
On Linux/macOS the updater verified the signature, left the file in a user-writable temp folder, then ran
`sudo cp` from it later. A same-user process could swap the file in between and obtain root. Fixed by re-hashing the
exact bytes inside the privileged process, from a root-owned copy.

### F8 Config file trust (Low-Medium, found during review) - fixed
`load_config` accepted a config that group/others could write, or one owned by another user. The config decides which
server receives the enrollment token and tickets.

### F9 Credential-shaped text could reach dialogs (Low, found during review) - fixed
Exception text was shown to users verbatim. Added redaction of device credentials, enrollment tokens, `X-Api-Key` and
bearer values.

### F10 Smaller items - fixed
Tracked-ticket file was trusted without validation; legacy provider built URLs from an un-coerced ticket id; the
checksum comparison was not constant-time; the data folder was created world-listable; the enrollment token could only
be passed on the command line (visible in `ps`).

## Areas reviewed with no change needed

| Area | Result |
|---|---|
| Command execution | Every `subprocess` call uses an argument list; no `shell=True`, `os.system`, `eval`, `exec` or `pickle`. The two generated update scripts quote every value with `shlex.quote` and require a hex SHA-256. |
| Release authenticity | Ed25519 signature over `checksums.txt`, then SHA-256 per asset, before anything runs (pre-existing, sound). Update URLs are limited to this repo's GitHub release path and GitHub asset hosts, redirects re-validated (previous pass). |
| Panel-to-core authentication | Per-device bearer credential, scoped server-side; client/contact never sent; enrollment sends no `Authorization`; bearer never sent on redirect or to a different base URL; https required except localhost. |
| Session/API token handling | No session cookies. Legacy `X-Api-Key` is only used by the legacy provider and is never sent by the panel provider. |
| Logging | The app writes no log files and has no `print`/`logging` of request data. |
| Docker / networking | No containers, no listening sockets: the app is a client only. |
| CI artifact integrity | Release assets are signed in a separate tag-only job; checksums cover the Windows exe and installer, Linux tarball and macOS zip. |
| Dependencies | All four flagged packages updated; `pip-audit` reports no known vulnerabilities (CI). |

## Residual risks (not fixed, by design or out of scope)

1. **Requirements are not hash-locked.** Windows pins exact versions; Linux/macOS use `>=` floors, so builds float to the
   newest release. `pip-audit` runs on every push, but a malicious new release of a dependency would be picked up until
   caught. Mitigation to consider: lock files with `--require-hashes`.
2. **No build provenance.** No SLSA/Sigstore attestation; PyInstaller output is not reproducible. The signing key is a
   single GitHub secret.
3. **Same-user compromise.** The device credential is protected from other users, not from malware running as the same
   user (DPAPI user scope; 0600 file). It is not stored in the OS keychain.
4. **Enrollment token exposure.** On Windows the installer takes `/EnrollmentToken=...` as a command-line argument,
   visible to other users for a moment. Mitigated by short-lived, capped, revocable tokens; Linux/macOS now support an
   environment variable.
5. **Legacy shared API key** (admin-equivalent) still works for installs that have not redeployed. Retire it once
   every PC has enrolled.
6. **TLS trust** is the OS store; no certificate pinning.
7. **`sudo -n` updater** depends on cached sudo credentials; where absent the update fails closed (old binary kept).
8. **Not exercised on real systems:** the macOS update script, Windows DPAPI code, the Inno Setup installer changes and the
   Linux/macOS `install.sh` token branch (see test results).
9. `pip install --upgrade pip` in CI is unpinned.
