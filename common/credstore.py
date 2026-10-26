"""
Per-device credential storage for the RivetPanel device-token API.

The credential (`rvpd_...`) is a secret, so it is kept in the user's own data
folder, never in the shared config.json: Windows protects it with DPAPI (bound
to the user account), other systems use a 0600 file. The record is bound to the
server URL it was issued by - a record for another URL is ignored, so a
credential can never be sent to a host that did not issue it.
"""

import json
import os
import secrets
import stat
import sys
import uuid

_FILE = "device.json"
_FILE_WIN = "device.bin"


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


_MAX_BYTES = 64 * 1024
_POSIX = os.name == "posix"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class InsecureStore(OSError):
    """The credential folder or file has unsafe ownership, permissions or type."""


def _path(folder: str) -> str:
    return os.path.join(folder, _FILE_WIN if sys.platform == "win32" else _FILE)


def _check_dir(folder: str) -> None:
    """The folder must be a real directory owned by us and closed to others
    (a symlinked or shared folder could redirect or expose the credential)."""
    if not _POSIX:
        return
    st = os.lstat(folder)
    if not stat.S_ISDIR(st.st_mode):
        raise InsecureStore("credential folder is not a plain directory")
    if st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise InsecureStore("credential folder must be owned by the current user with mode 0700")


def _ensure_dir(folder: str) -> None:
    os.makedirs(folder, mode=0o700, exist_ok=True)
    _check_dir(folder)


def _read_private(path: str) -> bytes:
    """Read a small regular file we own, without following symlinks."""
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_BYTES:
            raise InsecureStore("credential file is not a plain small file")
        if _POSIX and (st.st_uid != os.geteuid() or st.st_mode & 0o077):
            raise InsecureStore("credential file must be owned by the current user with mode 0600")
        with os.fdopen(fd, "rb") as f:
            fd = -1
            return f.read(_MAX_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)


def _write_private(path: str, raw: bytes) -> None:
    """Atomic write: random temp name created with O_EXCL|O_NOFOLLOW, 0600, fsync, rename."""
    tmp = f"{path}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load(folder: str, base_url: str):
    """Return the stored record for this server, or None."""
    try:
        _check_dir(folder)
        raw = _read_private(_path(folder))
        if sys.platform == "win32":
            raw = _dpapi(raw, protect=False)
        rec = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("base_url") != base_url or not rec.get("device_token"):
        return None
    return rec


def save(folder: str, record: dict) -> None:
    raw = json.dumps(record).encode("utf-8")
    if sys.platform == "win32":
        raw = _dpapi(raw, protect=True)
    _ensure_dir(folder)
    _write_private(_path(folder), raw)


def clear(folder: str) -> None:
    path = _path(folder)
    try:
        if stat.S_ISREG(os.lstat(path).st_mode):
            os.remove(path)
    except OSError:
        pass


def install_id(folder: str) -> str:
    """Stable per-install UUID (re-enrolling with it keeps the device row)."""
    path = os.path.join(folder, "install_id")
    try:
        _check_dir(folder)
        return str(uuid.UUID(_read_private(path).decode("ascii").strip()))
    except (OSError, ValueError):
        val = str(uuid.uuid4())
        _ensure_dir(folder)
        try:
            _write_private(path, val.encode("ascii"))
        except OSError:
            pass
        return val
