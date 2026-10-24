"""
RivetPanel - shared core

Platform-specific entry points (Windows/tray_app.py, Linux/tray_app.py,
macOS/tray_app.py) import this module and call run_app() with a list of
config file locations to check (in order of preference) and the path to
the tray/window icon image.

This module contains everything that is identical across platforms: config
loading, the ttk styling, the ticket popup window, the recent-tickets
window, offline queueing, update checks, the Quick Tools troubleshooting
menu, and the tray icon / event loop wiring.
"""

import base64
import getpass
import hashlib
import io
import json
import os
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
import webbrowser
import zipfile

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from PIL import Image, ImageDraw, ImageGrab, ImageTk
import pystray
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from psa import get_provider_class, get_provider_name
from psa.base import ChatUnavailable

APP_NAME = "RivetPanel"
VERSION = "2.1.10"
GITHUB_REPO = "TheTractorHacker/rivetpanel"

# Ed25519 public key for verifying release authenticity before the self-update
# mechanism extracts/executes anything downloaded from a GitHub release. The
# matching private key is held only as a GitHub Actions secret (see
# scripts/sign_release.py and .github/workflows/build.yml's sign-release job)
# and is never committed to this repository. Rotating this key requires
# shipping a new signed release under the OLD key first (so existing installs
# can still verify and update), since an install can never trust an update
# signed with a key it doesn't already have embedded.
RELEASE_SIGNING_PUBLIC_KEY_B64 = "/xTmtFmmSpU2/lQ0DW3MjIPQi5ja6hBtbY1ZlrvHLag="


def _notify(icon, message: str, title: str = APP_NAME, chat: bool = False):
    """Show a desktop notification.

    pystray's Icon.notify() (a legacy Shell_NotifyIcon balloon) is
    unreliable on Windows 11: it can silently no-op depending on a per-app
    toggle hidden behind right-clicking the tray icon itself - separate
    from, and overriding, the "Notification Area Icons" Control Panel
    setting most people know to check - with no exception raised either
    way. See https://github.com/moses-palmer/pystray/issues/112. Show a
    real toast via winotify (WinRT's toast API, via a bundled PowerShell
    call) on Windows instead; macOS/Linux already notify reliably through
    pystray's native backends there.

    chat: use the IM notification sound instead of the generic default
    (winotify toasts are silent unless a sound is explicitly set).
    """
    if platform.system() == "Windows":
        try:
            from winotify import Notification, audio
            toast = Notification(app_id=APP_NAME, title=title, msg=message)
            toast.set_audio(audio.IM if chat else audio.Default, loop=False)
            toast.show()
            return
        except Exception:
            pass  # fall through to the pystray balloon as a last resort
    try:
        icon.notify(message, title=title)
    except Exception:
        pass

ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
BG = "#f4f6fb"
CARD_BG = "#ffffff"
BORDER = "#d7dce5"
TEXT_MUTED = "#5b6573"


