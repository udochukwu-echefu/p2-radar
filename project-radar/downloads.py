"""Downloads and Desktop sweep for P2 Radar: sort a folder into things you can
safely bin (installers for apps you already have, duplicate copies,
unfinished downloads, archives you've already unpacked, old screenshots) and
things worth a look (big videos, files that look like secrets). Nothing moves
until you pick it, and everything goes to the Trash or Recycle Bin, with undo.
"""
import fnmatch
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

import osutil

HOME = Path.home()
# State lives outside the code so the code can sit read-only inside the app.
DATA_DIR = osutil.data_dir()
LOG_FILE = DATA_DIR / "sweep-log.json"

MAC_INSTALLERS = {".dmg", ".pkg", ".mpkg", ".xip"}
WIN_INSTALLERS = {".exe", ".msi", ".msix", ".msixbundle", ".appx", ".appxbundle"}
OTHER_INSTALLERS = {".ipk", ".apk", ".appimage", ".deb", ".rpm"}
INSTALLER_EXT = MAC_INSTALLERS | WIN_INSTALLERS | OTHER_INSTALLERS
OWN_INSTALLERS = WIN_INSTALLERS if osutil.IS_WIN else MAC_INSTALLERS if osutil.IS_MAC else {".appimage", ".deb", ".rpm"}
ARCHIVE_EXT = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar"}
VIDEO_EXT = {".mkv", ".mp4", ".mov", ".avi", ".webm", ".m4v", ".wmv"}
PARTIAL_EXT = {".crdownload", ".part", ".partial", ".download", ".opdownload"}
TOOL_HINTS = {"macos", "darwin", "windows", "win", "linux", "arm", "aarch", "amd", "x"}  # as tokens: arm64 -> "arm"
SENSITIVE = ["client_secret*.json", "credentials*.json", "*service-account*.json", "*.pem", "*.key",
             "*.p12", "*.pfx", ".env", "*.env", "id_rsa*", "id_ed25519*", "*.ovpn", "*.mobileprovision"]
BIG_VIDEO = 200 * 1024 * 1024
BIG_ARCHIVE = 100 * 1024 * 1024

CATEGORIES = {
    "partial":   ("Unfinished downloads", "These never finished downloading and can't be opened."),
    "duplicate": ("Duplicate copies", "Exact copies of another file here. The original is kept."),
    "installer": ("Installers", "Disk images and installers. Once the app is installed you don't need them."),
    "extracted": ("Archives you've unpacked", "The unpacked folder is right next to it."),
    "screenshot": ("Screenshots & recordings", "Screenshots and screen recordings. Ones older than a month are ticked."),
    "sensitive": ("Looks like a secret", "Keys or credentials shouldn't sit here. Move them somewhere safe, or bin them if you're done."),
    "video":     ("Big videos", "Large video files. Keep what you still want to watch."),
    "archive":   ("Big archives", "Large compressed files."),
    "project":   ("Project folders", "Folders tracked in git. Manage them in Projects; they're never ticked here."),
    "other":     ("Everything else", "Not sorted into a group. Pick anything you don't need."),
}
# macOS: "Screenshot 2026-10-29 at 22.19.36.png"; Windows Snipping Tool: "Screenshot 2026-11-03 001122.png"
SCREENSHOT = re.compile(r"^(Screenshot|Screen Shot|Screen Recording|CleanShot)[ _]", re.I)
OLD_SCREENSHOT_DAYS = 30


# ---------------------------------------------------------------- scanning

def tokens(name):
    return re.findall(r"[a-z]+", name.lower())


installed_apps = osutil.installed_apps
size_of = osutil.dir_size


def file_hash(path, limit=None):
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as f:
        read = 0
        while chunk := f.read(1 << 20):
            h.update(chunk)
            read += len(chunk)
            if limit and read >= limit:
                break
    return h.hexdigest()


