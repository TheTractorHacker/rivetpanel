"""
RivetIT/RivetMSP PSA provider.

Talks to RivetIT/RivetMSP's REST API (/api/v1/...). This is a behavior-preserving
extraction of what used to live inline in common/core.py - see git history
on core.py prior to the psa/ refactor for the original call sites.
"""

import json
import socket
import threading

import requests
from urllib.parse import urlparse

from .base import Category, ChatMessage, ChatUnavailable, PSAProvider, Ticket


class RivetProvider(PSAProvider):
    name = "rivet"
    supports_chat = True
    supports_stream_chat = True
    supports_categories = True

    def _base_url(self):
        """Validated server URL: https (http only for localhost or when
        allow_insecure_http is set), no credentials, no query/fragment."""
        raw = (self.config.get("rivet_base_url") or self.config.get("itflow_base_url") or "").strip().rstrip("/")
        u = urlparse(raw)
        local = u.hostname in ("localhost", "127.0.0.1", "::1")
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("rivet_base_url must be an http(s) URL.")
        if u.scheme == "http" and not (local or self.config.get("allow_insecure_http")):
            raise ValueError("rivet_base_url must use https (set allow_insecure_http only for testing).")
        if u.username or u.password or u.query or u.fragment:
            raise ValueError("rivet_base_url must not contain credentials, a query or a fragment.")
        return raw

    @staticmethod
    def _checked(resp):
        if resp.is_redirect:
            resp.close()
            raise requests.HTTPError(
                "The server redirected the request. Set rivet_base_url to the final https URL.")
        return resp

    @classmethod
    def _get(cls, *a, **kw):
        # Never follow redirects: X-Api-Key is a custom header, so requests
        # would resend it to whatever host a redirect points at.
        return cls._checked(requests.get(*a, allow_redirects=False, **kw))

    @classmethod
    def _post(cls, *a, **kw):
        return cls._checked(requests.post(*a, allow_redirects=False, **kw))

    def _headers(self):
        """Auth header for RivetIT/RivetMSP API requests.

        Passes the api_key in a header rather than a URL query parameter
        so it is never written to web server access logs.
        """
        return {"X-Api-Key": self.config["api_key"]}

    # ── Tickets ──────────────────────────────────────────────────────────

    def create_ticket(self, subject, details, *, category_id=None, files=None, request_id=None):
        cfg = self.config
        data = {
            "subject": subject,
            "details": details,
            "client_id": cfg["client_id"],
            "priority": cfg.get("priority", "Medium"),
            "hostname": socket.gethostname(),
        }
        if cfg.get("contact_id"):
            data["contact_id"] = cfg["contact_id"]
        if category_id:
            data["category_id"] = category_id

        request_files = None
        if files:
            request_files = [
                (field, (filename, content, content_type))
                for field, filename, content, content_type in files
            ]

        resp = self._post(
            f"{self._base_url()}/api/v1/tickets",
            headers=self._headers(),
            data=data,
            files=request_files,
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        return Ticket(
            id=result.get("id"),
            number=result.get("number", "?"),
            subject=subject,
            status="New",
            raw=result,
        )

    def list_tickets(self, *, limit=15):
        cfg = self.config
        params = {
            "status": "all",
            "limit": limit,
            "client_id": cfg["client_id"],
        }
        if cfg.get("contact_id"):
            params["contact_id"] = cfg["contact_id"]

        resp = self._get(
            f"{self._base_url()}/api/v1/tickets",
            params=params,
            headers=self._headers(),
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [
            Ticket(
                id=t.get("id"),
                number=t.get("number"),
                subject=t.get("subject"),
                status=t.get("status"),
                created_at=t.get("created_at") or "",
                raw=t,
            )
            for t in data
        ]

    def get_ticket(self, ticket_id):
        resp = self._get(
            f"{self._base_url()}/api/v1/tickets/{int(ticket_id)}",
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        t = resp.json()
        return Ticket(
            id=t.get("id"),
            number=t.get("number", "?"),
            subject=t.get("subject", ""),
            status=t.get("status", ""),
            created_at=t.get("created_at") or "",
            raw=t,
        )

    def list_categories(self):
        resp = self._get(
            f"{self._base_url()}/api/v1/ticket-categories",
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return [Category(id=c.get("id"), name=c.get("name")) for c in (resp.json() or [])]

    def portal_url(self):
        return f"{self._base_url()}/client/"

    # ── Live chat ────────────────────────────────────────────────────────

    def poll_chat(self, ticket_id, since_id=0):
        resp = self._get(
            f"{self._base_url()}/api/v1/tickets/{int(ticket_id)}/chat",
            params={"since_id": since_id},
            headers=self._headers(),
            timeout=15,
        )
        if resp.status_code == 403:
            raise ChatUnavailable("Live chat module disabled on the server")
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [
            ChatMessage(
                id=m.get("id"),
                sender_type=m.get("sender_type"),
                sender_name=m.get("sender_name") or ("Agent" if m.get("sender_type") == "agent" else "You"),
                message=m.get("message", ""),
                created_at=m.get("created_at") or "",
            )
            for m in data
        ]

    def send_chat_message(self, ticket_id, message):
        cfg = self.config
        body = {"message": message}
        if cfg.get("contact_id"):
            body["contact_id"] = cfg["contact_id"]
        resp = self._post(
            f"{self._base_url()}/api/v1/tickets/{int(ticket_id)}/chat",
            headers=self._headers(),
            json=body,
            timeout=15,
        )
        resp.raise_for_status()

    def stream_chat(self, ticket_id, on_message, stop_event):
        """Reconnecting SSE listener for one ticket's live chat channel.

        Calls on_message(ChatMessage) for every "chat" event received. Runs
        until stop_event is set. The server intentionally closes the
        connection every ~60s (see sse_ticket_chat_stream.php's
        $max_runtime) so a short pause and reconnect on any disconnect is
        the expected steady state, not an error.
        """
        url = f"{self._base_url()}/api/v1/tickets/{int(ticket_id)}/chat"
        params = {"stream": 1}

        while not stop_event.is_set():
            try:
                resp = self._get(
                    url, params=params, headers=self._headers(), stream=True, timeout=(10, 75)
                )
                if resp.status_code == 403:
                    # Live chat module disabled server-side - no point retrying.
                    return
                if resp.status_code != 200:
                    stop_event.wait(15)
                    continue

                event_name = None
                for line in resp.iter_lines(decode_unicode=True):
                    if stop_event.is_set():
                        return
                    if line is None or line == "":
                        event_name = None
                        continue
                    if line.startswith("event:"):
                        event_name = line[len("event:"):].strip()
                    elif line.startswith("data:") and event_name == "chat":
                        try:
                            data = json.loads(line[len("data:"):].strip())
                            on_message(ChatMessage(
                                id=data.get("chat_id"),
                                sender_type=data.get("sender_type"),
                                sender_name=data.get("sender_name"),
                                message=data.get("message"),
                                created_at=data.get("created_at") or "",
                            ))
                        except Exception:
                            pass
            except Exception:
                pass

            stop_event.wait(2)
