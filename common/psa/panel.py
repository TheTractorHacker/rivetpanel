"""
RivetPanel device-token provider.

Talks to the RivetCore Panel API (docs/panel/PANEL_API.md in rivet-core) of
RivetIT / RivetMSP. The machine enrolls once with a short-lived enrollment
token and from then on authenticates with its own per-device credential, which
the server scopes to one client (and optionally one contact). Nothing in the
request names a client or contact: the server reads identity from the
credential only.
"""

import base64
import getpass
import mimetypes
import os
import platform
import socket
import uuid

import requests

import credstore
from .base import Category, ChatMessage, ChatUnavailable, PSAProvider, Ticket
from .rivet import RivetProvider

APP_VERSION_DEFAULT = "0"


class PanelError(Exception):
    """An API error with the server's machine-readable `code`."""

    def __init__(self, status, code, message, retry_after=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


class PanelNotEnrolled(PanelError):
    pass


def _text(value):
    """The API returns plain text with only < and > entity-encoded."""
    return (value if isinstance(value, str) else "").replace("&lt;", "<").replace("&gt;", ">")


class PanelProvider(PSAProvider):
    name = "rivet"
    supports_chat = True
    supports_stream_chat = False   # the panel API is polled with `after`
    supports_categories = True

    def __init__(self, config: dict, data_folder: str = None, app_version: str = APP_VERSION_DEFAULT):
        super().__init__(config)
        self._folder = data_folder
        self._app_version = app_version
        self._record = None

    # ── configuration / identity ─────────────────────────────────────────

    def _base_url(self):
        # Same validation as the legacy provider (https, no credentials, ...).
        return RivetProvider(self.config)._base_url()

    def _data_folder(self):
        if self._folder:
            return self._folder
        import core  # late import: core imports this package
        return core.data_dir()

    def _load(self):
        if self._record is None:
            self._record = credstore.load(self._data_folder(), self._base_url())
        return self._record

    def enrolled(self):
        return self._load() is not None

    def can_create_tickets(self):
        rec = self._load()
        return bool(rec and rec.get("can_create_tickets", True))

    def limits(self):
        rec = self._load() or {}
        return rec.get("limits") or {}

    def _enroll(self):
        token = (self.config.get("enrollment_token") or "").strip()
        if not token:
            raise PanelNotEnrolled(401, "not_enrolled", "This PC is not enrolled. Ask your IT provider for a new setup.")
        folder = self._data_folder()
        body = {
            "enrollment_token": token,
            "device": {
                "install_id": credstore.install_id(folder),
                "hostname": socket.gethostname()[:100],
                "os": {"Windows": "windows", "Darwin": "macos"}.get(platform.system(), "linux"),
                "os_version": platform.platform()[:100],
                "app_version": self._app_version,
            },
        }
        data = self._send("POST", "panel_enroll", json=body, auth=False)
        rec = {
            "base_url": self._base_url(),
            "device_token": data["device_token"],
            "device_id": data.get("device_id"),
            "can_create_tickets": bool(data.get("can_create_tickets")),
            "limits": data.get("limits") or {},
            "client": data.get("client"),
            "contact": data.get("contact"),
        }
        credstore.save(folder, rec)
        self._record = rec
        return rec

    def ensure_enrolled(self):
        return self._load() or self._enroll()

    # ── transport ────────────────────────────────────────────────────────

    def _send(self, method, endpoint, *, params=None, json=None, auth=True, timeout=20):
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + self.ensure_enrolled()["device_token"]
        resp = requests.request(
            method, f"{self._base_url()}/api/v1/{endpoint}", params=params, json=json,
            headers=headers, timeout=timeout, allow_redirects=False,
        )
        if resp.is_redirect:
            resp.close()
            raise requests.HTTPError("The server redirected the request. Set rivet_base_url to the final https URL.")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            if resp.status_code >= 500 or not isinstance(data, dict):
                resp.raise_for_status()   # transient: callers queue / retry on RequestException
            code = data.get("code", "error")
            if auth and resp.status_code == 401 and code in ("revoked", "expired", "invalid_token"):
                credstore.clear(self._data_folder())
                self._record = None
            raise PanelError(
                resp.status_code, code, data.get("error") or f"Request failed ({resp.status_code})",
                retry_after=resp.headers.get("Retry-After"),
            )
        return data

    # ── tickets ──────────────────────────────────────────────────────────

    def create_ticket(self, subject, details, *, category_id=None, files=None, request_id=None):
        if not self.can_create_tickets() and self.enrolled():
            raise PanelError(403, "client_level_disabled",
                             "Your IT provider hasn't enabled ticket creation from this PC.")
        body = {
            "subject": subject,
            "body": details,
            "client_request_id": request_id or str(uuid.uuid4()),
            "display_name": _display_name(),
        }
        if category_id:
            body["category_id"] = int(category_id)
        attachments = self._attachments(files)
        if attachments:
            body["attachments"] = attachments
        data = self._send("POST", "panel_tickets", json=body, timeout=60)
        tid = data.get("ticket_id")
        return Ticket(id=tid, number=str(tid), subject=subject, status="New", raw=data)

    def _attachments(self, files):
        out = []
        allowed = {e.lower() for e in (self.limits().get("attachment_extensions") or [])}
        for _field, filename, content, content_type in files or []:
            ext = os.path.splitext(filename)[1].lstrip(".").lower()
            if allowed and ext not in allowed:
                raise PanelError(422, "attachment_rejected",
                                 f"Files of type .{ext} can't be attached (allowed: {', '.join(sorted(allowed))}).")
            ctype = content_type if content_type != "application/octet-stream" else \
                (mimetypes.guess_type(filename)[0] or content_type)
            out.append({"filename": filename, "content_type": ctype,
                        "data_base64": base64.b64encode(content).decode("ascii")})
        return out

    def list_tickets(self, *, limit=15):
        data = self._send("GET", "panel_tickets", params={"limit": limit})
        return [
            Ticket(id=t["ticket_id"], number=str(t["ticket_id"]), subject=_text(t.get("subject")),
                   status=_text(t.get("status")), created_at=t.get("created_at") or "", raw=t)
            for t in data.get("tickets", [])
        ]

    def get_ticket(self, ticket_id):
        data = self._send("GET", "panel_ticket_chat", params={"ticket_id": ticket_id, "limit": 1})
        t = data.get("ticket") or {}
        return Ticket(id=t.get("ticket_id", ticket_id), number=str(t.get("ticket_id", ticket_id)),
                      subject=_text(t.get("subject")), status=_text(t.get("status")), raw=t)

    def list_categories(self):
        data = self._send("GET", "panel_categories")
        return [Category(id=c["category_id"], name=_text(c.get("name"))) for c in data.get("categories", [])]

    def portal_url(self):
        return f"{self._base_url()}/client/"

    # ── chat ─────────────────────────────────────────────────────────────

    def poll_chat(self, ticket_id, since_id=0):
        try:
            data = self._send("GET", "panel_ticket_chat", params={"ticket_id": ticket_id, "after": since_id})
        except PanelError as exc:
            if exc.code in ("module_disabled", "client_level_disabled"):
                raise ChatUnavailable(str(exc))
            raise
        return [
            ChatMessage(
                id=m["message_id"],
                sender_type="agent" if m.get("from") == "staff" else "contact",
                sender_name=_text(m.get("author")) or ("Agent" if m.get("from") == "staff" else "You"),
                message=_text(m.get("body")),
                created_at=m.get("created_at") or "",
            )
            for m in data.get("messages", [])
        ]

    def send_chat_message(self, ticket_id, message):
        self._send("POST", "panel_ticket_chat", params={"ticket_id": ticket_id},
                   json={"body": message, "display_name": _display_name()})


def _display_name():
    try:
        return getpass.getuser()[:100]
    except Exception:
        return ""