def folder_fingerprint(path):
    """Same relative paths and same file sizes, plus content hashes for small
    folders. Good enough to spot 'folder' vs 'folder 2'."""
    h = hashlib.blake2b(digest_size=20)
    total = 0
    entries = []
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(root, f)
            try:
                s = os.lstat(p).st_size
            except OSError:
                continue
            entries.append((os.path.relpath(p, path), s, p))
            total += s
    for rel, s, p in entries:
        h.update(f"{rel}\0{s}\0".encode())
        if total < 100 * 1024 * 1024:
            try:
                h.update(file_hash(p).encode())
            except OSError:
                pass
    return h.hexdigest()


COPY_MARK = re.compile(r"( \(\d+\)| copy( \d+)?| \d+)$|^download( \(\d+\))?$", re.I)


def keeper_rank(item):
    """Lower is a better candidate to keep: a clean name, short, oldest."""
    stem = Path(item["name"]).stem
    messy = bool(COPY_MARK.search(stem)) or len(stem) > 60
    return (messy, len(item["name"]), item["mtime"])


def classify(folder):
    apps = installed_apps()
    items = []
    for e in os.scandir(folder):
        if e.name.startswith("."):
            continue
        st = e.stat(follow_symlinks=False)
        is_dir = e.is_dir(follow_symlinks=False)
        repo = is_dir and os.path.isdir(os.path.join(e.path, ".git"))
        items.append({
            "path": e.path, "name": e.name, "is_dir": is_dir,
            "size": size_of(e.path), "mtime": int(st.st_mtime),
            "category": "project" if repo else "other",
            "note": "Tracked in git" if repo else "", "suggest": False,
        })
    dirs = [i for i in items if i["is_dir"]]

    # Duplicates: same size first (cheap), then same content. Git projects are
    # never treated as duplicates here; the Projects tab handles copies safely.
    by_size = {}
    for i in items:
        if i["size"] > 0 and i["category"] != "project":
            by_size.setdefault((i["is_dir"], i["size"]), []).append(i)
    for (is_dir, _), group in by_size.items():
        if len(group) < 2:
            continue
        by_hash = {}
        for i in group:
            try:
                key = folder_fingerprint(i["path"]) if is_dir else file_hash(i["path"])
            except OSError:
                continue
            by_hash.setdefault(key, []).append(i)
        for same in by_hash.values():
            if len(same) < 2:
                continue
            same.sort(key=keeper_rank)
            for dup in same[1:]:
                dup.update(category="duplicate", suggest=True, note=f"Same as {same[0]['name']}")

    for i in items:
        if i["category"] != "other":
            continue
        name = i["name"]
        # Folders like gh_2.92.0_macOS_amd64 aren't name + extension.
        ext = "" if i["is_dir"] and not name.endswith(".app") else Path(name).suffix.lower()
        t = tokens(name[: -len(ext)] if ext else name)
        first = t[0] if t else ""
        if ext in PARTIAL_EXT or name.lower().startswith("unconfirmed "):
            i.update(category="partial", suggest=True)
        elif any(fnmatch.fnmatch(name.lower(), p) for p in SENSITIVE):
            i.update(category="sensitive")
        elif SCREENSHOT.match(name) and not i["is_dir"]:
            days = int((time.time() - i["mtime"]) / 86400)
            i.update(category="screenshot", suggest=days > OLD_SCREENSHOT_DAYS,
                     note=f"{days} days old" if days > OLD_SCREENSHOT_DAYS else "Recent")
        elif ext in INSTALLER_EXT or (i["is_dir"] and name.endswith(".app")):
            app = apps.get(first)
            if app:
                i.update(category="installer", suggest=True, note=f"{app} is already installed")
            elif ext not in OWN_INSTALLERS and not name.endswith(".app"):
                i.update(category="installer", note="Installer for another device or OS")
            elif name.endswith(".app"):
                i.update(category="installer", note="An app sitting in Downloads. Move it to Applications if you use it")
            else:
                i.update(category="installer", note="App doesn't look installed. Keep it if you still need to install it")
        elif i["is_dir"] and first and set(t[1:]) & TOOL_HINTS and shutil.which(first):
            i.update(category="installer", suggest=True, note=f"{first} is already installed at {shutil.which(first)}")
        elif ext in ARCHIVE_EXT:
            sib = next((d for d in dirs if d is not i and tokens(d["name"])[:1] == t[:1] and t), None)
            if sib:
                i.update(category="extracted", suggest=True, note=f"Unpacked as {sib['name']}")
            elif i["size"] > BIG_ARCHIVE:
                i.update(category="archive")
        elif ext in VIDEO_EXT and i["size"] > BIG_VIDEO:
            i.update(category="video")
    return items


