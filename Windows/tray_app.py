"""
RivetPanel - Windows tray app

Lets end users submit an RivetIT/RivetMSP support ticket (with an optional full-screen
screenshot) in a few clicks, without logging in to the client portal.

Configuration is read from (in order of preference):
  1. %ProgramData%\\RivetPanel\\config.json
  2. config.json next to this script / the packaged .exe

See config.json for the expected fields. Shared UI/logic lives in
../common/core.py.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))

import core  # noqa: E402


def config_paths():
    paths = []
    program_data = os.environ.get("ProgramData")
    if program_data:
        paths.append(os.path.join(program_data, core.app_folder_name(), "config.json"))
    return paths


def icon_path():
    return os.path.join(core.app_dir(), "assets", "icon.ico")


if __name__ == "__main__":
    core.run_app(config_paths(), icon_path=icon_path())
