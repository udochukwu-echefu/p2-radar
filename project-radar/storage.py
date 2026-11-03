"""Storage sweep for P2 Radar: space that rebuilds itself (dependency folders,
package caches, app caches, developer tool data) and home-folder clutter
(tool folders that only hold a copied skill). Same item shape as
downloads.scan, so the dashboard reviews and trashes it the same way, with
the same undo. Locations differ per platform; the logic doesn't.
"""

import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import downloads
import osutil

HOME = Path.home()
MIN_CACHE = 50 * 1024 * 1024
STALE_DEPS_DAYS = 60
SKILL_HOMES = [HOME / ".claude/skills", HOME / ".agents/skills"]

if osutil.IS_WIN:
    LOCAL = Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData/Local")
    ROAMING = Path(os.environ.get("APPDATA") or HOME / "AppData/Roaming")
    # (path, note, pre-ticked)
    PACKAGE_CACHES = [(LOCAL / "npm-cache", "npm cache", True), (LOCAL / "pip/Cache", "pip cache", True),
                      (LOCAL / "Yarn/Cache", "Yarn cache", True), (LOCAL / "pnpm-cache", "pnpm cache", True),
                      (HOME / ".nuget/packages", "NuGet packages, restored on the next build", False),
                      (HOME / ".cache/huggingface", "Hugging Face models, re-downloaded on use", False)]
    APP_CACHES = [(LOCAL / "Google/Chrome/User Data/Default/Cache", "Chrome cache", False),
                  (LOCAL / "Microsoft/Edge/User Data/Default/Cache", "Edge cache", False),
                  (LOCAL / "Mozilla/Firefox/Profiles", None, False),
                  (ROAMING / "Code/Cache", "VS Code cache", True), (ROAMING / "Code/CachedData", "VS Code cache", True),
                  (LOCAL / "Spotify/Data", "Spotify cache", False), (LOCAL / "CrashDumps", "Crash reports", True)]
    DEV_DATA = [(HOME / ".gradle/caches", "Gradle caches", False), (LOCAL / "Microsoft/VisualStudio/Packages/_Instances", None, False)]
    DEV_TITLE = ("Developer tool data", "Build caches and crash data that tools regenerate.")
    DOCKER = LOCAL / "Docker/wsl"
else:
    CACHES = HOME / "Library/Caches"
    PACKAGE_CACHES = [(HOME / ".npm/_cacache", "npm cache", True), (CACHES / "pnpm", "pnpm cache", True),
                      (HOME / ".cache/pip", "pip cache", True), (HOME / ".cache/huggingface", "Hugging Face models, re-downloaded on use", False)]
    APP_CACHES = []   # every folder in ~/Library/Caches, see _caches()
    dev = HOME / "Library/Developer"
    DEV_DATA = [(dev / "Xcode/DerivedData", "Build output", True), (dev / "Xcode/iOS DeviceSupport", "Symbols for devices you've connected", False),
                (dev / "Xcode/Archives", "App archives (keep if you ship builds)", False), (dev / "CoreSimulator/Caches", "Simulator caches", True)]
    DEV_TITLE = ("Xcode build data", "Build output and device support files Xcode regenerates.")
    DOCKER = HOME / "Library/Containers/com.docker.docker"

# Caches that only hold re-downloadable packages or updater payloads (macOS ~/Library/Caches names).
SAFE_CACHES = {"Homebrew", "pip", "pip-tools", "pypoetry", "yarn", "go-build", "node-gyp", "typescript",
               "@opencodedesktop-updater", "electron", "electron-builder", "ms-playwright-go"}

CATEGORIES = {
    "deps":     ("Project dependencies", "node_modules folders. npm install brings them back. Projects idle for 2 months are ticked."),
    "npm":      ("Package manager caches", "Downloaded packages that npm and others fetch again when needed."),
    "caches":   ("App caches", "Caches apps rebuild on their own. Quit the app first for the best result."),
    "xcode":    DEV_TITLE,
    "toolfolders": ("Unused AI-tool folders", "Folders that only hold a copy of a skill, created by a skills installer."),
}


def _item(path, category, note="", suggest=False, name=None, mtime=None):
    p = Path(path)
    try:
        mt = int(mtime if mtime is not None else p.stat().st_mtime)
    except OSError:
        mt = int(time.time())
    return {"path": str(p), "name": name or p.name, "is_dir": p.is_dir(), "size": 0, "mtime": mt,
            "category": category, "note": note, "suggest": suggest}