def app_dir():
    """Directory containing the running script, or the PyInstaller binary."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # .../<Platform>/tray_app.py -> .../<Platform>
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def app_folder_name():
    """PascalCase app folder name for Windows/macOS (ProgramData, Application
    Support). Resolves to plain "RivetPanel" for the rivet build so existing
    installs keep upgrading into the same paths with no migration; other
    providers get a "RivetPanel-<Provider>" folder so multiple provider
    builds can coexist on one machine without colliding."""
    name = get_provider_name()
    return "RivetPanel" if name == "rivet" else f"RivetPanel-{name.capitalize()}"


def app_folder_slug():
    """kebab-case app folder name for Linux (/etc, XDG dirs). Same
    rivet-stays-unqualified rule as app_folder_name()."""
    name = get_provider_name()
    return "rivetpanel" if name == "rivet" else f"rivetpanel-{name}"


def data_dir():
    """Per-user writable directory for the offline queue and ticket tracking."""
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, app_folder_name())
    elif system == "Darwin":
        path = os.path.expanduser(f"~/Library/Application Support/{app_folder_name()}")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        path = os.path.join(base, app_folder_slug())
    os.makedirs(path, exist_ok=True)
    return path


def load_config(extra_paths):
    """Return the first config.json found among extra_paths, then app_dir()."""
    candidates = list(extra_paths) + [os.path.join(app_dir(), "config.json")]
    for path in candidates:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg["_source"] = path
            return cfg
    raise FileNotFoundError(
        "config.json not found. Checked:\n" + "\n".join(c for c in candidates if c)
    )


def build_tray_icon_image(icon_path, branding_logo=None):
    """Load the branding logo or bundled tray icon, or fall back to a drawn one."""
    candidate = safe_image_path(branding_logo) or icon_path
    if candidate and os.path.isfile(candidate):
        return Image.open(candidate)

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, size - 3, size - 3], fill=(37, 99, 235, 255))
    draw.rectangle([14, 22, size - 15, size - 22], fill=(255, 255, 255, 255))
    return img


_badged_icon_cache = {}  # id(base image) -> badged copy, built lazily once


def _tray_icon_with_badge(base_image):
    cached = _badged_icon_cache.get(id(base_image))
    if cached is not None:
        return cached
    img = base_image.convert("RGBA").copy()
    w, h = img.size
    r = max(8, w // 4)
    draw = ImageDraw.Draw(img)
    draw.ellipse([w - r, 0, w, r], fill=(220, 38, 38, 255), outline=(255, 255, 255, 255), width=2)
    _badged_icon_cache[id(base_image)] = img
    return img


def set_chat_unread(icon, unread: bool):
    """Toggle a small red badge on the tray icon for unread live-chat messages.

    Reads the un-badged image from icon._base_image (set once in run_app)
    rather than taking it as a parameter, so callers that only have `icon`
    (poll_ticket_chat, _on_chat_event) don't need to thread it through.
    """
    base = getattr(icon, "_base_image", None)
    if base is None:
        return
    try:
        icon.icon = _tray_icon_with_badge(base) if unread else base
    except Exception:
        pass


def _darken(hex_color, factor=0.82):
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    r, g, b = (max(0, min(255, int(c * factor))) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def setup_style(root, accent=None):
    """Configure a clean, modern ttk theme shared by every window.

    accent: optional hex color (e.g. "#16a34a") to override the default
    RivetIT blue, for MSPs that want their own branding color.
    """
    accent_color = accent or ACCENT
    accent_dark = _darken(accent_color) if accent else ACCENT_DARK

    style = ttk.Style(root)
    for theme in ("clam",):
        try:
            style.theme_use(theme)
            break
        except tk.TclError:
            continue

    base_font = ("Segoe UI", 10)
    bold_font = ("Segoe UI", 10, "bold")
    title_font = ("Segoe UI Semibold", 13)

    root.option_add("*Font", base_font)

    style.configure(".", font=base_font, background=BG)
    style.configure("Card.TFrame", background=CARD_BG)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground="#1f2533")
    style.configure("Card.TLabel", background=CARD_BG, foreground="#1f2533")
    style.configure("Title.TLabel", background=BG, font=title_font, foreground="#1f2533")
    style.configure("Muted.TLabel", background=BG, foreground=TEXT_MUTED)
    style.configure("Muted.Card.TLabel", background=CARD_BG, foreground=TEXT_MUTED)
    style.configure("FieldLabel.TLabel", background=BG, font=bold_font, foreground="#374151")
    style.configure("TCheckbutton", background=BG, foreground="#1f2533")

    style.configure(
        "TEntry",
        padding=8,
        relief="flat",
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        fieldbackground=CARD_BG,
    )
    style.map("TEntry", bordercolor=[("focus", accent_color)])

    style.configure(
        "TCombobox",
        padding=6,
        relief="flat",
        fieldbackground=CARD_BG,
        background=CARD_BG,
    )
    style.map("TCombobox", bordercolor=[("focus", accent_color)])

    style.configure(
        "Accent.TButton",
        font=bold_font,
        background=accent_color,
        foreground="white",
        borderwidth=0,
        padding=(12, 10),
    )
    style.map(
        "Accent.TButton",
        background=[("active", accent_dark), ("disabled", "#9db4e8")],
    )

    style.configure(
        "Secondary.TButton",
        font=base_font,
        background="#e9edf6",
        foreground="#1f2533",
        borderwidth=0,
        padding=(10, 8),
    )
    style.map(
        "Secondary.TButton",
        background=[("active", "#dbe2f0")],
    )

    # Combobox / checkbox / scrollbar / table: flat, consistent with the cards
    style.configure(
        "TCombobox", padding=7, relief="flat", bordercolor=BORDER, lightcolor=BORDER,
        darkcolor=BORDER, arrowcolor=TEXT_MUTED, arrowsize=14,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", CARD_BG)],
        background=[("readonly", CARD_BG), ("active", CARD_BG)],
        bordercolor=[("focus", accent_color)],
        selectbackground=[("readonly", CARD_BG)],
        selectforeground=[("readonly", "#1f2533")],
    )
    root.option_add("*TCombobox*Listbox.selectBackground", accent_color)
    root.option_add("*TCombobox*Listbox.font", base_font)
    style.configure(
        "TCheckbutton", background=BG, foreground="#1f2533",
        indicatorcolor=CARD_BG, indicatorbackground=CARD_BG, bordercolor=BORDER,
    )
    style.map(
        "TCheckbutton",
        indicatorcolor=[("selected", accent_color)],
        background=[("active", BG)],
    )
    style.configure(
        "Vertical.TScrollbar", background="#e3e8f2", troughcolor=BG, bordercolor=BG,
        lightcolor="#e3e8f2", darkcolor="#e3e8f2", arrowsize=12, relief="flat",
    )
    style.map("Vertical.TScrollbar", background=[("active", "#c9d2e4")])
    style.configure(
        "Treeview", background=CARD_BG, fieldbackground=CARD_BG, foreground="#1f2533",
        rowheight=30, borderwidth=0, relief="flat",
    )
    style.map(
        "Treeview",
        background=[("selected", accent_color)],
        foreground=[("selected", "white")],
    )
    style.configure(
        "Treeview.Heading", background="#eef1f8", foreground=TEXT_MUTED, font=bold_font,
        relief="flat", borderwidth=0, padding=(8, 8),
    )
    style.map("Treeview.Heading", background=[("active", "#e3e8f2")])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure("Header.TFrame", background=CARD_BG)
    style.configure("Header.TLabel", background=CARD_BG, foreground="#1f2533", font=title_font)
    style.configure("HeaderMuted.TLabel", background=CARD_BG, foreground=TEXT_MUTED)

    return style


def build_window_header(win, title, subtitle="", icon_path=None, accent=ACCENT, keep=None):
    """Branded card header with an accent stripe, shared by every window.
    Returns the header frame; pass `keep` (a list) to hold the PhotoImage."""
    tk.Frame(win, bg=accent, height=4).pack(fill="x")
    header = ttk.Frame(win, style="Header.TFrame", padding=(20, 14))
    header.pack(fill="x")
    if icon_path and os.path.isfile(icon_path):
        try:
            img = ImageTk.PhotoImage(Image.open(icon_path).resize((36, 36)))
            if keep is not None:
                keep.append(img)
            ttk.Label(header, image=img, style="Header.TLabel").pack(side="left", padx=(0, 12))
        except Exception:
            pass
    box = ttk.Frame(header, style="Header.TFrame")
    box.pack(side="left", fill="x", expand=True)
    ttk.Label(box, text=title, style="Header.TLabel").pack(anchor="w")
    if subtitle:
        ttk.Label(box, text=subtitle, style="HeaderMuted.TLabel").pack(anchor="w")
    tk.Frame(win, bg=BORDER, height=1).pack(fill="x")
    return header


STATUS_COLORS = {
    "new": "#2563eb", "open": "#2563eb", "in progress": "#b45309", "assigned": "#b45309",
    "on hold": "#6b7280", "awaiting": "#7c3aed", "resolved": "#15803d", "closed": "#6b7280",
}


# ── System info ───────────────────────────────────────────────────────────

def gather_system_info():
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "unknown"

    try:
        username = getpass.getuser()
    except Exception:
        username = "unknown"

    local_ip = "unknown"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        pass

    return {
        "hostname": hostname,
        "os": f"{platform.system()} {platform.release()}",
        "username": username,
        "local_ip": local_ip,
    }


def format_system_info_block(info):
    return (
        "\n\n--- System Info (auto-attached) ---\n"
        f"Hostname: {info['hostname']}\n"
        f"OS: {info['os']}\n"
        f"User: {info['username']}\n"
        f"Local IP: {info['local_ip']}"
    )


# ── Quick Tools (troubleshooting) ───────────────────────────────────────────

def get_public_ip():
    try:
        resp = requests.get("https://api.ipify.org?format=json", timeout=8)
        resp.raise_for_status()
        return resp.json().get("ip", "Unknown")
    except Exception as exc:
        return f"Could not determine public IP: {exc}"


def ping_host(host="google.com"):
    count_flag = "-n" if platform.system() == "Windows" else "-c"
    try:
        result = subprocess.run(
            ["ping", count_flag, "4", host],
            capture_output=True, text=True, timeout=20,
        )
        return (result.stdout or result.stderr).strip()
    except FileNotFoundError:
        return "ping command not available on this system."
    except Exception as exc:
        return f"Error pinging {host}: {exc}"


def list_printers():
    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-Printer | Select-Object -ExpandProperty Name"],
                capture_output=True, text=True, timeout=20,
            )
            names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        else:
            result = subprocess.run(["lpstat", "-p"], capture_output=True, text=True, timeout=20)
            names = []
            for line in result.stdout.splitlines():
                if line.startswith("printer "):
                    names.append(line.split()[1])

        if not names:
            return "No printers found."
        return "\n".join(names)
    except FileNotFoundError:
        return "Printer listing tool not available on this system."
    except Exception as exc:
        return f"Error listing printers: {exc}"


def restart_print_spooler():
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.run(["net", "stop", "spooler"], capture_output=True, text=True, timeout=30, check=True)
            subprocess.run(["net", "start", "spooler"], capture_output=True, text=True, timeout=30, check=True)
            return "Print spooler restarted successfully."
        elif system == "Darwin":
            subprocess.run(["launchctl", "stop", "org.cups.cupsd"], capture_output=True, text=True, timeout=30, check=True)
            subprocess.run(["launchctl", "start", "org.cups.cupsd"], capture_output=True, text=True, timeout=30, check=True)
            return "Print service (CUPS) restarted successfully."
        else:
            subprocess.run(["systemctl", "restart", "cups"], capture_output=True, text=True, timeout=30, check=True)
            return "Print service (CUPS) restarted successfully."
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        return (
            "Couldn't restart the print service - this usually requires "
            "administrator/root privileges.\n\n" + detail
        )
    except FileNotFoundError:
        return "Print service control tool not available on this system."
    except Exception as exc:
        return f"Error restarting print service: {exc}"


def tool_public_ip():
    return f"Your public IP address is:\n\n{get_public_ip()}"


def tool_ping():
    return "Pinging google.com...\n\n" + ping_host("google.com")


def tool_list_printers():
    return "Installed printers:\n\n" + list_printers()


def tool_restart_spooler():
    return restart_print_spooler()


def open_network_adapters():
    """Open the OS's network adapters/connections settings UI."""
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(["control.exe", "ncpa.cpl"])
        elif system == "Darwin":
            subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.network"])
        else:
            for cmd in (["nm-connection-editor"], ["gnome-control-center", "network"]):
                try:
                    subprocess.Popen(cmd)
                    return
                except FileNotFoundError:
                    continue
    except Exception:
        pass