def scan(folder):
    t0 = time.time()
    items = classify(folder)
    items.sort(key=lambda i: -i["size"])
    return {
        "folder": str(folder),
        "display": osutil.display(folder),
        "scanned_at": int(time.time()),
        "seconds": round(time.time() - t0, 1),
        "categories": [{"id": k, "title": v[0], "blurb": v[1]} for k, v in CATEGORIES.items()],
        "items": items,
        "can_undo": bool(last_sweep()),
    }


# ---------------------------------------------------------------- trash + undo

to_trash = osutil.trash   # macOS Trash or Windows Recycle Bin; returns an undo token


def read_log():
    try:
        return json.loads(LOG_FILE.read_text())
    except (OSError, ValueError):
        return []


def last_sweep():
    log = read_log()
    return log[-1] if log and not log[-1].get("undone") else None


def sweep(paths):
    moved, failed = [], []
    for p in paths:
        try:
            size = size_of(p)
            moved.append({"from": p, "to": to_trash(p), "size": size})
        except Exception as e:
            failed.append({"path": p, "error": str(e)[:200]})
    if moved:
        log = read_log()
        log.append({"at": int(time.time()), "items": moved})
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text(json.dumps(log, indent=2))
    freed = sum(m["size"] for m in moved)
    bin_ = osutil.LABELS["trash"]
    msg = f"Moved {len(moved)} item(s), {human(freed)}, to the {bin_}. Empty the {bin_} to free the space."
    if failed:
        msg += f" {len(failed)} couldn't be moved: " + "; ".join(f"{os.path.basename(f['path'])} ({f['error']})" for f in failed)
    return {"ok": not failed, "message": msg, "moved": len(moved)}


def undo():
    log = read_log()
    if not log or log[-1].get("undone"):
        return {"ok": False, "error": "Nothing to undo."}
    back, missing = 0, 0
    for m in log[-1]["items"]:
        try:
            restored = osutil.untrash(m["to"], m["from"])
        except (OSError, RuntimeError):
            restored = False
        if restored:
            back += 1
        else:
            missing += 1
    log[-1]["undone"] = True
    LOG_FILE.write_text(json.dumps(log, indent=2))
    msg = f"Put back {back} item(s)."
    if missing:
        msg += f" {missing} weren't in the {osutil.LABELS['trash']} any more (already emptied?)."
    return {"ok": True, "message": msg}


# ---------------------------------------------------------------- CLI

def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def summary(data):
    items = data["items"]
    total = sum(i["size"] for i in items)
    sug = [i for i in items if i["suggest"]]
    print(f"\n{data['display']}: {len(items)} items, {human(total)}\n")
    for cid, (title, _) in CATEGORIES.items():
        group = [i for i in items if i["category"] == cid]
        if not group or cid == "other":
            continue
        print(f"  {title}  ({human(sum(i['size'] for i in group))})")
        for i in group:
            mark = "x" if i["suggest"] else " "
            note = f"  - {i['note']}" if i["note"] else ""
            print(f"   [{mark}] {human(i['size']):>9}  {i['name'][:60]}{note}")
        print()
    print(f"  Suggested for the Trash: {len(sug)} items, {human(sum(i['size'] for i in sug))}  [x]")
    print("  Nothing has been moved. Review and sweep with:  python3 radar.py serve\n")