def _deps(repos):
    out = []
    for r in repos or []:
        idle = (time.time() - (r.get("last_active") or 0)) / 86400
        for nm in r.get("node_modules", []):
            rel = os.path.relpath(nm, r["path"])
            note = f"{r['name']} · active {int(idle)} days ago" if idle >= 1 else f"{r['name']} · active today"
            out.append(_item(nm, "deps", note, suggest=idle > STALE_DEPS_DAYS,
                             name=f"{r['name']}{os.sep}{rel}", mtime=r.get("last_active")))
    return out


def _listed(entries, category):
    return [_item(p, category, note or "", suggest=tick, name=osutil.display(p)) for p, note, tick in entries if note and Path(p).is_dir()]


def _caches():
    if osutil.IS_WIN:
        return _listed(APP_CACHES, "caches")
    out = []
    try:
        entries = list(os.scandir(CACHES))
    except OSError:
        return out
    for e in entries:
        if not e.is_dir(follow_symlinks=False) or e.name.startswith(("com.apple.", ".")):
            continue
        safe = e.name in SAFE_CACHES or e.name.endswith(("-updater", ".ShipIt"))
        out.append(_item(e.path, "caches", "Re-downloaded when needed" if safe else "", suggest=safe))
    return out


def _tool_folders():
    """~/.<tool> folders whose only content is skills/ copied by an installer."""
    known = set()
    for h in SKILL_HOMES:
        try:
            known |= {e.name for e in os.scandir(h) if e.is_dir()}
        except OSError:
            pass
    apps = {k.replace("-", "") for k in osutil.installed_apps()}
    out = []
    for e in os.scandir(HOME):
        if not e.name.startswith(".") or not e.is_dir(follow_symlinks=False) or e.name in (".claude", ".agents", ".Trash"):
            continue
        try:
            kids = [k for k in os.listdir(e.path) if k not in (".DS_Store", "desktop.ini")]
        except OSError:
            continue
        if kids != ["skills"]:
            continue
        try:
            skills = sorted(k for k in os.listdir(os.path.join(e.path, "skills")) if not k.startswith("."))
        except OSError:
            continue
        tool = e.name[1:].lower()
        in_use = tool.replace("-", "") in apps or shutil.which(tool) is not None
        covered = all(s in known for s in skills)
        note = (f"Holds {', '.join(skills) or 'nothing'}"
                + (" · also in ~/.claude or ~/.agents" if covered and skills else "")
                + (f" · {tool} looks installed" if in_use else ""))
        out.append(_item(e.path, "toolfolders", note, suggest=covered and not in_use, name=osutil.display(e.path)))
    return out


def docker_info():
    """Docker's disk use, as information only (Radar doesn't delete images)."""
    if not DOCKER.exists():
        return None
    info = {"disk": osutil.dir_size(DOCKER), "running": False, "reclaimable": None}
    if shutil.which("docker"):
        try:
            r = subprocess.run(["docker", "system", "df", "--format", "{{json .}}"], capture_output=True, text=True, timeout=8, **osutil.quiet())
            if r.returncode == 0:
                info["running"] = True
                rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
                info["reclaimable"] = ", ".join(f"{x.get('Type')}: {x.get('Reclaimable')}" for x in rows)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass
    return info


def scan(repos):
    t0 = time.time()
    items = _deps(repos) + _listed(PACKAGE_CACHES, "npm") + _caches() + _listed(DEV_DATA, "xcode") + _tool_folders()
    with ThreadPoolExecutor(max_workers=6) as ex:
        sizes = list(ex.map(lambda i: osutil.dir_size(i["path"]), items))
    for i, s in zip(items, sizes):
        i["size"] = s
    items = [i for i in items if i["category"] == "toolfolders" or i["size"] >= (MIN_CACHE if i["category"] == "caches" else 1)]
    items.sort(key=lambda i: -i["size"])
    return {
        "folder": str(HOME), "display": "This computer",
        "scanned_at": int(time.time()), "seconds": round(time.time() - t0, 1),
        "categories": [{"id": k, "title": v[0], "blurb": v[1]} for k, v in CATEGORIES.items()],
        "items": items, "can_undo": bool(downloads.last_sweep()),
        "docker": docker_info(),
    }