def run_quick_tool(root, title, func):
    """Run a (possibly slow/blocking) tool function in a background thread
    and show its result in a message box on the Tk main thread."""
    def worker():
        try:
            result = func()
        except Exception as exc:
            result = f"Error: {exc}"
        root.after(0, lambda: messagebox.showinfo(title, result))
    threading.Thread(target=worker, daemon=True).start()


# ── Offline queue ────────────────────────────────────────────────────────

def queue_dir():
    path = os.path.join(data_dir(), "queue")
    os.makedirs(path, exist_ok=True)
    return path


def save_to_queue(subject, details, category_id, files_payload, request_id=None):
    """Persist a ticket submission to retry later, in provider-agnostic form
    (retrying goes back through provider.create_ticket(), same as a fresh
    submission).

    files_payload: list of (field_name, filename, bytes, content_type)
    """
    item_dir = os.path.join(queue_dir(), str(int(time.time() * 1000)))
    os.makedirs(item_dir, exist_ok=True)

    payload = {"subject": subject, "details": details, "category_id": category_id, "request_id": request_id}
    with open(os.path.join(item_dir, "payload.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f)

    manifest = []
    for i, (field_name, filename, content, content_type) in enumerate(files_payload):
        stored_as = f"file_{i}_{filename}"
        with open(os.path.join(item_dir, stored_as), "wb") as f:
            f.write(content)
        manifest.append({
            "field": field_name, "filename": filename,
            "stored_as": stored_as, "content_type": content_type,
        })

    with open(os.path.join(item_dir, "files.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)


def flush_queue(config, provider):
    """Try to resend queued tickets. Stops at the first failure (assumed
    still offline). Returns the number successfully sent."""
    qdir = queue_dir()
    sent = 0
    for name in sorted(os.listdir(qdir)):
        item_dir = os.path.join(qdir, name)
        payload_path = os.path.join(item_dir, "payload.json")
        if not os.path.isfile(payload_path):
            continue

        try:
            with open(payload_path, "r", encoding="utf-8") as f:
                payload = json.load(f)

            files = None
            manifest_path = os.path.join(item_dir, "files.json")
            if os.path.isfile(manifest_path):
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                if manifest:
                    files = []
                    for entry in manifest:
                        with open(os.path.join(item_dir, entry["stored_as"]), "rb") as fh:
                            files.append((entry["field"], entry["filename"], fh.read(), entry["content_type"]))

            ticket = provider.create_ticket(
                payload["subject"], payload["details"],
                category_id=payload.get("category_id"), files=files,
                request_id=payload.get("request_id"),
            )
            if ticket.id:
                track_new_ticket(ticket.id, ticket.number, payload["subject"])

            shutil.rmtree(item_dir, ignore_errors=True)
            sent += 1
        except Exception:
            break

    return sent


# ── Ticket tracking (for status-change notifications) ──────────────────────

def tracked_tickets_path():
    return os.path.join(data_dir(), "tracked_tickets.json")


def load_tracked_tickets():
    path = tracked_tickets_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_tracked_tickets(tickets):
    with open(tracked_tickets_path(), "w", encoding="utf-8") as f:
        json.dump(tickets, f)


def track_new_ticket(ticket_id, number, subject, status="New"):
    tickets = load_tracked_tickets()
    tickets.append({"id": ticket_id, "number": number, "subject": subject, "status": status, "last_chat_id": 0})
    save_tracked_tickets(tickets[-20:])
    # Start an instant chat listener for this ticket right away, instead of
    # waiting for the next background_loop reconciliation pass.
    try:
        _ensure_chat_listeners()
    except Exception:
        pass


def poll_ticket_updates(config, provider, icon):
    """Check tracked tickets for status changes and show a notification."""
    tickets = load_tracked_tickets()
    if not tickets:
        return

    changed = False
    for t in tickets:
        try:
            new_status = provider.get_ticket(t["id"]).status
            if new_status and new_status != t["status"]:
                _notify(
                    icon,
                    f"Ticket #{t['number']} \"{t['subject']}\" is now {new_status}",
                    title="Ticket Update",
                )
                t["status"] = new_status
                changed = True
        except Exception:
            continue

    if changed:
        save_tracked_tickets(tickets)


def poll_ticket_chat(config, provider, icon):
    """Check tracked tickets for new agent chat messages and notify."""
    if not provider.supports_chat:
        return

    tickets = load_tracked_tickets()
    if not tickets:
        return

    changed = False
    for t in tickets:
        last_chat_id = t.get("last_chat_id", 0)
        try:
            messages = provider.poll_chat(t["id"], since_id=last_chat_id)
        except ChatUnavailable:
            continue
        except Exception:
            continue

        if not messages:
            continue

        agent_messages = [m for m in messages if m.sender_type == "agent"]
        if agent_messages:
            if len(agent_messages) == 1:
                body = agent_messages[0].message
            else:
                body = f"{len(agent_messages)} new messages"
            _notify(icon, f"Ticket #{t['number']}: {body}", title="Live Chat", chat=True)
            set_chat_unread(icon, True)

        t["last_chat_id"] = messages[-1].id
        changed = True

    if changed:
        save_tracked_tickets(tickets)


# ── Live chat (SSE) ──────────────────────────────────────────────────────
#
# poll_ticket_chat() above is a 300s safety-net fallback. The listeners below
# subscribe to a provider's live-push channel (provider.stream_chat(), e.g.
# RivetIT's server-sent-events stream backed by Redis pub/sub - see
# includes/sse_ticket_chat_stream.php) so new messages arrive within ~1-2
# seconds instead of waiting for the next poll. Only invoked for providers
# where provider.supports_stream_chat is True.

_chat_listeners = {}  # ticket_id -> threading.Event (set to stop)
_chat_listener_provider = None
_chat_listener_icon = None


def _on_chat_event(icon, tracked_ticket, message):
    if message.sender_type == "agent":
        _notify(
            icon,
            f"Ticket #{tracked_ticket['number']}: {message.message}",
            title="Live Chat",
            chat=True,
        )
        set_chat_unread(icon, True)

    # Keep last_chat_id in sync so the poll_ticket_chat() fallback doesn't
    # re-notify for messages already delivered live.
    chat_id = message.id or 0
    if not chat_id:
        return
    tickets = load_tracked_tickets()
    changed = False
    for t in tickets:
        if t["id"] == tracked_ticket["id"] and chat_id > t.get("last_chat_id", 0):
            t["last_chat_id"] = chat_id
            changed = True
    if changed:
        save_tracked_tickets(tickets)


def _ensure_chat_listeners():
    """Start a stream_chat() listener thread for every tracked ticket that
    doesn't already have one, and stop listeners for tickets no longer
    tracked.

    Safe to call repeatedly (from the background loop, and immediately after
    track_new_ticket()) - it's a no-op for tickets already covered, and a
    no-op entirely for providers that don't support streaming chat.
    """
    provider = _chat_listener_provider
    icon = _chat_listener_icon
    if not provider or not icon or not provider.supports_stream_chat:
        return

    tickets = load_tracked_tickets()
    tracked_ids = {t["id"] for t in tickets}

    for ticket_id in list(_chat_listeners):
        if ticket_id not in tracked_ids:
            _chat_listeners.pop(ticket_id).set()

    for t in tickets:
        ticket_id = t["id"]
        if ticket_id in _chat_listeners:
            continue
        stop_event = threading.Event()
        threading.Thread(
            target=provider.stream_chat,
            args=(ticket_id, lambda message, t=t: _on_chat_event(icon, t, message), stop_event),
            daemon=True,
        ).start()
        _chat_listeners[ticket_id] = stop_event


# ── Update check ─────────────────────────────────────────────────────────

# ── Hardening helpers ─────────────────────────────────────────────────────

from urllib.parse import urljoin, urlparse

RELEASE_HOSTS = {
    "github.com", "api.github.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_UPDATE_REDIRECTS = 5


def is_trusted_release_url(url, first_hop=True):
    """Only https URLs on GitHub's release hosts; the first hop must also be
    this repo's own release download path. Update URLs come from a JSON
    response, so they are never fetched unchecked."""
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme != "https" or u.hostname not in RELEASE_HOSTS or u.username or u.password:
        return False
    if first_hop and u.hostname == "github.com":
        return u.path.startswith(f"/{GITHUB_REPO}/releases/download/")
    return True


def fetch_release_url(url, **kwargs):
    """requests.get that follows redirects manually, re-validating each hop."""
    for hop in range(MAX_UPDATE_REDIRECTS + 1):
        if not is_trusted_release_url(url, first_hop=(hop == 0)):
            raise ReleaseVerificationError(f"Refusing to download from untrusted URL: {url}")
        resp = requests.get(url, allow_redirects=False, **kwargs)
        if resp.is_redirect and resp.headers.get("Location"):
            url = urljoin(url, resp.headers["Location"])
            resp.close()
            continue
        return resp
    raise ReleaseVerificationError("Too many redirects while downloading update.")


def read_attachment(path, protected_paths=()):
    """Read a user-chosen attachment safely: a regular file (no devices/FIFOs),
    capped size, and never the app's own config or data (which hold the API
    key). Returns bytes; raises ValueError with a user-presentable message."""
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        raise ValueError("The attachment is not a regular file.")
    for prot in protected_paths:
        if not prot:
            continue
        prot_real = os.path.realpath(prot)
        if real == prot_real or real.startswith(prot_real.rstrip(os.sep) + os.sep):
            raise ValueError("That file belongs to the app and can't be attached.")
    if os.path.getsize(real) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"The attachment is larger than {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.")
    with open(real, "rb") as f:
        return f.read(MAX_ATTACHMENT_BYTES + 1)


def safe_image_path(path, max_bytes=5 * 1024 * 1024):
    """Config-supplied logo paths: image extensions, regular file, small."""
    if not path:
        return None
    real = os.path.realpath(path)
    if (os.path.splitext(real)[1].lower() in (".png", ".ico", ".jpg", ".jpeg")
            and os.path.isfile(real) and os.path.getsize(real) <= max_bytes):
        return real
    return None



def _version_tuple(v):
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def check_for_update():
    """Return (latest_version, release_dict) if a newer release is available."""
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest", timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        tag = (data.get("tag_name") or "").lstrip("v")
        if tag and _version_tuple(tag) > _version_tuple(VERSION):
            return tag, data
    except Exception:
        return None
    return None


class ReleaseVerificationError(Exception):
    """Raised when a downloaded release asset fails signature/checksum
    verification. do_self_update() treats this as fatal and aborts the update
    rather than extracting/executing an unverified artifact."""


def _verify_release_asset(assets, asset_name, downloaded_path):
    """Verify that the file at downloaded_path is exactly what the release's
    Ed25519-signed checksums.txt says it should be. Every self-update MUST
    call this after downloading a platform asset and BEFORE extracting or
    executing it - this is the only thing standing between a compromised
    release feed/GitHub account/MITM download and arbitrary code execution
    on every installed client.

    Raises ReleaseVerificationError on any failure (missing signature file,
    bad signature, missing/mismatched checksum entry) - never returns a
    falsy "not verified" value that could be accidentally ignored.
    """
    checksums_asset = next((a for a in assets if a.get("name") == "checksums.txt"), None)
    sig_asset = next((a for a in assets if a.get("name") == "checksums.txt.sig"), None)
    if not checksums_asset or not sig_asset:
        raise ReleaseVerificationError(
            "Release is missing checksums.txt/checksums.txt.sig - refusing to "
            "install an unsigned update."
        )

    checksums_resp = fetch_release_url(checksums_asset["browser_download_url"], timeout=30)
    checksums_resp.raise_for_status()
    checksums_bytes = checksums_resp.content

    sig_resp = fetch_release_url(sig_asset["browser_download_url"], timeout=30)
    sig_resp.raise_for_status()
    signature_b64 = sig_resp.text.strip()

    try:
        signature = base64.b64decode(signature_b64)
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(RELEASE_SIGNING_PUBLIC_KEY_B64)
        )
        public_key.verify(signature, checksums_bytes)
    except (InvalidSignature, ValueError) as exc:
        raise ReleaseVerificationError(f"Release signature verification failed: {exc}")

    expected_hash = None
    for line in checksums_bytes.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, name = line.partition("  ")
        if name.strip() == asset_name:
            expected_hash = digest.strip()
            break
    if not expected_hash:
        raise ReleaseVerificationError(
            f"No checksum entry for '{asset_name}' in the signed checksums.txt."
        )

    actual_hash = hashlib.sha256()
    with open(downloaded_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            actual_hash.update(chunk)
    actual_hash = actual_hash.hexdigest()

    if actual_hash != expected_hash:
        raise ReleaseVerificationError(
            f"Checksum mismatch for '{asset_name}': expected {expected_hash}, got {actual_hash}."
        )


# ── Ticket entry window ─────────────────────────────────────────────────────

class TicketWindow:
    """The popup ticket-entry window. One instance reused per open."""

    def __init__(self, root, config, provider, icon_path=None):
        self.root = root
        self.config = config
        self.provider = provider
        self.icon_path = icon_path
        self.screenshot_bytes = None
        self.attached_file_path = None
        self.categories = []
        self.selected_category_id = 0
        self.window = None
        self._header_img = None
        self._thumb_image = None

    def show(self):
        if self.window is not None and self.window.winfo_exists():
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()
            return

        self.screenshot_bytes = None
        self.attached_file_path = None
        self.selected_category_id = 0

        win = tk.Toplevel(self.root)
        win.title(APP_NAME)
        win.resizable(False, False)
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        if self.icon_path and os.path.isfile(self.icon_path):
            try:
                win.iconbitmap(self.icon_path)
            except tk.TclError:
                pass
        self.window = win

        self._header_keep = []
        build_window_header(
            win, "New Support Ticket",
            (f"{self.config['company_name']} will get back to you as soon as possible"
             if self.config.get("company_name") else "We'll get back to you as soon as possible"),
            self.icon_path, accent=self.config.get("accent_color") or ACCENT, keep=self._header_keep,
        )
        outer = ttk.Frame(win, padding=(20, 16, 20, 20), style="TFrame")
        outer.pack(fill="both", expand=True)

        # Subject
        ttk.Label(outer, text="Subject", style="FieldLabel.TLabel").pack(anchor="w", pady=(0, 4))
        self.subject_entry = ttk.Entry(outer, width=46)
        self.subject_entry.pack(fill="x", pady=(0, 14))

        # Category (only shown if the server has ticket categories configured)
        if self.categories:
            ttk.Label(outer, text="Category", style="FieldLabel.TLabel").pack(anchor="w", pady=(0, 4))
            self.category_var = tk.StringVar(value="Uncategorized")
            category_names = ["Uncategorized"] + [c.name for c in self.categories]
            self.category_combo = ttk.Combobox(
                outer, textvariable=self.category_var, values=category_names,
                state="readonly", width=43,
            )
            self.category_combo.pack(fill="x", pady=(0, 14))
            self.category_combo.bind("<<ComboboxSelected>>", self._on_category_selected)

        # Description
        ttk.Label(outer, text="Describe the issue", style="FieldLabel.TLabel").pack(anchor="w", pady=(0, 4))
        text_frame = tk.Frame(outer, bg=BORDER, padx=1, pady=1)
        text_frame.pack(fill="x", pady=(0, 14))
        self.details_text = tk.Text(
            text_frame,
            width=46,
            height=7,
            relief="flat",
            bg=CARD_BG,
            fg="#1f2533",
            insertbackground="#1f2533",
            font=("Segoe UI", 10),
            padx=8,
            pady=8,
            wrap="word",
        )
        self.details_text.pack(fill="both", expand=True)

        # System info checkbox
        self.include_sysinfo_var = tk.BooleanVar(value=self.config.get("include_system_info", True))
        ttk.Checkbutton(
            outer, text="Include system info (helps us diagnose faster)",
            variable=self.include_sysinfo_var,
        ).pack(anchor="w", pady=(0, 10))

        # Screenshot preview area
        preview_frame = tk.Frame(outer, bg=CARD_BG, highlightbackground=BORDER, highlightthickness=1)
        preview_frame.pack(fill="x", pady=(0, 10))
        self.preview_label = tk.Label(
            preview_frame,
            text="No screenshot attached",
            fg=TEXT_MUTED,
            bg=CARD_BG,
            font=("Segoe UI", 9),
            height=6,
        )
        self.preview_label.pack(fill="both", expand=True, padx=2, pady=2)

        # Screenshot buttons
        btn_row = ttk.Frame(outer, style="TFrame")
        btn_row.pack(fill="x", pady=(0, 10))
        btn_row.columnconfigure(0, weight=1)
        btn_row.columnconfigure(1, weight=1)

        self.screenshot_btn = ttk.Button(
            btn_row, text="Attach Screenshot", style="Secondary.TButton", command=self.take_screenshot
        )
        self.screenshot_btn.grid(row=0, column=0, sticky="we", padx=(0, 6))

        self.remove_btn = ttk.Button(
            btn_row, text="Remove Screenshot", style="Secondary.TButton", command=self.remove_screenshot, state="disabled"
        )
        self.remove_btn.grid(row=0, column=1, sticky="we", padx=(6, 0))

        # File attachment
        file_row = ttk.Frame(outer, style="TFrame")
        file_row.pack(fill="x", pady=(0, 14))
        self.attach_file_btn = ttk.Button(
            file_row, text="Attach File", style="Secondary.TButton", command=self.choose_file
        )
        self.attach_file_btn.pack(side="left")
        self.file_label = ttk.Label(file_row, text="No file attached", style="Muted.TLabel")
        self.file_label.pack(side="left", padx=(10, 0))

        # Status + submit
        self.status_label = ttk.Label(outer, text="", style="Muted.TLabel", wraplength=380)
        self.status_label.pack(fill="x", pady=(0, 8))

        self.submit_btn = ttk.Button(outer, text="Submit Ticket", style="Accent.TButton", command=self.submit)
        self.submit_btn.pack(fill="x")

        win.update_idletasks()
        # Center on screen
        w, h = win.winfo_width(), win.winfo_height()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

        self.subject_entry.focus_set()

    def _on_category_selected(self, event=None):
        name = self.category_var.get()
        self.selected_category_id = next(
            (c.id for c in self.categories if c.name == name), 0
        )

    def choose_file(self):
        path = filedialog.askopenfilename(parent=self.window, title="Select a file to attach")
        if path:
            self.attached_file_path = path
            self.file_label.configure(text=os.path.basename(path))
            self.attach_file_btn.configure(text="Change File")

    def take_screenshot(self):
        # Hide our window so it isn't part of the capture
        self.window.withdraw()
        self.window.update()
        time.sleep(0.3)

        try:
            screenshot = ImageGrab.grab()
        except Exception as exc:
            self.window.deiconify()
            self.window.lift()
            if platform.system() == "Linux" and os.environ.get("XDG_SESSION_TYPE") == "wayland":
                # scrot/maim need X11; grim only works on wlroots compositors
                # (Sway etc.), not GNOME/KDE's Wayland - none of them can be
                # fixed by installing a different tool under a GNOME/KDE
                # Wayland session.
                messagebox.showerror(
                    APP_NAME,
                    "Couldn't capture a screenshot: this session is running Wayland, "
                    "which isn't supported for screen capture on this desktop.\n\n"
                    "Log out, click the gear/settings icon on the login screen, and "
                    "choose \"Ubuntu on Xorg\" (or your distro's X11 session), then try again.",
                )
            else:
                messagebox.showerror(
                    APP_NAME,
                    "Couldn't capture a screenshot on this system:\n" + str(exc),
                )
            return
        finally:
            self.window.deiconify()
            self.window.lift()

        buf = io.BytesIO()
        screenshot.save(buf, format="PNG")
        self.screenshot_bytes = buf.getvalue()

        thumb = screenshot.copy()
        thumb.thumbnail((300, 140))
        self._thumb_image = ImageTk.PhotoImage(thumb)
        self.preview_label.configure(image=self._thumb_image, text="")

        self.screenshot_btn.configure(text="Retake Screenshot")
        self.remove_btn.configure(state="normal")

    def remove_screenshot(self):
        self.screenshot_bytes = None
        self._thumb_image = None
        self.preview_label.configure(image="", text="No screenshot attached")
        self.screenshot_btn.configure(text="Attach Screenshot")
        self.remove_btn.configure(state="disabled")

    def submit(self):
        subject = self.subject_entry.get().strip()
        details = self.details_text.get("1.0", "end").strip()

        if not subject:
            messagebox.showwarning(APP_NAME, "Please enter a subject.")
            return

        self.submit_btn.configure(state="disabled", text="Submitting...")
        self.status_label.configure(text="", style="Muted.TLabel")
        self.window.update()

        threading.Thread(
            target=self._submit_worker, args=(subject, details), daemon=True
        ).start()

    def _submit_worker(self, subject, details):
        try:
            result = self._send_ticket(subject, details)
            self.window.after(0, self._submit_success, result)
        except Exception as exc:
            self.window.after(0, self._submit_error, str(exc))

    def _send_ticket(self, subject, details):
        if self.include_sysinfo_var.get():
            details = details + format_system_info_block(gather_system_info())

        files = []
        if self.screenshot_bytes:
            files.append(("files[]", "screenshot.png", self.screenshot_bytes, "image/png"))
        if self.attached_file_path:
            file_bytes = read_attachment(
                self.attached_file_path,
                protected_paths=[self.config.get("_source"), data_dir(), os.path.dirname(self.config.get("_source") or "")],
            )
            files.append(("files[]", os.path.basename(self.attached_file_path), file_bytes, "application/octet-stream"))

        category_id = self.selected_category_id or None
        # One id per user action: the server de-duplicates retries (including the
        # offline queue's) so a dropped connection never creates two tickets.
        request_id = str(uuid.uuid4())
        try:
            ticket = self.provider.create_ticket(
                subject, details, category_id=category_id, files=files or None, request_id=request_id)
        except requests.exceptions.RequestException:
            save_to_queue(subject, details, category_id, files, request_id=request_id)
            return {"queued": True}

        if ticket.id:
            track_new_ticket(ticket.id, ticket.number, subject)
        return ticket

    def _submit_success(self, result=None):
        if isinstance(result, dict) and result.get("queued"):
            self.status_label.configure(
                text="No connection right now - your ticket was saved and will be "
                     "submitted automatically once you're back online.",
                style="Card.TLabel", foreground="#b45309",
            )
        else:
            self.status_label.configure(text="Ticket submitted successfully!", style="Card.TLabel", foreground="#15803d")
        self.window.update()
        self.window.after(1800, self._close)

    def _submit_error(self, message):
        self.submit_btn.configure(state="normal", text="Submit Ticket")
        self.status_label.configure(text=f"Failed to submit: {message}", style="Card.TLabel", foreground="#b91c1c")

    def _close(self):
        if self.window is not None:
            self.window.destroy()
            self.window = None
        self.screenshot_bytes = None
        self.attached_file_path = None


# ── Recent tickets window ───────────────────────────────────────────────────

class RecentTicketsWindow:
    """Shows the most recent tickets for this client/contact."""

    def __init__(self, root, config, provider, chat_window=None):
        self.root = root
        self.config = config
        self.provider = provider
        self.chat_window = chat_window
        self.window = None
        self.tree = None
        self.status_label = None
        self.tickets_by_id = {}

    def show(self):
        if self.window is not None and self.window.winfo_exists():
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()
            self.refresh()
            return

        win = tk.Toplevel(self.root)
        win.title(f"{APP_NAME} - My Recent Tickets")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        self.window = win

        self._header_keep = []
        build_window_header(
            win, "My Recent Tickets",
            "Double-click a ticket to open live chat" if self.chat_window else "Your latest requests",
            None, accent=self.config.get("accent_color") or ACCENT, keep=self._header_keep,
        )
        outer = ttk.Frame(win, padding=16, style="TFrame")
        outer.pack(fill="both", expand=True)

        columns = ("number", "subject", "status", "created")
        self.tree = ttk.Treeview(outer, columns=columns, show="headings", height=10)
        self.tree.heading("number", text="#")
        self.tree.heading("subject", text="Subject")
        self.tree.heading("status", text="Status")
        self.tree.heading("created", text="Created")
        for col in ("number", "subject", "status", "created"):
            self.tree.heading(col, anchor="w")
        self.tree.column("number", width=100, anchor="w")
        self.tree.column("subject", width=270, anchor="w")
        self.tree.column("status", width=100, anchor="w")
        self.tree.column("created", width=130, anchor="w")
        for name, color in STATUS_COLORS.items():
            self.tree.tag_configure("st_" + name, foreground=color)
        self.tree.tag_configure("odd", background="#f8f9fc")
        tree_frame = tk.Frame(outer, bg=BORDER, padx=1, pady=1)
        tree_frame.pack(fill="both", expand=True)
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(in_=tree_frame, side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tree_frame.lift(); self.tree.lift()
        self.tree.bind("<Double-1>", self._on_double_click)

        self.status_label = ttk.Label(outer, text="Loading...", style="Muted.TLabel")
        self.status_label.pack(anchor="w", pady=(8, 0))

        win.update_idletasks()
        w, h = 640, 440
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

        self.refresh()

    def refresh(self):
        self.status_label.configure(text="Loading...")
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self):
        try:
            tickets = self.provider.list_tickets(limit=15)
        except Exception as exc:
            self.root.after(0, self._show_error, str(exc))
            return

        self.root.after(0, self._populate, tickets)

    def _populate(self, tickets):
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.tickets_by_id = {}
        for i, t in enumerate(tickets):
            self.tickets_by_id[str(t.id)] = t
            tags = []
            status_key = (t.status or "").strip().lower()
            if status_key in STATUS_COLORS:
                tags.append("st_" + status_key)
            if i % 2:
                tags.append("odd")
            self.tree.insert("", "end", iid=str(t.id), tags=tags, values=(
                t.number, t.subject, t.status, (t.created_at or "")[:16],
            ))
        self.status_label.configure(
            text=(f"{len(tickets)} ticket(s)" if tickets else "No tickets yet - submit one from the tray menu.")
        )

    def _show_error(self, message):
        self.status_label.configure(text=f"Couldn't load tickets: {message}")

    def _on_double_click(self, event=None):
        item_id = self.tree.focus()
        if not item_id or not self.chat_window:
            return
        ticket = self.tickets_by_id.get(item_id)
        if not ticket:
            return
        self.chat_window.show(int(item_id), ticket.number, ticket.subject)


class TicketChatWindow:
    """Live chat window for a single ticket. One instance reused per open."""

    POLL_INTERVAL_MS = 15000

    def __init__(self, root, config, provider, mark_read=None):
        self.root = root
        self.config = config
        self.provider = provider
        self.mark_read = mark_read
        self.window = None
        self.text = None
        self.entry = None
        self.send_btn = None
        self.ticket_id = None
        self.ticket_number = None
        self.last_id = 0
        self._poll_job = None
        self._sse_stop_event = None
        self._seen_ids = set()

    def show(self, ticket_id, ticket_number, ticket_subject):
        if self.mark_read:
            self.mark_read()
        self._stop_sse()
        self.ticket_id = ticket_id
        self.ticket_number = ticket_number
        self.last_id = 0
        self._seen_ids = set()

        if self.window is not None and self.window.winfo_exists():
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()
        else:
            win = tk.Toplevel(self.root)
            win.configure(bg=BG)
            win.attributes("-topmost", True)
            win.protocol("WM_DELETE_WINDOW", self._on_close)
            self.window = win

            self._header_keep = []
            header = build_window_header(
                win, "Live Chat", "", None,
                accent=self.config.get("accent_color") or ACCENT, keep=self._header_keep,
            )
            self.title_label = header.winfo_children()[-1].winfo_children()[0]
            outer = ttk.Frame(win, padding=16, style="TFrame")
            outer.pack(fill="both", expand=True)

            text_frame = tk.Frame(outer, bg=BORDER, padx=1, pady=1)
            text_frame.pack(fill="both", expand=True, pady=(0, 10))
            self.text = tk.Text(
                text_frame,
                width=60,
                height=16,
                relief="flat",
                bg=CARD_BG,
                fg="#1f2533",
                font=("Segoe UI", 10),
                padx=8,
                pady=8,
                wrap="word",
                state="disabled",
            )
            self.text.pack(fill="both", expand=True)
            self.text.tag_configure("name_agent", font=("Segoe UI", 9, "bold"), foreground="#374151",
                                    lmargin1=8, lmargin2=8, spacing1=8)
            self.text.tag_configure("msg_agent", background="#eef1f8", lmargin1=8, lmargin2=8,
                                    rmargin=60, spacing1=2, spacing3=4)
            self.text.tag_configure("name_me", font=("Segoe UI", 9, "bold"), foreground="#1d4ed8",
                                    justify="right", rmargin=8, lmargin1=60, lmargin2=60, spacing1=8)
            self.text.tag_configure("msg_me", background="#dbe7ff", justify="right", rmargin=8,
                                    lmargin1=60, lmargin2=60, spacing1=2, spacing3=4)
            self.text.tag_configure("empty", foreground=TEXT_MUTED, justify="center", spacing1=60)

            entry_row = ttk.Frame(outer, style="TFrame")
            entry_row.pack(fill="x")
            self.entry = ttk.Entry(entry_row)
            self.entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
            self.entry.bind("<Return>", lambda event: self.send())
            self.send_btn = ttk.Button(entry_row, text="Send", style="Accent.TButton", command=self.send)
            self.send_btn.pack(side="right")

            win.update_idletasks()
            w, h = 480, 500
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

        self.title_label.configure(text=f"Ticket #{ticket_number}: {ticket_subject}")
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", "Loading messages...\n")
        self.text.configure(state="disabled")

        self._load_history()
        self._schedule_poll()
        self._start_sse()
        self.entry.focus_set()

    def _on_close(self):
        if self._poll_job:
            self.root.after_cancel(self._poll_job)
            self._poll_job = None
        self._stop_sse()
        self.window.withdraw()

    def _start_sse(self):
        if not self.provider.supports_stream_chat:
            return
        self._sse_stop_event = threading.Event()
        threading.Thread(
            target=self.provider.stream_chat,
            args=(self.ticket_id, self._on_sse_message, self._sse_stop_event),
            daemon=True,
        ).start()

    def _stop_sse(self):
        if self._sse_stop_event:
            self._sse_stop_event.set()
            self._sse_stop_event = None

    def _on_sse_message(self, message):
        self.root.after(0, self._append_messages, [message], False)

    def _schedule_poll(self):
        if self._poll_job:
            self.root.after_cancel(self._poll_job)
        self._poll_job = self.root.after(self.POLL_INTERVAL_MS, self._poll)

    def _poll(self):
        threading.Thread(target=self._fetch_messages, args=(self.ticket_id, self.last_id), daemon=True).start()
        self._schedule_poll()

    def _load_history(self):
        threading.Thread(target=self._fetch_messages, args=(self.ticket_id, 0), daemon=True).start()

    def _fetch_messages(self, ticket_id, since_id):
        try:
            messages = self.provider.poll_chat(ticket_id, since_id=since_id)
        except Exception as exc:
            if ticket_id == self.ticket_id and since_id == 0:
                self.root.after(0, self._show_error, str(exc))
            return

        if ticket_id != self.ticket_id:
            return

        self.root.after(0, self._append_messages, messages, since_id == 0)

    def _append_messages(self, messages, replace):
        if not messages and not replace:
            return

        self.text.configure(state="normal")
        if replace:
            self.text.delete("1.0", "end")
            self._seen_ids = set()
            if not messages:
                self.text.insert("end", "No messages yet. Say hello!\n", "empty")

        for m in messages:
            mid = m.id
            if mid and mid in self._seen_ids:
                # Already shown - the periodic poll and the SSE listener can
                # both deliver the same message.
                continue
            if mid:
                self._seen_ids.add(mid)
            sender = m.sender_name or ("Agent" if m.sender_type == "agent" else "You")
            timestamp = (m.created_at or "")[:16]
            side = "agent" if m.sender_type == "agent" else "me"
            self.text.insert("end", f"{sender}  {timestamp}\n", "name_" + side)
            self.text.insert("end", f"{m.message}\n", "msg_" + side)
            self.last_id = max(self.last_id, mid or 0)

        self.text.configure(state="disabled")
        self.text.see("end")

    def _show_error(self, message):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", f"Couldn't load chat: {message}\n")
        self.text.configure(state="disabled")

    def send(self):
        message = self.entry.get().strip()
        if not message or not self.ticket_id:
            return
        self.entry.delete(0, "end")
        threading.Thread(target=self._send_message, args=(self.ticket_id, message), daemon=True).start()

    def _send_message(self, ticket_id, message):
        try:
            self.provider.send_chat_message(ticket_id, message)
        except Exception as exc:
            if ticket_id == self.ticket_id:
                self.root.after(0, lambda: messagebox.showerror(APP_NAME, f"Couldn't send message: {exc}"))
            return

        if ticket_id == self.ticket_id:
            self.root.after(0, self._poll)


def _install_xorg_context_menu(icon, root, menu):
    """Make right-click work on pystray's bare Xlib fallback backend.

    pystray tries backends in this order on Linux: appindicator -> gtk ->
    xorg, and the first two both require the "gi" (PyGObject) bindings. Our
    CI build (.github/workflows/build.yml) apt-installs python3-gi /
    gir1.2-appindicator3-0.1 for the *system* Python, but freezes the app
    with actions/setup-python's isolated interpreter, which never sees those
    bindings - so every shipped Linux build always falls through to
    pystray._xorg, regardless of what the end user has installed. That
    backend (HAS_MENU = False) has no menu support at all and its
    _on_button_press only reacts to the left button, silently dropping every
    right-click. Replace its button handler with one that also opens a real
    Tkinter popup menu on right-click, so the tray behaves like Windows/macOS.
    """
    if not type(icon).__module__.endswith("_xorg"):
        return  # native AppIndicator/GTK backend already has a real menu

    try:
        import Xlib.X
    except ImportError:
        return

    def build_tk_menu(parent, items):
        for entry in items:
            if entry is pystray.Menu.SEPARATOR:
                parent.add_separator()
            elif entry.submenu is not None:
                sub = tk.Menu(parent, tearoff=0)
                build_tk_menu(sub, entry.submenu)
                parent.add_cascade(label=entry.text, menu=sub)
            else:
                parent.add_command(
                    label=entry.text,
                    state="normal" if entry.enabled else "disabled",
                    command=lambda item=entry: item(icon),
                )

    def show_menu():
        tk_menu = tk.Menu(root, tearoff=0)
        build_tk_menu(tk_menu, menu)
        x, y = root.winfo_pointerx(), root.winfo_pointery()
        try:
            tk_menu.tk_popup(x, y)
        finally:
            tk_menu.grab_release()

    def on_button_press(event):
        if event.detail == 1:
            icon()  # left click - same default-item behavior as before
        elif event.detail == 3:
            root.after(0, show_menu)

    try:
        icon._message_handlers[Xlib.X.ButtonPress] = on_button_press
    except AttributeError:
        pass


def run_app(config_paths, icon_path=None):
    """Load config, build the tray icon + menu, and run the event loop.

    config_paths: list of config.json locations to check, in order of
                   preference (app_dir()/config.json is always checked last).
    icon_path:     path to the tray/window icon image (.ico or .png).
    """
    try:
        config = load_config(config_paths)
    except Exception as exc:
        # Show a one-off error dialog; the user has no way to fix config
        # themselves so just inform them clearly.
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, str(exc))
        sys.exit(1)

    provider = get_provider_class()(config)

    root = tk.Tk()
    root.withdraw()  # hidden root, used only to host Toplevel windows
    setup_style(root, accent=config.get("accent_color"))

    tray_image = build_tray_icon_image(icon_path, branding_logo=config.get("branding_logo"))

    def mark_chat_read():
        # `icon` is assigned further down in this function - safe since this
        # is only ever called later, from a chat window the user opened
        # (i.e. well after the tray icon exists and the event loop is running).
        set_chat_unread(icon, False)

    ticket_window = TicketWindow(root, config, provider, icon_path=icon_path)
    chat_window = TicketChatWindow(root, config, provider, mark_read=mark_chat_read) if provider.supports_chat else None
    recent_window = RecentTicketsWindow(root, config, provider, chat_window=chat_window)

    # Load ticket categories in the background so the dropdown is ready
    # by the time the user opens the New Ticket window. No-op for providers
    # that don't support categories.
    def load_categories():
        if not provider.supports_categories:
            return
        try:
            ticket_window.categories = provider.list_categories()
        except Exception:
            pass
    threading.Thread(target=load_categories, daemon=True).start()

    def open_window(icon=None, item=None):
        def show():
            can = getattr(provider, "can_create_tickets", None)
            if can and provider.enrolled() and not can():
                messagebox.showinfo(
                    APP_NAME, "Your IT provider hasn't enabled ticket creation from this PC yet.\n"
                              "Please contact them directly.")
                return
            ticket_window.show()
        root.after(0, show)

    def open_recent(icon=None, item=None):
        root.after(0, recent_window.show)

    def quit_app(icon, item):
        icon.stop()
        root.after(0, root.quit)

    def do_public_ip(icon=None, item=None):
        run_quick_tool(root, "Public IP Address", tool_public_ip)

    def do_ping(icon=None, item=None):
        run_quick_tool(root, "Ping Google", tool_ping)

    def do_printers(icon=None, item=None):
        run_quick_tool(root, "Installed Printers", tool_list_printers)

    def do_spooler(icon=None, item=None):
        run_quick_tool(root, "Restart Print Service", tool_restart_spooler)

    def do_network_adapters(icon=None, item=None):
        open_network_adapters()

    def do_open_portal(icon=None, item=None):
        url = provider.portal_url()
        if url and urlparse(url).scheme in ("http", "https"):
            webbrowser.open(url)

    def _download_asset(url, dest):
        r = fetch_release_url(url, stream=True, timeout=120)
        with r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

    def do_self_update(tag, release):
        """Download and silently install the update for the current platform.

        Windows: Inno Setup silent install — CloseApplications=force handles
        closing this process and relaunching the updated app automatically.

        macOS / Linux: a detached shell script waits for this process to exit,
        replaces the binary/app bundle in-place (tries direct write first, falls
        back to sudo with cached credentials from the RMM tool), then relaunches.
        No password prompts, no browser, no dialogs.
        """
        system = platform.system()
        assets = release.get("assets", [])

        # Scope asset matching to this build's provider so a release tag
        # carrying multiple providers' installers never cross-updates one
        # provider's install with another's binary. The itflow build keeps
        # its original unqualified filenames for upgrade continuity with
        # installs from before multi-provider support existed.
        provider_name = get_provider_name()
        if provider_name == "rivet":
            windows_suffix, macos_suffix, linux_suffix = "setup.exe", "-macos.zip", "-linux.tar.gz"
        else:
            windows_suffix = f"-{provider_name}-setup.exe"
            macos_suffix = f"-{provider_name}-macos.zip"
            linux_suffix = f"-{provider_name}-linux.tar.gz"

        _notify(icon, f"Downloading v{tag} update...", title=APP_NAME)

        try:
            tmp_dir = tempfile.mkdtemp(prefix="rivetpanel_update_")

            if system == "Windows":
                asset = next((a for a in assets if a["name"].lower().endswith(windows_suffix)), None)
                if not asset:
                    root.after(0, lambda: messagebox.showerror(APP_NAME, "Windows installer not found in release."))
                    return
                dest = os.path.join(tmp_dir, asset["name"])
                _download_asset(asset["browser_download_url"], dest)
                _verify_release_asset(assets, asset["name"], dest)
                subprocess.Popen([dest, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
                _notify(icon, f"Installing v{tag} — restarting...", title=APP_NAME)

            elif system == "Darwin" and getattr(sys, "frozen", False):
                # Derive the .app bundle path from the running executable:
                # .app/Contents/MacOS/RivetPanel  ->  up three levels  -> .app
                app_bundle = os.path.normpath(
                    os.path.join(os.path.dirname(sys.executable), "..", "..")
                )
                asset = next((a for a in assets if a["name"].lower().endswith(macos_suffix)), None)
                if not asset:
                    root.after(0, lambda: messagebox.showerror(APP_NAME, "macOS update not found in release."))
                    return
                dest = os.path.join(tmp_dir, asset["name"])
                _download_asset(asset["browser_download_url"], dest)
                _verify_release_asset(assets, asset["name"], dest)
                with zipfile.ZipFile(dest, "r") as z:
                    for name in z.namelist():
                        if ".." not in name and not name.startswith("/"):
                            z.extract(name, tmp_dir)
                new_app = os.path.join(tmp_dir, "RivetPanel.app")
                if not os.path.isdir(new_app):
                    raise RuntimeError("RivetPanel.app not found in update package")

                # Detached script: waits for us to exit, replaces bundle, relaunches
                updater = os.path.join(tmp_dir, "apply_update.sh")
                with open(updater, "w") as f:
                    f.write(f"""#!/bin/bash
sleep 2
rm -rf {shlex.quote(app_bundle)}
cp -R {shlex.quote(new_app)} {shlex.quote(app_bundle)} 2>/dev/null \\
  || sudo cp -R {shlex.quote(new_app)} {shlex.quote(app_bundle)}
open -a {shlex.quote(app_bundle)}
""")
                os.chmod(updater, 0o755)
                subprocess.Popen(["bash", updater])
                _notify(icon, f"Installing v{tag} — restarting...", title=APP_NAME)
                time.sleep(1)
                icon.stop()
                root.after(0, root.quit)

            elif system == "Linux" and getattr(sys, "frozen", False):
                current_binary = sys.executable  # e.g. /opt/rivetpanel/RivetPanel
                asset = next((a for a in assets if a["name"].lower().endswith(linux_suffix)), None)
                if not asset:
                    root.after(0, lambda: messagebox.showerror(APP_NAME, "Linux update not found in release."))
                    return
                dest = os.path.join(tmp_dir, asset["name"])
                _download_asset(asset["browser_download_url"], dest)
                _verify_release_asset(assets, asset["name"], dest)
                with tarfile.open(dest, "r:gz") as t:
                    for member in t.getmembers():
                        if ".." not in member.name and not member.name.startswith("/"):
                            t.extract(member, tmp_dir)
                new_binary = os.path.join(tmp_dir, "RivetPanel")
                if not os.path.isfile(new_binary):
                    raise RuntimeError("RivetPanel binary not found in update package")
                os.chmod(new_binary, 0o755)

                # Detached script: waits for our PID to exit, replaces binary, relaunches
                updater = os.path.join(tmp_dir, "apply_update.sh")
                with open(updater, "w") as f:
                    f.write(f"""#!/bin/bash
PID={os.getpid()}
while kill -0 "$PID" 2>/dev/null; do sleep 0.5; done
cp -f {shlex.quote(new_binary)} {shlex.quote(current_binary)} 2>/dev/null \\
  || sudo cp -f {shlex.quote(new_binary)} {shlex.quote(current_binary)}
chmod +x {shlex.quote(current_binary)}
nohup {shlex.quote(current_binary)} >/dev/null 2>&1 &
disown
""")
                os.chmod(updater, 0o755)
                subprocess.Popen(["bash", updater])
                _notify(icon, f"Installing v{tag} — restarting...", title=APP_NAME)
                time.sleep(1)
                icon.stop()
                root.after(0, root.quit)

            else:
                # Dev mode (not frozen) — just notify, can't self-replace interpreter
                _notify(icon, f"{APP_NAME} v{tag} available.", title=APP_NAME)

        except Exception as exc:
            root.after(0, lambda: messagebox.showerror(APP_NAME, f"Update failed: {exc}"))

    def do_check_update(icon=None, item=None):
        def worker():
            result = check_for_update()
            if not result:
                root.after(0, lambda: messagebox.showinfo(APP_NAME, f"You're up to date (v{VERSION})."))
                return
            tag, release = result
            def ask():
                if messagebox.askyesno(APP_NAME, f"A new version ({tag}) is available. Download and install now?"):
                    threading.Thread(target=do_self_update, args=(tag, release), daemon=True).start()
            root.after(0, ask)
        threading.Thread(target=worker, daemon=True).start()

    quick_tools_menu = pystray.Menu(
        pystray.MenuItem("My Public IP", do_public_ip),
        pystray.MenuItem("Ping Google", do_ping),
        pystray.MenuItem("List Printers", do_printers),
        pystray.MenuItem("Restart Print Service", do_spooler),
        pystray.MenuItem("Network Adapters", do_network_adapters),
    )

    menu_items = [
        pystray.MenuItem("New Ticket", open_window, default=True),
        pystray.MenuItem("My Recent Tickets", open_recent),
    ]
    if provider.portal_url():
        menu_items.append(pystray.MenuItem("Open Client Portal", do_open_portal))
    if config.get("support_phone"):
        phone = str(config["support_phone"])
        menu_items.append(pystray.MenuItem(
            f"Call Support ({phone})", lambda icon=None, item=None: webbrowser.open("tel:" + "".join(c for c in phone if c.isdigit() or c == "+"))))
    if config.get("support_email"):
        email = str(config["support_email"])
        menu_items.append(pystray.MenuItem(
            "Email Support", lambda icon=None, item=None: webbrowser.open("mailto:" + email)))
    menu_items += [
        pystray.MenuItem("Quick Tools", quick_tools_menu),
        pystray.MenuItem("Check for Updates", do_check_update),
        pystray.MenuItem("Exit", quit_app),
    ]
    menu = pystray.Menu(*menu_items)

    icon = pystray.Icon(APP_NAME, tray_image, APP_NAME, menu)
    icon._base_image = tray_image  # read by set_chat_unread() to build/clear the badge
    _install_xorg_context_menu(icon, root, menu)

    global _chat_listener_provider, _chat_listener_icon
    _chat_listener_provider = provider
    _chat_listener_icon = icon

    tray_thread = threading.Thread(target=icon.run, daemon=True)
    tray_thread.start()

    # Background loop: retry queued (offline) tickets, poll for status
    # changes, and keep one live-chat SSE listener running per tracked
    # ticket (poll_ticket_chat is a 300s fallback in case Redis/SSE is down).
    def background_loop():
        time.sleep(5)
        _ensure_chat_listeners()
        while True:
            try:
                flush_queue(config, provider)
            except Exception:
                pass
            try:
                poll_ticket_updates(config, provider, icon)
            except Exception:
                pass
            try:
                poll_ticket_chat(config, provider, icon)
            except Exception:
                pass
            try:
                _ensure_chat_listeners()
            except Exception:
                pass
            time.sleep(300)
    threading.Thread(target=background_loop, daemon=True).start()

    # Silent startup update check — auto-installs on all platforms, no dialogs
    if config.get("check_for_updates", True):
        def startup_check():
            result = check_for_update()
            if not result:
                return
            tag, release = result
            do_self_update(tag, release)
        threading.Thread(target=startup_check, daemon=True).start()

    root.mainloop()
