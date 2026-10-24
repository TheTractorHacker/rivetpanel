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


def _path(folder: str) -> str:
    return os.path.join(folder, _FILE_WIN if sys.platform == "win32" else _FILE)


def load(folder: str, base_url: str):
    """Return the stored record for this server, or None."""
    try:
        with open(_path(folder), "rb") as f:
            raw = f.read()
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
    os.makedirs(folder, exist_ok=True)
    path = _path(folder)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    os.replace(tmp, path)


def clear(folder: str) -> None:
    try:
        os.remove(_path(folder))
    except OSError:
        pass


def install_id(folder: str) -> str:
    """Stable per-install UUID (re-enrolling with it keeps the device row)."""
    path = os.path.join(folder, "install_id")
    try:
        with open(path, "r", encoding="ascii") as f:
            val = f.read().strip()
        return str(uuid.UUID(val))
    except (OSError, ValueError):
        val = str(uuid.uuid4())
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="ascii") as f:
            f.write(val)
        return val
