"""
Security helpers for the self-update path (kept separate from core.py so they
can be tested without a display).

The release is Ed25519-signed (core._verify_release_asset). These helpers make
sure nothing between "verified" and "executed" can be swapped or can write
outside the update directory:

* asset names from the release JSON are never used as paths verbatim
* archives are never extracted with `extractall`-style helpers: the one file we
  need is read into memory (tar) or listed and checked (zip), links/devices are
  refused
* the privileged copy in the Linux/macOS update scripts re-verifies the SHA-256
  of the bytes it installs *inside the privileged process*, from a root-owned
  copy, so a same-user process cannot swap the file after verification
"""

import os
import posixpath
import re
import shlex
import tarfile
import zipfile

MAX_BINARY_BYTES = 400 * 1024 * 1024
_ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UnsafeArchive(Exception):
    pass


def safe_asset_name(name):
    """A release asset name usable as a single path component, or raise."""
    if (not isinstance(name, str) or not _ASSET_NAME_RE.match(name)
            or name in (".", "..") or ".." in name):
        raise UnsafeArchive(f"Unsafe release asset name: {name!r}")
    return name


def _clean_member(name):
    norm = posixpath.normpath(name.replace("\\", "/"))
    if norm.startswith("/") or norm == ".." or norm.startswith("../") or "\x00" in name:
        raise UnsafeArchive(f"Unsafe archive path: {name!r}")
    return norm


def read_tar_file(archive_path, member_name, max_bytes=MAX_BINARY_BYTES):
    """Return the bytes of exactly one regular file in a .tar.gz.

    Links, devices and anything outside the archive root are refused for the
    whole archive, not just for the member we want.
    """
    wanted = _clean_member(member_name)
    data = None
    with tarfile.open(archive_path, "r:gz") as t:
        for m in t.getmembers():
            norm = _clean_member(m.name)
            if m.issym() or m.islnk() or m.isdev() or m.isfifo():
                raise UnsafeArchive(f"Archive contains a link or special file: {m.name!r}")
            if norm == wanted:
                if not m.isreg() or m.size > max_bytes:
                    raise UnsafeArchive(f"{member_name} is not a regular file of a sane size")
                f = t.extractfile(m)
                data = f.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise UnsafeArchive(f"{member_name} is too large")
    if data is None:
        raise UnsafeArchive(f"{member_name} not found in update package")
    return data


def check_zip(archive_path, required_prefix, max_total=2 * 1024 * 1024 * 1024):
    """Validate a macOS update zip without extracting it: every entry stays
    inside the archive root, none is a symlink, and `required_prefix` exists."""
    found = False
    total = 0
    with zipfile.ZipFile(archive_path) as z:
        for info in z.infolist():
            norm = _clean_member(info.filename)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise UnsafeArchive(f"Archive contains a symlink: {info.filename!r}")
            total += info.file_size
            if total > max_total:
                raise UnsafeArchive("Update package is unreasonably large")
            if norm == required_prefix or norm.startswith(required_prefix + "/"):
                found = True
    if not found:
        raise UnsafeArchive(f"{required_prefix} not found in update package")


def _check_hex(digest):
    if not isinstance(digest, str) or not _SHA256_RE.match(digest):
        raise ValueError("expected a lowercase hex SHA-256")
    return digest


def linux_update_script(pid, new_binary, current_binary, sha256_hex):
    """Detached update script. The privileged install re-verifies the hash of
    the exact bytes it installs, from a root-owned copy (no TOCTOU)."""
    q = shlex.quote
    _check_hex(sha256_hex)
    return f"""#!/bin/bash
set -u
PID={int(pid)}
SRC={q(new_binary)}
DEST={q(current_binary)}
EXPECTED={sha256_hex}
while kill -0 "$PID" 2>/dev/null; do sleep 0.5; done

# $1 is empty (run as this user) or "sudo -n" (cached credentials, never prompts).
install_verified() {{
    local tmp rc=1
    tmp=$($1 mktemp) || return 1
    if $1 sh -c 'cat "$1" > "$2"' _ "$SRC" "$tmp" \\
       && [ "$($1 sha256sum "$tmp" | cut -d' ' -f1)" = "$EXPECTED" ]; then
        $1 install -m 755 "$tmp" "$DEST"; rc=$?
    fi
    $1 rm -f "$tmp"
    return $rc
}}

install_verified "" 2>/dev/null || install_verified "sudo -n" || exit 1
nohup "$DEST" >/dev/null 2>&1 &
disown
"""


def macos_update_script(zip_path, app_bundle, sha256_hex):
    """Same idea for the .app bundle: the signed zip's hash is re-checked on a
    root-owned copy and the bundle is unpacked from that copy."""
    q = shlex.quote
    _check_hex(sha256_hex)
    return f"""#!/bin/bash
set -u
ZIP={q(zip_path)}
BUNDLE={q(app_bundle)}
EXPECTED={sha256_hex}
sleep 2

# $1 is empty (run as this user) or "sudo -n".
apply_update() {{
    local tz td rc=1
    tz=$($1 mktemp) || return 1
    td=$($1 mktemp -d) || {{ $1 rm -f "$tz"; return 1; }}
    if $1 sh -c 'cat "$1" > "$2"' _ "$ZIP" "$tz" \\
       && [ "$($1 shasum -a 256 "$tz" | cut -d' ' -f1)" = "$EXPECTED" ] \\
       && $1 ditto -x -k "$tz" "$td" && [ -d "$td/RivetPanel.app" ]; then
        $1 rm -rf "$BUNDLE" && $1 ditto "$td/RivetPanel.app" "$BUNDLE"; rc=$?
    fi
    $1 rm -rf "$tz" "$td"
    return $rc
}}

apply_update "" 2>/dev/null || apply_update "sudo -n" || exit 1
open -a "$BUNDLE"
"""
