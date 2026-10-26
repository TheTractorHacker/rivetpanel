#!/bin/bash
# RivetPanel - Linux installer
#
# Installs the prebuilt RivetPanel binary to /opt/rivetpanel,
# writes /etc/rivetpanel/config.json, and registers it as an
# XDG autostart application for all users.
#
# Usage (run as root, e.g. via RMM):
#   ./install.sh <rivet_base_url> <api_key> <client_id> [contact_id] [priority]
#
# Example:
#   ./install.sh https://psa.example.com XXXXXXXXXXXXXXXX 5 12 Medium
#
# Re-running this script upgrades an existing install: it stops any running
# instance, replaces the binary, and (unless new values are passed) keeps the
# existing config.json. If a previous "ITFlow Quick Ticket" install is found,
# it is removed and its config.json is migrated.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/rivetpanel"
CONFIG_DIR="/etc/rivetpanel"
CONFIG_PATH="$CONFIG_DIR/config.json"
AUTOSTART_DIR="/etc/xdg/autostart"

OLD_INSTALL_DIR="/opt/itflow-quick-ticket"
OLD_CONFIG_PATH="/etc/itflow-quick-ticket/config.json"
OLD_AUTOSTART_FILE="$AUTOSTART_DIR/itflow-quick-ticket.desktop"

RIVET_BASE_URL="${1:-}"
API_KEY="${2:-}"
# The enrollment token can also come from the environment, which keeps it out of the
# process list (`ps`) while the installer runs: RIVETPANEL_ENROLLMENT_TOKEN=rvtp1... ./install.sh <url>
if [ -z "$API_KEY" ] && [ -n "${RIVETPANEL_ENROLLMENT_TOKEN:-}" ]; then API_KEY="$RIVETPANEL_ENROLLMENT_TOKEN"; fi
CLIENT_ID="${3:-}"
CONTACT_ID="${4:-}"
PRIORITY="${5:-Medium}"

# Stop any running instance so the binary can be replaced cleanly.
pkill -f "$INSTALL_DIR/RivetPanel" 2>/dev/null || true

# Best-effort: install a screenshot helper for the "Attach Screenshot"
# feature (Pillow's ImageGrab.grab() shells out to scrot on X11). Only
# helps under an X11 session - GNOME/KDE Wayland sessions can't capture
# with any of these tools, the app shows guidance to switch sessions.
if command -v apt-get >/dev/null 2>&1 && ! command -v scrot >/dev/null 2>&1; then
    apt-get update -qq || true
    apt-get install -y scrot || echo "Warning: failed to install scrot; screenshot capture may not work." >&2
fi

# Remove a previous "ITFlow Quick Ticket" install, migrating its config first.
if [ -d "$OLD_INSTALL_DIR" ] || [ -f "$OLD_AUTOSTART_FILE" ]; then
    echo "Removing previous ITFlow Quick Ticket install..."
    pkill -f "$OLD_INSTALL_DIR/ITFlowQuickTicket" 2>/dev/null || true
    rm -f "$OLD_AUTOSTART_FILE"
    rm -rf "$OLD_INSTALL_DIR"
fi

if [ -f "$OLD_CONFIG_PATH" ] && [ ! -f "$CONFIG_PATH" ]; then
    mkdir -p "$CONFIG_DIR"
    cp "$OLD_CONFIG_PATH" "$CONFIG_PATH"
    rm -rf "/etc/itflow-quick-ticket"
fi

mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$AUTOSTART_DIR"

if [ -f "$SCRIPT_DIR/dist/RivetPanel" ]; then
    install -m 755 "$SCRIPT_DIR/dist/RivetPanel" "$INSTALL_DIR/RivetPanel"
elif [ -f "$SCRIPT_DIR/RivetPanel" ]; then
    install -m 755 "$SCRIPT_DIR/RivetPanel" "$INSTALL_DIR/RivetPanel"
else
    echo "RivetPanel binary not found next to install.sh (expected ./RivetPanel or ./dist/RivetPanel)" >&2
    exit 1
fi

mkdir -p "$INSTALL_DIR/assets"
if [ -f "$SCRIPT_DIR/assets/icon.png" ]; then
    install -m 644 "$SCRIPT_DIR/assets/icon.png" "$INSTALL_DIR/assets/icon.png"
fi

install -m 644 "$SCRIPT_DIR/rivetpanel.desktop" "$AUTOSTART_DIR/rivetpanel.desktop"

lock_config() {
    # The app runs as whichever user is logged in (XDG autostart), not as
    # root, so 600 root:root would lock it out - but the API key must not be
    # world-readable either. Use a dedicated group: root-owned (so users
    # cannot redirect the app at another server), readable only by members.
    # Members: the user who ran sudo, anyone in RIVETPANEL_USERS (space
    # separated), and every user with a current login session.
    getent group rivetpanel >/dev/null || groupadd --system rivetpanel
    chown root:rivetpanel "$CONFIG_PATH"
    chmod 640 "$CONFIG_PATH"
    for u in ${SUDO_USER:-} ${RIVETPANEL_USERS:-} $(loginctl list-users --no-legend 2>/dev/null | awk '{print $2}'); do
        [ "$u" != "root" ] && id "$u" >/dev/null 2>&1 && usermod -aG rivetpanel "$u"
    done
    echo "Config readable by group 'rivetpanel'. Other users: usermod -aG rivetpanel <user> (then re-login)."
}

if [ -n "$RIVET_BASE_URL" ] && [[ "$API_KEY" == rvtp1.* ]]; then
    # Device-token install: the second argument is an enrollment token, no
    # shared API key is stored. The server knows the client and contact.
    python3 -c "
import json, sys
with open(sys.argv[3], 'w') as f:
    json.dump({'rivet_base_url': sys.argv[1], 'enrollment_token': sys.argv[2]}, f, indent=4)
    f.write('\n')
" "$RIVET_BASE_URL" "$API_KEY" "$CONFIG_PATH"
    lock_config
elif [ -n "$RIVET_BASE_URL" ] && [ -n "$API_KEY" ] && [ -n "$CLIENT_ID" ]; then
    if [ -n "$CONTACT_ID" ]; then
        CONTACT_JSON="$CONTACT_ID"
    else
        CONTACT_JSON="null"
    fi

    python3 -c "
import json, sys
data = {
    'rivet_base_url': sys.argv[1],
    'api_key': sys.argv[2],
    'client_id': int(sys.argv[3]),
    'contact_id': int(sys.argv[4]) if sys.argv[4] != 'null' else None,
    'priority': sys.argv[5],
}
with open(sys.argv[6], 'w') as f:
    json.dump(data, f, indent=4)
    f.write('\n')
" "$RIVET_BASE_URL" "$API_KEY" "$CLIENT_ID" "$CONTACT_JSON" "$PRIORITY" "$CONFIG_PATH"
    lock_config
elif [ ! -f "$CONFIG_PATH" ]; then
    echo "No config.json exists and no connection settings were passed." >&2
    echo "Usage: $0 <rivet_base_url> <api_key> <client_id> [contact_id] [priority]" >&2
    exit 1
else
    echo "Keeping existing $CONFIG_PATH"
fi

echo "RivetPanel installed to $INSTALL_DIR"
echo "It will start automatically on next login for all users."

# Launch now for the current graphical session, if any.
if [ -n "${XDG_CURRENT_DESKTOP:-}" ] || [ -n "${DISPLAY:-}" ]; then
    nohup "$INSTALL_DIR/RivetPanel" >/dev/null 2>&1 &
    disown || true
    echo "Launched RivetPanel"
fi
