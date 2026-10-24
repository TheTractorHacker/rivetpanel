<p align="center"><img src="assets/logo.png" width="128" alt="RivetPanel logo"></p>

# RivetPanel

[![Latest release](https://img.shields.io/github/v/release/TheTractorHacker/rivetpanel?include_prereleases&label=release)](https://github.com/TheTractorHacker/rivetpanel/releases/latest)
[![Build apps and installers](https://github.com/TheTractorHacker/rivetpanel/actions/workflows/build.yml/badge.svg)](https://github.com/TheTractorHacker/rivetpanel/actions/workflows/build.yml)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-blue)

A lightweight tray/menu bar app that lets end users submit an RivetIT/RivetMSP support
ticket — with an optional screenshot or file attachment — in a few clicks,
with no portal login. Intended for mass deployment via RMM, with all
connection settings (RivetIT/RivetMSP URL, API key, client ID, etc.) configured at
install time so the end user never sees credentials.

## Screenshots

<table>
<tr>
<td align="center" width="34%"><b>New Ticket</b></td>
<td align="center" width="33%"><b>My Recent Tickets</b></td>
<td align="center" width="33%"><b>Live Chat</b></td>
</tr>
<tr>
<td><img src="docs/screenshots/ticket_window.png" width="100%"></td>
<td><img src="docs/screenshots/recent_tickets.png" width="100%"></td>
<td><img src="docs/screenshots/chat_window.png" width="100%"></td>
</tr>
</table>

## Security model: per-device enrollment

RivetPanel no longer needs a shared admin API key on every PC. An administrator mints an
**enrollment token** for a client (and optionally a person) on the RivetIT/RivetMSP Deploy page and passes it
to the installer (`/EnrollmentToken=rvtp1...`, `deploy_rivetpanel.ps1 -EnrollmentToken`, or
`install.sh <url> <rvtp1...token>`). On first run the app swaps it for its **own device credential**, stored in the user's
data folder (Windows: DPAPI-protected). The server scopes that credential to one client; it can create and read tickets
and chat for that client and nothing else, and an administrator can revoke it per device. A fleet token (up to N machines,
short-lived) covers an RMM mass deployment. Installs that still carry the legacy `api_key` keep working until redeployed.
The server side is the RivetCore Panel API ([PANEL_API.md](https://github.com/TheTractorHacker/rivet-core/blob/main/docs/panel/PANEL_API.md)).

## Features

- New Ticket window with subject, description, screenshot, and file
  attachment
- Auto-attaches system info (hostname, OS, user, local IP) to help
  diagnose issues faster
- Auto-links the ticket to the matching asset in RivetIT/RivetMSP by hostname
- Ticket category dropdown (when configured in RivetIT/RivetMSP)
- Offline queue — tickets submitted while offline are sent automatically
  once connectivity returns
- "My Recent Tickets" tray menu view
- "Open Client Portal" button to open the RivetIT/RivetMSP client portal in the browser
- Desktop notifications when a submitted ticket's status changes
- Silent auto-update check against GitHub releases, with a one-click
  download-and-install update on Windows
- Per-MSP branding (accent color + logo)
- "Quick Tools" troubleshooting menu: public IP, ping google.com, list
  printers, restart print spooler/CUPS, open network adapter settings

## Platforms

| Platform | Status | Docs |
|----------|--------|------|
| Windows  | Available | [Windows/README.md](Windows/README.md) |
| macOS    | Available | [macOS/README.md](macOS/README.md) |
| Linux    | Available | [Linux/README.md](Linux/README.md) |

## Requirements

Requires **[RivetIT or RivetMSP](https://github.com/TheTractorHacker/RivetIT) v2.11.32 or later** —
this is the version that added multipart/form-data attachment support to
`POST /api/v1/tickets` (used to submit the optional screenshot alongside the
ticket).

## Repo layout

- `common/` — shared tray app UI/logic (ttk ticket window, config loading,
  tray icon wiring) used by every platform's `tray_app.py`
- `Windows/` — tray app entry point, PyInstaller spec, branded icon, and
  TacticalRMM deploy script
- `installer/` — Inno Setup installer that prompts for per-install
  configuration and writes `config.json`
- `macOS/` — menu bar app entry point, PyInstaller spec/bundle, LaunchAgent,
  and `install.sh`
- `Linux/` — tray app entry point, PyInstaller spec, XDG autostart entry,
  and `install.sh`
- `.github/workflows/` — CI that builds all three platforms and attaches
  the binaries/installers to GitHub releases
