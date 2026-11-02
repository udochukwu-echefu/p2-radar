"""Storage sweep for Radar: space that rebuilds itself (dependency folders,
package caches, Xcode build output) and home-folder clutter (tool folders
that only hold a copied skill). Same item shape as downloads.scan, so the
dashboard reviews and trashes it the same way, with the same undo.
"""

import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import downloads

HOME = Path.home()
CACHES = HOME / "Library/Caches"
MIN_CACHE = 50 * 1024 * 1024
STALE_DEPS_DAYS = 60

# Caches that only hold re-downloadable packages or updater payloads.
SAFE_CACHES = {"Homebrew", "pip", "pip-tools", "pypoetry", "yarn", "go-build", "node-gyp", "typescript",
               "@opencodedesktop-updater", "electron", "electron-builder", "ms-playwright-go"}
SKILL_HOMES = [HOME / ".claude/skills", HOME / ".agents/skills"]

CATEGORIES = {
    "deps":     ("Project dependencies", "node_modules folders. npm install brings them back. Projects idle for 2 months are ticked."),
    "npm":      ("Package manager caches", "Downloaded packages that npm and others fetch again when needed."),
    "caches":   ("App caches", "Caches apps rebuild on their own. Quit the app first for the best result."),
    "xcode":    ("Xcode build data", "Build output and device support files Xcode regenerates."),
    "toolfolders": ("Unused AI-tool folders", "Folders that only hold a copy of a skill, created by a skills installer."),
}


def _size(path):
    return downloads.size_of(str(path))


def _item(path, category, note="", suggest=False, name=None, mtime=None):
    p = Path(path)
    try:
        mt = int(mtime if mtime is not None else p.stat().st_mtime)
    except OSError:
        mt = int(time.time())
    return {"path": str(p), "name": name or p.name, "is_dir": p.is_dir(), "size": 0, "mtime": mt,
            "category": category, "note": note, "suggest": suggest}


def _display(p):
    s = str(p)
    return "~/" + os.path.relpath(s, HOME) if s.startswith(str(HOME)) else s


def _deps(repos):
    out = []
    for r in repos or []:
        idle = (time.time() - (r.get("last_active") or 0)) / 86400
        for nm in r.get("node_modules", []):
            rel = os.path.relpath(nm, r["path"])
            note = f"{r['name']} · active {int(idle)} days ago" if idle >= 1 else f"{r['name']} · active today"
            out.append(_item(nm, "deps", note, suggest=idle > STALE_DEPS_DAYS,
                             name=f"{r['name']}/{rel}", mtime=r.get("last_active")))
    return out


def _caches():
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


def _package_caches():
    out = []
    for path, note in [(HOME / ".npm/_cacache", "npm cache"), (HOME / "Library/Caches/pnpm", "pnpm cache"),
                       (HOME / ".cache/pip", "pip cache"), (HOME / ".cache/huggingface", "Hugging Face models, re-downloaded on use")]:
        if path.is_dir():
            out.append(_item(path, "npm", note, suggest=path.name != "huggingface", name=_display(path)))
    return out


def _xcode():
    out = []
    dev = HOME / "Library/Developer"
    for path, note in [(dev / "Xcode/DerivedData", "Build output"), (dev / "Xcode/iOS DeviceSupport", "Symbols for devices you've connected"),
                       (dev / "Xcode/Archives", "App archives (keep if you ship builds)"), (dev / "CoreSimulator/Caches", "Simulator caches")]:
        if path.is_dir():
            out.append(_item(path, "xcode", note, suggest=path.name in ("DerivedData", "Caches"), name=_display(path)))
    return out


def _installed_names():
    names = set()
    for d in ("/Applications", str(HOME / "Applications")):
        try:
            names |= {e.name[:-4].lower().replace(" ", "") for e in os.scandir(d) if e.name.endswith(".app")}
        except OSError:
            pass
    return names


def _tool_folders():
    """~/.<tool> folders whose only content is skills/ copied by an installer."""
    known = set()
    for h in SKILL_HOMES:
        try:
            known |= {e.name for e in os.scandir(h) if e.is_dir()}
        except OSError:
            pass
    apps = _installed_names()
    out = []
    for e in os.scandir(HOME):
        if not e.name.startswith(".") or not e.is_dir(follow_symlinks=False) or e.name in (".claude", ".agents", ".Trash"):
            continue
        try:
            kids = [k for k in os.listdir(e.path) if k != ".DS_Store"]
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
        out.append(_item(e.path, "toolfolders", note, suggest=covered and not in_use, name="~/" + e.name))
    return out


def docker_info():
    """Docker's disk use, as information only (Radar doesn't delete images)."""
    raw = HOME / "Library/Containers/com.docker.docker"
    if not raw.exists():
        return None
    info = {"disk": _size(raw), "running": False, "reclaimable": None}
    if shutil.which("docker"):
        try:
            r = subprocess.run(["docker", "system", "df", "--format", "{{json .}}"], capture_output=True, text=True, timeout=8)
            if r.returncode == 0:
                info["running"] = True
                rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
                info["reclaimable"] = ", ".join(f"{x.get('Type')}: {x.get('Reclaimable')}" for x in rows)
        except (subprocess.TimeoutExpired, ValueError):
            pass
    return info


def scan(repos):
    t0 = time.time()
    items = _deps(repos) + _package_caches() + _caches() + _xcode() + _tool_folders()
    with ThreadPoolExecutor(max_workers=6) as ex:
        sizes = list(ex.map(lambda i: _size(i["path"]), items))
    for i, s in zip(items, sizes):
        i["size"] = s
    items = [i for i in items if i["category"] == "toolfolders" or i["size"] >= (MIN_CACHE if i["category"] == "caches" else 1)]
    items.sort(key=lambda i: -i["size"])
    return {
        "folder": str(HOME), "display": "This Mac",
        "scanned_at": int(time.time()), "seconds": round(time.time() - t0, 1),
        "categories": [{"id": k, "title": v[0], "blurb": v[1]} for k, v in CATEGORIES.items()],
        "items": items, "can_undo": bool(downloads.last_sweep()),
        "docker": docker_info(),
    }
