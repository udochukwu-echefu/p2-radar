"""MagSafe charging light for P2 Radar (MacBooks with MagSafe 3).

The Mac app bundles a small helper, magsafe-led, that reads the light (SMC key
ACLC) as the user. Changing it needs a root daemon, which the app installs once
with the admin password (mac-app/MagSafe.swift). After that the wanted mode
lives in a file the user owns; the daemon re-reads it every second.
"""
import os
import subprocess
from pathlib import Path

import osutil

MODES = ("off", "green", "orange", "system")
PLIST = "/Library/LaunchDaemons/com.udo.radar.magsafe-led.plist"
MODE_FILE = osutil.data_dir() / "magsafe-mode"


def helper():
    """The helper inside the running app, or None outside the Mac app."""
    app = os.environ.get("RADAR_APP_BIN")   # .../P2 Radar.app/Contents/MacOS/Radar
    if not (osutil.IS_MAC and app):
        return None
    h = Path(app).parent.parent / "Resources" / "magsafe-led"
    return h if h.exists() else None


def wanted():
    try:
        w = MODE_FILE.read_text().strip()
    except OSError:
        return None
    return w if w in MODES else None


def state():
    h = helper()
    if not h:
        return {"supported": False}
    try:
        r = subprocess.run([str(h), "status"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return {"supported": False}
    if r.returncode != 0:   # no ACLC key: this Mac has no MagSafe 3 light
        return {"supported": False}
    words = r.stdout.split()
    return {"supported": True, "now": words[0] if words else "other",
            "installed": os.path.exists(PLIST), "wanted": wanted()}


def set_wanted(mode):
    if mode not in MODES:
        return {"ok": False, "error": "Unknown mode."}
    MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MODE_FILE.with_name(MODE_FILE.name + ".tmp")
    tmp.write_text(mode + "\n")
    os.replace(tmp, MODE_FILE)
    return {"ok": True, **state()}
