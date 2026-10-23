"""
ITPanel Pro - Linux tray app

Lets end users submit an ITFlow support ticket (with an optional full-screen
screenshot) in a few clicks, without logging in to the client portal.

Configuration is read from (in order of preference):
  1. /etc/itpanel-pro/config.json   (system-wide, set by install.sh / RMM)
  2. ~/.config/itpanel-pro/config.json
  3. config.json next to this script / the packaged binary

See config.json for the expected fields. Shared UI/logic lives in
../common/core.py.

Notes:
  - The tray icon requires a system tray host. On GNOME this means the
    "AppIndicator and KStatusNotifierItem Support" extension (or a similar
    indicator extension) must be enabled.
  - Screenshot capture uses Pillow's ImageGrab, which on Linux shells out to
    `scrot`, `grim`, `maim`, or `slurp` (one of these must be installed).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))

import core  # noqa: E402


def config_paths():
    folder = core.app_folder_slug()
    return [
        f"/etc/{folder}/config.json",
        os.path.expanduser(f"~/.config/{folder}/config.json"),
    ]


def icon_path():
    return os.path.join(core.app_dir(), "assets", "icon.png")


if __name__ == "__main__":
    core.run_app(config_paths(), icon_path=icon_path())
