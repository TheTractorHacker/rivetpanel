# RivetPanel remediation

Every change below is covered by an automated regression test (see `PANEL_TEST_RESULTS.md`).
No security feature was disabled or loosened to make a test pass; two tests that initially did not detect a reverted
fix were strengthened instead (see test results).

## CI / supply chain (`.github/`)

| Change | Detail |
|---|---|
| Actions pinned to full commit SHAs | `actions/checkout` v4.4.0 `11d5960a...`, `actions/setup-python` v5.6.0 `a26af69b...`, `actions/upload-artifact` v4.6.2 `ea165f8d...`, `actions/download-artifact` v4.3.0 `d3f86a10...`, `softprops/action-gh-release` v2.6.2 `3bb12739...`. Each SHA was resolved from the upstream repo's release tag; the version stays in a trailing comment. |
| Maintainability | New `.github/dependabot.yml`: weekly grouped PRs for GitHub Actions (rewrites SHA and comment together) and pip for Windows/Linux/macOS. Pins stay reviewed *and* current. |
| Checkout credentials | `persist-credentials: false` on all four checkouts. |
| Permissions | Workflow default stays `contents: read`; `contents: write` only on the three build jobs (they publish to a release on tags) and `sign-release`. `permissions` scopes other than `contents` are rejected by test. |
| Dependency audit | New CI step: `pip-audit` on the pinned Windows file and on the resolved Linux environment. |
| Test job | Runs all eight suites with `RIVETPANEL_STRICT_DEPS=1` (installed versions must meet the floors). |

## Dependencies

Windows pinned, Linux/macOS floored at: Pillow 13.0.0, requests 2.34.2, cryptography 50.0.2, pyinstaller 6.22.3.
CI builds and tests on Python 3.12 with these versions (Pillow, Ed25519 verification, requests all exercised).

## File access and path safety

| Where | Fix |
|---|---|
| `updater_security.safe_asset_name` | Release asset names must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` and contain no `..`; used for every download destination. |
| `updater_security.read_tar_file` | Reads exactly one member into memory; rejects traversal, absolute paths, symlinks, hardlinks, devices and FIFOs anywhere in the archive; size capped. No `extract`/`extractall` remains. |
| `updater_security.check_zip` | macOS zip validated without extraction (no traversal, no symlinks, required bundle present, size capped). |
| `core._queue_file_path` | Queue attachments must be a plain, non-symlink, size-capped file directly inside their queue folder; names sanitised when written; queue folders are mode 0700. |
| `core.read_attachment` / `safe_image_path` (previous pass) | Regular file only, 25 MB cap, symlinks resolved, app config/data never attachable; logo paths restricted to small image files. |
| `core.load_config` | POSIX: refuse a config writable by group/others or owned by another user (root or current user only); require a JSON object; 64 KB cap. Windows relies on the installer's ProgramData ACL (Administrators/SYSTEM write). |
| `core.data_dir` | Created 0700 and re-asserted when owned by the current user. |
| `core.load_tracked_tickets` | Validates structure; only numeric ids accepted. |
| Legacy provider | Ticket ids coerced with `int()` before use in URLs. |

## Credential store (`credstore.py`)

Folder must be a real directory owned by the user with mode 0700 (symlinked or shared folders are refused); files are read
with `O_NOFOLLOW`, must be regular, owned by the user, mode 0600 and under 64 KB; writes go to a random temp name created
with `O_EXCL|O_NOFOLLOW`, are fsync'd and atomically renamed; `clear()` never removes a non-regular file; the record stays
bound to the server URL that issued it. Windows keeps DPAPI protection.

## Privileged update path

The signed archive is no longer trusted after verification. Linux: the binary is read from the verified tarball in memory,
hashed, and the update script re-hashes a root-owned copy of the file immediately before `install`; a swapped file is
refused and the old binary is left untouched. macOS: the signed zip's hash is re-verified on a root-owned copy and the
bundle is unpacked from that copy. `sudo -n` replaces `sudo` so a detached script can never hang on a password prompt.
Windows is unchanged (signed installer, verified before it runs).

## Credential handling and logging

`core.redact()` removes device credentials (`rvpd_...`), enrollment tokens (`rvtp1....`), `X-Api-Key`, `api_key=`,
`Authorization` and bearer values from every error shown to the user. Linux/macOS `install.sh` accept
`RIVETPANEL_ENROLLMENT_TOKEN` so the token need not appear in the process list; the config-permission step is now a shared
function used by both the token and the legacy branches.

## Docker

Nothing to remediate: no Dockerfile or compose file exists. `tests/test_container_security.py` fails the build if one is
ever added that runs as root, is privileged, uses host networking, keeps default capabilities, publishes ports on all
interfaces, bakes secrets into ENV/ARG, or uses an unpinned base image.

## Compatibility

Existing deployment methods are unchanged: legacy `api_key` installs still select the legacy provider; the Inno Setup
silent parameters, `deploy_rivetpanel.ps1` and `install.sh <url> <key> <client_id>` keep their interface (not re-run on real Windows/macOS systems, see test results); the RivetCore
Panel API contract is untouched. The one behavioural change users may notice: a config file that other users can write
is now refused with a clear message instead of being used.
