"""
PSA/RMM provider interface.

Every backend ITPanel Pro can talk to (ITFlow, Syncro, Atera, ...)
implements PSAProvider. common/core.py's UI/background code is written
entirely against this interface plus the normalized dataclasses below,
never against a specific provider's request/response shapes.

Capability flags (supports_chat, supports_stream_chat, supports_categories)
let core.py hide UI elements (the chat window, the category dropdown) for
providers that don't support them, instead of those methods raising or
being stubbed out with dead code paths.
"""

from dataclasses import dataclass, field


@dataclass
class Ticket:
    id: str
    number: str
    subject: str
    status: str
    created_at: str = ""
    raw: dict = field(default_factory=dict)  # escape hatch for provider-specific extras


@dataclass
class ChatMessage:
    id: int
    sender_type: str  # "agent" | "contact"
    sender_name: str
    message: str
    created_at: str = ""


@dataclass
class Category:
    id: str
    name: str


class ChatUnavailable(Exception):
    """Raised by a provider's chat methods when live chat is temporarily or
    permanently unusable server-side (e.g. ITFlow's "module disabled" 403).
    core.py catches this generically instead of checking HTTP status codes."""


class PSAProvider:
    """Base class for a PSA/RMM backend. One concrete subclass is compiled
    into each ITPanel Pro build (see psa/__init__.py) - never more than one
    per running app, so no runtime provider switching exists."""

    name = "base"
    supports_chat = False
    supports_stream_chat = False
    supports_categories = False

    def __init__(self, config: dict):
        self.config = config

    def create_ticket(self, subject: str, details: str, *, category_id=None, files=None) -> Ticket:
        """files: list of (field_name, filename, bytes, content_type), or None."""
        raise NotImplementedError

    def list_tickets(self, *, limit: int = 15) -> list:
        raise NotImplementedError

    def get_ticket(self, ticket_id) -> Ticket:
        raise NotImplementedError

    def list_categories(self) -> list:
        return []

    def portal_url(self):
        return None

    # ── Optional chat capability - only called by core.py when the
    # capability flags above are True ──────────────────────────────────────

    def poll_chat(self, ticket_id, since_id: int = 0) -> list:
        raise NotImplementedError

    def send_chat_message(self, ticket_id, message: str) -> None:
        raise NotImplementedError

    def stream_chat(self, ticket_id, on_message, stop_event) -> None:
        """Block until stop_event is set, calling on_message(ChatMessage) for
        each new message. Must handle its own reconnect logic internally."""
        raise NotImplementedError
