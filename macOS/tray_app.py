"""
ITPanel Pro - macOS menu bar app

Lets end users submit an ITFlow support ticket (with an optional full-screen
screenshot) in a few clicks, without logging in to the client portal.

Configuration is read from (in order of preference):
  1. /Library/Application Support/ITPanelPro/config.json
     (system-wide, set by install.sh / RMM)
  2. ~/Library/Application Support/ITPanelPro/config.json
  3. config.json next to this script / inside the .app bundle

See config.json for the expected fields. Shared UI/logic lives in
../common/core.py.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))

import core  # noqa: E402


def config_paths():
    folder = core.app_folder_name()
    return [
        f"/Library/Application Support/{folder}/config.json",
        os.path.expanduser(f"~/Library/Application Support/{folder}/config.json"),
    ]


def icon_path():
    return os.path.join(core.app_dir(), "assets", "icon.png")


if __name__ == "__main__":
    core.run_app(config_paths(), icon_path=icon_path())
