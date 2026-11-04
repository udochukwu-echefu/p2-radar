#!/usr/bin/env python3
"""P2 Radar: find every git repo on this Mac and show which ones have
work that isn't safe yet (uncommitted, unpushed, or no remote at all), and
sweep ~/Downloads of installers, duplicates and other leftovers.

Usage:
  python3 radar.py               # project summary in the terminal
  python3 radar.py downloads     # Downloads dry run in the terminal
  python3 radar.py serve         # dashboard at http://127.0.0.1:8765
  python3 radar.py --json        # full project scan as JSON
  python3 radar.py notify        # macOS notification with the at-risk count

Standard library only. Nothing leaves this machine unless you press Push or
"Back up to GitHub", and nothing in Downloads moves until you pick it.
"""

import argparse
import fnmatch
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import convert
import downloads
import leaks
import magsafe
import osutil
import storage

HOME = Path.home()
HERE = Path(__file__).resolve().parent
DATA_DIR = downloads.DATA_DIR

# Folders never worth descending into while looking for repos.
SKIP_DIRS = {
    "node_modules", "Library", "Applications", "Music", "Pictures", "Movies", "AppData", "$Recycle.Bin",
    "Public", "venv", "__pycache__", "Pods", "DerivedData", "site-packages",
    "dist", "build", "target", ".next", ".nuxt", ".turbo",
}
MAX_DEPTH = 9

# Files a WIP snapshot should never sweep into a commit.
SENSITIVE_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
    "client_secret*.json", "credentials*.json", "*service-account*.json",
    "*serviceaccount*.json", ".npmrc", ".pypirc", "*.keystore",
]
SENSITIVE_OK = {".env.example", ".env.sample", ".env.template", ".env.defaults"}
JUNK_PARTS = {"node_modules", ".venv", "venv", "__pycache__", ".DS_Store"}
LARGE_BYTES = 50 * 1024 * 1024
PREVIEW_LIMIT = 5000


# ---------------------------------------------------------------- scanning

def find_repos(roots):
    """Walk the roots and return (repos, blocked). A repo is any directory
    containing a .git folder; worktrees and submodules (where .git is a file)
    are skipped on purpose. `blocked` lists top folders macOS refused to let
    us read, which usually means the terminal lacks Files & Folders access."""
    found, blocked = [], []

    def walk(path, depth):
        try:
            entries = list(os.scandir(path))
        except PermissionError:
            if depth <= 1:
                blocked.append(path)
            return
        except (FileNotFoundError, NotADirectoryError, OSError):
            return
        names = {e.name for e in entries}
        if ".git" in names and os.path.isdir(os.path.join(path, ".git")):
            found.append(path)
        if depth >= MAX_DEPTH:
            return
        for e in entries:
            if not e.is_dir(follow_symlinks=False):
                continue
            n = e.name
            if n.startswith(".") or n in SKIP_DIRS or n.endswith(".app"):
                continue
            walk(e.path, depth + 1)

    for root in roots:
        walk(str(root), 0)
    return sorted(set(found)), sorted(blocked)


# GIT_OPTIONAL_LOCKS=0: `git status` would otherwise create and delete .git/index.lock,
# which the app's file watcher reports as a change, which runs `git status` again.
GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}


def git(repo, *args, timeout=30, input=None):
    r = subprocess.run(
        ["git", "-C", repo, "-c", "core.quotePath=false", *args],
        capture_output=True, text=True, timeout=timeout, input=input, encoding="utf-8", errors="replace",
        env=GIT_ENV, **osutil.quiet(),
    )
    return r.returncode, r.stdout, r.stderr


def normalize_remote(url):
    if not url:
        return None
    u = url.strip().lower()
    u = re.sub(r"^(https?://|ssh://|git://)", "", u)
    u = re.sub(r"^[^@/]+@", "", u)
    u = u.replace(":", "/").rstrip("/")
    return re.sub(r"\.git$", "", u)


dir_size = osutil.dir_size


def find_node_modules(repo, max_depth=4):
    out = []

    def walk(p, d):
        try:
            for e in os.scandir(p):
                if not e.is_dir(follow_symlinks=False):
                    continue
                if e.name == "node_modules":
                    out.append(e.path)
                elif d < max_depth and not e.name.startswith(".") and e.name not in SKIP_DIRS:
                    walk(e.path, d + 1)
        except OSError:
            pass

    walk(repo, 0)
    return out


def inspect(repo, with_sizes=True):
    info = {
        "path": repo,
        "name": os.path.basename(repo),
        "display": osutil.display(repo),
        "branch": None, "upstream": None, "ahead": 0, "behind": 0,
        "has_commits": True, "remote": None, "remote_url": None,
        "staged": 0, "modified": 0, "untracked": 0, "conflicts": 0, "dirty": 0,
        "unpushed": 0, "stashes": 0, "last_commit": None, "last_active": None,
        "root": None, "node_modules": [], "node_modules_bytes": 0, "error": None,
    }
    try:
        code, out, err = git(repo, "status", "--porcelain=v2", "--branch")
        if code != 0:
            info["error"] = err.strip()[:300]
            return finish(info)
        dirty_paths = []
        for line in out.splitlines():
            if line.startswith("# branch.oid"):
                info["has_commits"] = line.split()[-1] != "(initial)"
            elif line.startswith("# branch.head"):
                h = line.split(" ", 2)[2]
                info["branch"] = None if h == "(detached)" else h
            elif line.startswith("# branch.upstream"):
                info["upstream"] = line.split(" ", 2)[2]
            elif line.startswith("# branch.ab"):
                m = re.match(r"# branch.ab \+(\d+) -(\d+)", line)
                if m:
                    info["ahead"], info["behind"] = int(m[1]), int(m[2])
            elif line.startswith(("1 ", "2 ")):
                xy = line.split(" ")[1]
                if xy[0] != ".":
                    info["staged"] += 1
                if xy[1] != ".":
                    info["modified"] += 1
                parts = line.split(" ", 8 if line[0] == "1" else 9)
                dirty_paths.append(parts[-1].split("\t")[0])
            elif line.startswith("u "):
                info["conflicts"] += 1
                dirty_paths.append(line.split(" ", 10)[-1])
            elif line.startswith("? "):
                info["untracked"] += 1
                dirty_paths.append(line[2:])
        info["dirty"] = sum(1 for l in out.splitlines() if l[:2] in ("1 ", "2 ", "u ", "? "))

        code, out, _ = git(repo, "remote")
        remotes = out.split()
        if remotes:
            info["remote"] = "origin" if "origin" in remotes else remotes[0]
            _, url, _ = git(repo, "remote", "get-url", info["remote"])
            info["remote_url"] = url.strip() or None

        if info["has_commits"]:
            _, out, _ = git(repo, "log", "-1", "--format=%ct")
            info["last_commit"] = int(out.strip()) if out.strip() else None
            _, out, _ = git(repo, "rev-list", "--max-parents=0", "HEAD")
            roots = sorted(out.split())
            info["root"] = roots[0] if roots else None
            if info["remote"]:
                _, out, _ = git(repo, "rev-list", "--count", "--branches", "--not", "--remotes")
                info["unpushed"] = int(out.strip() or 0)
            else:
                _, out, _ = git(repo, "rev-list", "--count", "--all")
                info["unpushed"] = int(out.strip() or 0)
        _, out, _ = git(repo, "stash", "list")
        info["stashes"] = len(out.splitlines())

        # Last activity: newest of the last commit and any changed file.
        latest = info["last_commit"] or 0
        for p in dirty_paths[:300]:
            try:
                latest = max(latest, int(os.lstat(os.path.join(repo, p.rstrip("/"))).st_mtime))
            except OSError:
                pass
        info["last_active"] = latest or None

        if with_sizes:
            nms = find_node_modules(repo)
            info["node_modules"] = nms
            info["node_modules_bytes"] = sum(dir_size(p) for p in nms)
    except subprocess.TimeoutExpired:
        info["error"] = "git timed out"
    except Exception as e:  # keep one broken repo from sinking the scan
        info["error"] = str(e)[:300]
    return finish(info)


def finish(info):
    """Work out how exposed this repo is. Higher risk sorts first."""
    reasons = []
    risk = 0
    if info["error"]:
        reasons.append("error")
    if not info["remote"]:
        if info["has_commits"] or info["dirty"]:
            reasons.append("local-only")
            risk += 100
    else:
        if info["unpushed"]:
            reasons.append("unpushed")
            risk += 40
        if info["has_commits"] and not info["upstream"] and info["branch"]:
            reasons.append("no-upstream")
            risk += 10
    if info["dirty"]:
        reasons.append("uncommitted")
        risk += 30
    if info["conflicts"]:
        reasons.append("conflicts")
        risk += 20
    if info["stashes"]:
        reasons.append("stashed")
        risk += 5
    risk += min(info["dirty"], 200) / 10
    # Something untouched for a year is less likely to be work you'd miss.
    if info["last_active"] and time.time() - info["last_active"] > 365 * 86400:
        risk *= 0.3
        reasons.append("stale")
    info["reasons"] = reasons
    info["risk"] = risk
    info["at_risk"] = any(r in reasons for r in ("local-only", "unpushed", "uncommitted", "conflicts"))
    return info


def group_duplicates(repos):
    """Repos with the same first commit or the same remote are copies of each
    other. Union-find over both keys."""
    parent = list(range(len(repos)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen = {}
    for i, r in enumerate(repos):
        for key in (("root", r["root"]), ("remote", normalize_remote(r["remote_url"]))):
            if key[1] is None:
                continue
            if key in seen:
                parent[find(i)] = find(seen[key])
            else:
                seen[key] = i
    groups = {}
    for i in range(len(repos)):
        groups.setdefault(find(i), []).append(i)
    gid = 0
    for members in groups.values():
        for i in members:
            repos[i]["dup_group"] = None
            repos[i]["dup_of"] = []
        if len(members) < 2:
            continue
        gid += 1
        newest = max(members, key=lambda i: repos[i]["last_active"] or 0)
        for i in members:
            repos[i]["dup_group"] = gid
            repos[i]["dup_newest"] = i == newest
            repos[i]["dup_of"] = [repos[j]["display"] for j in members if j != i]
    return repos


def scan(roots, with_sizes=True):
    t0 = time.time()
    paths, blocked = find_repos(roots)
    with ThreadPoolExecutor(max_workers=8) as ex:
        repos = list(ex.map(lambda p: inspect(p, with_sizes), paths))
    group_duplicates(repos)
    repos.sort(key=lambda r: (-r["risk"], -(r["last_active"] or 0)))
    return {
        "scanned_at": int(time.time()),
        "seconds": round(time.time() - t0, 1),
        "roots": [str(r) for r in roots],
        "blocked": [osutil.display(b) for b in blocked],
        "platform": osutil.info(),
        "repos": repos,
        "gh": bool(shutil.which("gh")),
        "editor": bool(shutil.which("code")),
    }


# ---------------------------------------------------------------- actions

def is_sensitive(path):
    name = os.path.basename(path)
    if name in SENSITIVE_OK:
        return False
    return any(fnmatch.fnmatch(name.lower(), p) for p in SENSITIVE_PATTERNS)


def commit_preview(repo):
    """List what a WIP snapshot would commit, and what it would leave out."""
    code, out, err = git(repo, "status", "--porcelain=v1", "-z", "-uall", timeout=60)
    if code != 0:
        return {"ok": False, "error": err.strip()}
    items = out.split("\0")
    files, i = [], 0
    while i < len(items):
        entry = items[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if xy[0] in "RC":
            i += 1  # skip the original path of a staged rename/copy
        files.append({"path": path, "status": xy.strip() or xy})
        if len(files) > PREVIEW_LIMIT:
            return {"ok": False, "error": f"More than {PREVIEW_LIMIT} changed files. "
                    "This usually means a .gitignore is missing (node_modules, venv, build output). "
                    "Add one, then try again."}
    include, exclude = [], []
    for f in files:
        p = f["path"]
        reason = None
        parts = set(Path(p).parts)
        if is_sensitive(p):
            reason = "looks like a secret"
        elif parts & JUNK_PARTS:
            reason = "dependency or cache folder"
        else:
            try:
                if os.path.getsize(os.path.join(repo, p)) > LARGE_BYTES:
                    reason = "over 50 MB"
            except OSError:
                pass  # deleted file: fine to stage the deletion
        (exclude if reason else include).append({**f, "reason": reason} if reason else f)
    return {"ok": True, "include": include, "exclude": exclude}


def wip_commit(repo):
    prev = commit_preview(repo)
    if not prev["ok"]:
        return prev
    if not prev["include"]:
        return {"ok": False, "error": "Nothing safe to commit: every changed file was held back."}
    spec = "\0".join(f["path"] for f in prev["include"]) + "\0"
    code, _, err = git(repo, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul", input=spec, timeout=120)
    if code != 0:
        return {"ok": False, "error": err.strip()}
    # Anything already staged that we decided to hold back gets unstaged.
    for f in prev["exclude"]:
        git(repo, "reset", "-q", "--", f["path"])
    msg = f"WIP snapshot from P2 Radar ({datetime.now():%Y-%m-%d %H:%M})"
    code, out, err = git(repo, "commit", "-m", msg, timeout=120)
    if code != 0:
        return {"ok": False, "error": (err or out).strip()}
    held = len(prev["exclude"])
    note = f"Committed {len(prev['include'])} file(s)."
    if held:
        note += f" Held back {held} file(s) that look like secrets, caches or large files."
    return {"ok": True, "message": note}


def push(repo):
    _, out, _ = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if out.strip():
        code, out, err = git(repo, "push", timeout=180)
    else:
        _, remotes, _ = git(repo, "remote")
        remote = "origin" if "origin" in remotes.split() else (remotes.split() or [None])[0]
        if not remote:
            return {"ok": False, "error": "This repo has no remote. Create a GitHub repo first."}
        code, out, err = git(repo, "push", "-u", remote, "HEAD", timeout=180)
    if code != 0:
        return {"ok": False, "error": (err or out).strip()}
    return {"ok": True, "message": "Pushed."}


def create_github(repo, name):
    if not shutil.which("gh"):
        return {"ok": False, "error": "GitHub CLI (gh) isn't installed."}
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-") or "project"
    r = subprocess.run(
        ["gh", "repo", "create", name, "--private", "--source", repo, "--remote", "origin", "--push"],
        capture_output=True, text=True, timeout=300, **osutil.quiet(),
    )
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or r.stdout).strip()}
    return {"ok": True, "message": f"Created private repo {name} and pushed. {r.stdout.strip()}"}


def trash_node_modules(repo):
    """Through the shared sweep, so Undo in Cleanup can put them back."""
    found = find_node_modules(repo)
    if not found:
        return {"ok": False, "error": "No node_modules folders found."}
    res = downloads.sweep(found)
    res["message"] += " Run npm install to get them back."
    return res


def open_in(repo, where):
    {"finder": osutil.open_path, "editor": osutil.open_editor, "terminal": osutil.open_terminal}.get(where, osutil.open_path)(repo)
    return {"ok": True}


# ---------------------------------------------------------------- copies + bulk

def copy_analysis(group):
    """For each copy of a project: the commits no other copy has."""
    commits = {}
    for r in group:
        _, out, _ = git(r["path"], "rev-list", "--all", timeout=60)
        commits[r["path"]] = set(out.split())
    result = []
    for r in group:
        others = set().union(*(c for p, c in commits.items() if p != r["path"]))
        unique = commits[r["path"]] - others
        subjects = []
        if unique:
            _, out, _ = git(r["path"], "log", "--no-walk=unsorted", "--format=%h%x09%ct%x09%s", *sorted(unique)[:12])
            for line in out.splitlines():
                h, ct, s = (line.split("\t", 2) + ["", ""])[:3]
                subjects.append({"hash": h, "time": int(ct or 0), "subject": s})
        result.append({"path": r["path"], "display": r["display"], "unique": len(unique), "commits": subjects,
                       "dirty": r["dirty"], "stashes": r["stashes"],
                       "contained": not unique and not r["dirty"] and not r["stashes"]})
    return result


def copy_fetch(dest, src):
    """Bring another copy's branches into this one as radar/<copy>/<branch>. Nothing is merged."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(src)).strip("-") or "copy"
    code, out, err = git(dest, "fetch", "--no-tags", src, f"+refs/heads/*:refs/heads/radar/{slug}/*", timeout=180)
    if code != 0:
        return {"ok": False, "error": (err or out).strip()}
    _, out, _ = git(dest, "branch", "--list", f"radar/{slug}/*", "--format=%(refname:short)")
    branches = out.split()
    return {"ok": True, "message": f"Fetched {plural_s(len(branches), 'branch', 'branches')} into this copy: "
                                   f"{', '.join(branches[:4])}. Merge them when you're ready; nothing was changed in your working files."}


def plural_s(n, one, many):
    return f"{n} {one if n == 1 else many}"


def copy_trash(path):
    """Move a copy to the Trash, but only when every commit in it also exists
    in another copy and it has no uncommitted or stashed work."""
    with State.lock:
        r = next((x for x in State.data["repos"] if x["path"] == path), None)
        group = [x for x in State.data["repos"] if r and x["dup_group"] == r["dup_group"]] if r else []
    if not r or len(group) < 2:
        return {"ok": False, "error": "This isn't one of several copies."}
    a = next(x for x in copy_analysis([inspect(g["path"], with_sizes=False) for g in group]) if x["path"] == path)
    if not a["contained"]:
        return {"ok": False, "error": "This copy has work the others don't. Bring it over first."}
    res = downloads.sweep([path])
    if res["ok"]:
        with State.lock:
            State.data["repos"] = [x for x in State.data["repos"] if x["path"] != path]
            group_duplicates(State.data["repos"])
        res["message"] = f"Moved {r['display']} to the Trash. Undo it from Cleanup if you change your mind."
    return res


def bulk_preview(paths):
    out = []
    for p in paths:
        r = repo_by_path(p)
        prev = commit_preview(p) if r and r["dirty"] else {"ok": True, "include": [], "exclude": []}
        out.append({"path": p, "ok": prev["ok"], "error": prev.get("error"),
                    "include": len(prev.get("include", [])), "exclude": len(prev.get("exclude", []))})
    return {"ok": True, "items": out}


def bulk_backup(items):
    """Snapshot (if needed), then push or create a private GitHub repo, per project."""
    results = []
    for it in items:
        p = it.get("path")
        r = repo_by_path(p)
        if not r:
            results.append({"path": p, "ok": False, "error": "Unknown repo"})
            continue
        steps, ok, err = [], True, None
        try:
            if r["dirty"]:
                res = wip_commit(p)
                ok, err = res["ok"], res.get("error")
                steps.append("snapshot")
            if ok:
                fresh = inspect(p, with_sizes=False)
                if not fresh["remote"]:
                    res = create_github(p, it.get("name") or r["name"])
                    steps.append("github")
                elif fresh["unpushed"] or not fresh["upstream"]:
                    res = push(p)
                    steps.append("push")
                else:
                    res = {"ok": True}
                ok, err = res["ok"], res.get("error")
        except subprocess.TimeoutExpired:
            ok, err = False, "Timed out"
        updated, _ = refresh_one(p)
        hub.broadcast("repo", updated)
        results.append({"path": p, "ok": ok, "error": err, "steps": steps, "repo": updated})
    done = sum(1 for x in results if x["ok"])
    return {"ok": done == len(results), "results": results,
            "message": f"Backed up {done} of {len(results)} projects." + ("" if done == len(results) else " See the ones marked with an error.")}


# ---------------------------------------------------------------- live updates

class Hub:
    """Server-sent events: the page subscribes to /api/events."""
    def __init__(self):
        self.clients, self.lock = [], threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=200)
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def broadcast(self, event, data):
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        with self.lock:
            for q in self.clients:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass


hub = Hub()
CACHE_FILE = DATA_DIR / "scan-cache.json"
HIDDEN_FILE = DATA_DIR / "hidden.json"


def load_hidden():
    try:
        return set(json.loads(HIDDEN_FILE.read_text()))
    except (OSError, ValueError):
        return set()


def set_hidden(path, hidden):
    h = load_hidden()
    (h.add if hidden else h.discard)(path)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HIDDEN_FILE.write_text(json.dumps(sorted(h), indent=2))
    return {"ok": True}


class State:
    lock = threading.Lock()
    ready = threading.Event()
    data = None
    roots = []
    dl_dir = osutil.user_folder("Downloads")
    sweeps, sweep_locks = {}, {k: threading.Lock() for k in ("downloads", "desktop", "storage")}
    leaks, outputs = None, set()
    git_sigs = {}
    token = secrets.token_urlsafe(24)
    app_secret = os.environ.get("RADAR_APP_SECRET", "")
    port = 8765


def save_cache(data):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def load_cache():
    try:
        data = json.loads(CACHE_FILE.read_text())
        data["cached"] = True
        return data
    except (OSError, ValueError):
        return None


def full_scan():
    data = scan(State.roots)
    with State.lock:
        State.data = data
    save_cache(data)
    State.ready.set()
    State.sweeps.pop("storage", None)   # depends on the project list
    hub.broadcast("scan", {"at": data["scanned_at"]})
    return data


def get_scan(refresh=False):
    if refresh:
        full_scan()
    State.ready.wait()
    with State.lock:
        return {**State.data, "hidden": sorted(load_hidden())}


def repo_by_path(path):
    with State.lock:
        return next((r for r in (State.data or {}).get("repos", []) if r["path"] == path), None)


def refresh_one(path, with_sizes=True):
    """Re-inspect one repo. Returns (repo, changed)."""
    fresh = inspect(path, with_sizes=with_sizes)
    with State.lock:
        repos = State.data["repos"]
        before = next((dict(r) for r in repos if r["path"] == path), None)
        for i, r in enumerate(repos):
            if r["path"] == path:
                if not with_sizes:   # keep the last known dependency sizes
                    fresh["node_modules"], fresh["node_modules_bytes"] = r["node_modules"], r["node_modules_bytes"]
                repos[i] = fresh
        group_duplicates(repos)
        updated = next((r for r in repos if r["path"] == path), fresh)
        data = State.data
    changed = before != updated
    if changed:
        save_cache(data)
    return updated, changed


def push_repo(path):
    """Refresh a repo and tell the dashboard, but only when something it shows changed."""
    repo, changed = refresh_one(path, with_sizes=False)
    if changed:
        hub.broadcast("repo", repo)


def repo_for(path):
    with State.lock:
        best = None
        for r in (State.data or {}).get("repos", []):
            rp = r["path"]
            if (path == rp or path.startswith(rp + os.sep)) and (best is None or len(rp) > len(best)):
                best = rp
        return best


def on_changed(paths):
    """Called by Radar.app's file watcher: refresh just the repos and folders that changed."""
    repos, kinds = set(), set()
    for p in paths:
        if "/node_modules/" in p or "/.git/objects/" in p or (".git" in p and p.endswith(".lock")):
            continue
        if p.startswith(str(State.dl_dir)):
            kinds.add("downloads")
        if p.startswith(str(osutil.user_folder("Desktop")) + os.sep):
            kinds.add("desktop")
        rp = repo_for(p)
        if rp:
            repos.add(rp)
    for rp in repos:
        try:
            push_repo(rp)
        except subprocess.TimeoutExpired:
            pass
    for k in kinds:
        State.sweeps.pop(k, None)
        hub.broadcast("sweep", {"kind": k})
    return {"ok": True, "repos": len(repos), "kinds": sorted(kinds)}


def git_signature(repo):
    sig = []
    for rel in (".git/index", ".git/HEAD", ".git/logs/HEAD", ".git/refs/heads", ".git/refs/remotes", ".git/FETCH_HEAD"):
        try:
            sig.append(os.stat(os.path.join(repo, rel)).st_mtime_ns)
        except OSError:
            sig.append(0)
    return tuple(sig)


def poll_git():
    """Fallback for live updates without the app: notice commits, pushes and
    checkouts from git's own metadata every few seconds."""
    while True:
        time.sleep(4)
        if not State.ready.is_set():
            continue
        with State.lock:
            paths = [r["path"] for r in State.data["repos"]]
        for p in paths:
            sig = git_signature(p)
            if State.git_sigs.get(p) not in (None, sig):
                try:
                    push_repo(p)
                except (subprocess.TimeoutExpired, StopIteration):
                    pass
            State.git_sigs[p] = sig


def summary_json():
    with State.lock:
        repos = list((State.data or {}).get("repos", []))
    hidden = load_hidden()
    lanes = []
    for r in repos:
        if r["path"] in hidden or "stale" in r["reasons"]:
            continue
        if "local-only" in r["reasons"]:
            lanes.append((0, r, f"{r['dirty']} changes, no remote" if r["dirty"] else "no remote"))
        elif r["dirty"]:
            lanes.append((1, r, f"{r['dirty']} uncommitted"))
        elif r["remote"] and r["unpushed"]:
            lanes.append((2, r, f"{r['unpushed']} to push"))
    lanes.sort(key=lambda x: (x[0], -x[1]["risk"]))
    return {"atRisk": len(lanes), "localOnly": sum(1 for x in lanes if x[0] == 0),
            "items": [{"name": r["name"], "path": r["path"], "line": line, "lane": lane} for lane, r, line in lanes[:10]]}


# ---------------------------------------------------------------- sweeps, secrets, convert

def get_sweep(kind, refresh=False):
    with State.sweep_locks[kind]:
        if refresh or kind not in State.sweeps:
            if kind == "downloads":
                State.sweeps[kind] = downloads.scan(State.dl_dir)
            elif kind == "desktop":
                State.sweeps[kind] = downloads.scan(osutil.user_folder("Desktop"))
            else:
                State.ready.wait()
                with State.lock:
                    repos = list(State.data["repos"])
                State.sweeps[kind] = storage.scan(repos)
        return {**State.sweeps[kind], "kind": kind, "can_undo": bool(downloads.last_sweep())}


def get_leaks(refresh=False):
    if refresh or State.leaks is None:
        State.ready.wait()
        with State.lock:
            repos = list(State.data["repos"])
        State.leaks = leaks.scan(repos)
    return State.leaks


def known_paths():
    s = set(State.outputs)
    for d in State.sweeps.values():
        s |= {i["path"] for i in d["items"]}
    if State.leaks:
        s |= {f["path"] for f in State.leaks["findings"]}
    with State.lock:
        s |= {r["path"] for r in (State.data or {}).get("repos", [])}
    return s


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _host_ok(self):
        # Blocks DNS-rebinding: only answer to our own loopback address.
        return self.headers.get("Host") in (f"127.0.0.1:{State.port}", f"localhost:{State.port}")

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = hub.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    msg = ": ping\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.unsubscribe(q)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        refresh = qs.get("refresh") == ["1"]
        if u.path == "/":
            html = (HERE / "dashboard.html").read_text().replace("__RADAR_TOKEN__", State.token)
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if u.path == "/api/events":
            return self._events()
        if u.path == "/api/scan":
            return self._send(200, get_scan(refresh))
        if u.path == "/api/summary":
            State.ready.wait(5)
            return self._send(200, summary_json())
        if u.path == "/api/sweep":
            kind = (qs.get("kind") or ["downloads"])[0]
            if kind not in State.sweep_locks:
                return self._send(400, {"error": "unknown kind"})
            return self._send(200, get_sweep(kind, refresh))
        if u.path == "/api/copies":
            group = int((qs.get("group") or ["0"])[0])
            with State.lock:
                members = [r for r in State.data["repos"] if r["dup_group"] == group]
            return self._send(200, {"group": group, "copies": copy_analysis(members)})
        if u.path == "/api/secrets":
            return self._send(200, get_leaks(refresh))
        if u.path == "/api/magsafe":
            return self._send(200, magsafe.state())
        if u.path == "/api/convert/recent":
            items = convert.recent()
            State.outputs |= {i["path"] for i in items}
            return self._send(200, {"items": items, "native": convert.native()})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        route = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        if route.path == "/api/changed":
            # Only Radar.app's file watcher knows this secret.
            if not State.app_secret or self.headers.get("X-Radar-App") != State.app_secret:
                return self._send(403, {"error": "forbidden"})
            body = json.loads(self.rfile.read(length) or b"{}")
            threading.Thread(target=on_changed, args=(body.get("paths", [])[:2000],), daemon=True).start()
            return self._send(200, {"ok": True})
        if self.headers.get("X-Radar-Token") != State.token:
            return self._send(403, {"error": "forbidden"})
        if route.path == "/api/convert/upload":
            return self._upload(route, length)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        try:
            handler = {
                "/api/action": self._repo_action,
                "/api/sweep/action": self._sweep_action,
                "/api/secrets/action": self._secret_action,
                "/api/convert": self._convert,
                "/api/reveal": self._reveal,
                "/api/magsafe": lambda b: self._send(200, magsafe.set_wanted(b.get("mode"))),
            }.get(route.path)
            if not handler:
                return self._send(404, {"error": "not found"})
            return handler(body)
        except subprocess.TimeoutExpired:
            return self._send(200, {"ok": False, "error": "Timed out."})

    def _repo_action(self, body):
        action = body.get("action")
        known = {r["path"] for r in (State.data or {}).get("repos", [])}
        if action in ("bulk_preview", "bulk_backup"):
            items = body.get("items") or [{"path": p} for p in body.get("paths", [])]
            if not items or any(i.get("path") not in known for i in items):
                return self._send(400, {"ok": False, "error": "Unknown repo. Rescan and try again."})
            return self._send(200, bulk_preview([i["path"] for i in items]) if action == "bulk_preview" else bulk_backup(items))
        path = body.get("path")
        if path not in known:
            return self._send(400, {"ok": False, "error": "Unknown repo. Rescan and try again."})
        if action == "preview":
            return self._send(200, commit_preview(path))
        if action == "open":
            return self._send(200, open_in(path, body.get("where")))
        if action in ("hide", "unhide"):
            return self._send(200, set_hidden(path, action == "hide"))
        if action == "copy_trash":
            return self._send(200, copy_trash(path))
        if action == "copy_fetch":
            if body.get("from") not in known:
                return self._send(400, {"ok": False, "error": "Unknown copy."})
            result = copy_fetch(path, body["from"])
        else:
            result = {
                "commit": lambda: wip_commit(path),
                "push": lambda: push(path),
                "github": lambda: create_github(path, body.get("name") or os.path.basename(path)),
                "trash_node_modules": lambda: trash_node_modules(path),
            }.get(action, lambda: {"ok": False, "error": "unknown action"})()
        result["repo"], _ = refresh_one(path)
        self._send(200, result)

    def _sweep_action(self, body):
        kind, action = body.get("kind"), body.get("action")
        if action == "undo":
            res = downloads.undo()
            State.sweeps.clear()
        else:
            if kind not in State.sweeps:
                return self._send(400, {"ok": False, "error": "Rescan and try again."})
            known = {i["path"] for i in State.sweeps[kind]["items"]}
            paths = body.get("paths") or []
            if not paths or any(p not in known for p in paths):
                return self._send(400, {"ok": False, "error": "Unknown item. Rescan and try again."})
            if action != "trash":
                return self._send(400, {"ok": False, "error": "unknown action"})
            res = downloads.sweep(paths)
            State.sweeps.pop(kind, None)
        res["scan"] = get_sweep(kind if kind in State.sweep_locks else "downloads", refresh=True)
        self._send(200, res)

    def _secret_action(self, body):
        f = next((x for x in (State.leaks or {}).get("findings", []) if x["id"] == body.get("id")), None)
        if not f:
            return self._send(400, {"ok": False, "error": "Rescan and try again."})
        action = body.get("action")
        if action == "untrack" and f["kind"] == "tracked":
            res = leaks.untrack(f)
        elif action == "ignore" and f["kind"] == "unignored":
            res = leaks.ignore(f)
        elif action == "trash" and f["kind"] == "loose":
            res = downloads.sweep([f["path"]])
        else:
            return self._send(400, {"ok": False, "error": "Not available for this file."})
        if f.get("repo"):
            push_repo(f["repo"])
        res["scan"] = get_leaks(refresh=True)
        self._send(200, res)

    def _convert(self, body):
        path, to = body.get("path"), body.get("to")
        if path not in State.outputs:
            return self._send(400, {"ok": False, "error": "Unknown file. Refresh and try again."})
        res = convert.convert(path, to)
        State.outputs |= set(res.get("outputs", []))
        self._send(200, res)

    def _upload(self, route, length):
        qs = parse_qs(route.query)
        name = os.path.basename((qs.get("name") or ["file"])[0]) or "file"
        to = (qs.get("to") or [""])[0]
        if length > 300 * 1024 * 1024:
            return self._send(413, {"ok": False, "error": "That file is over 300 MB."})
        inbox = DATA_DIR / "convert-inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        src = inbox / name
        with open(src, "wb") as fh:
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        if not to:   # just asking what this file can become
            return self._send(200, {"ok": True, "targets": convert.targets(src), "path": str(src)})
        res = convert.convert(src, to, out_dir=State.dl_dir)
        State.outputs |= set(res.get("outputs", []))
        src.unlink(missing_ok=True)
        self._send(200, res)

    def _reveal(self, body):
        path = body.get("path")
        if path not in known_paths() or not os.path.exists(path):
            return self._send(400, {"ok": False, "error": "Unknown file."})
        (osutil.open_path if body.get("how") == "open" else osutil.reveal)(path)
        self._send(200, {"ok": True})


def serve(port, open_browser):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    State.port = port = httpd.server_address[1]  # --port 0 picks a free one
    print(f"RADAR_PORT={port}", flush=True)  # Radar.app waits for this line
    url = f"http://127.0.0.1:{port}/"
    print(f"P2 Radar running at {url}  (Ctrl+C to stop)")
    cached = load_cache()
    if cached:   # open instantly with the last scan, then refresh behind it
        State.data = cached
        State.ready.set()

    def warm():
        full_scan()
        for kind in ("downloads", "storage", "desktop"):
            try:
                get_sweep(kind)
            except OSError:
                pass
    threading.Thread(target=warm, daemon=True).start()
    threading.Thread(target=poll_git, daemon=True).start()
    if os.environ.get("RADAR_APP"):
        # Started by Radar.app: exit if the app goes away without stopping us.
        parent = os.getppid()

        def watch():
            while os.getppid() == parent:
                time.sleep(2)
            os._exit(0)
        threading.Thread(target=watch, daemon=True).start()
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


# ---------------------------------------------------------------- CLI

def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def ago(ts):
    if not ts:
        return "never"
    d = (time.time() - ts) / 86400
    if d < 1:
        return "today"
    if d < 2:
        return "yesterday"
    if d < 60:
        return f"{int(d)}d ago"
    if d < 730:
        return f"{int(d / 30)}mo ago"
    return f"{int(d / 365)}y ago"


def summary(data):
    repos = data["repos"]
    risky = [r for r in repos if r["at_risk"]]
    print(f"\nP2 Radar: {len(repos)} repos scanned in {data['seconds']}s, {len(risky)} at risk\n")
    if data["blocked"]:
        print(f"  ! macOS blocked these folders, so repos inside them are missing: {', '.join(data['blocked'])}")
        print("    Fix: System Settings > Privacy & Security > Files & Folders (or Full Disk Access) > allow your terminal.\n")
    if risky:
        w = max(len(r["display"]) for r in risky)
        w = min(w, 70)
        print(f"  {'PROJECT':<{w}}  {'CHANGES':>7}  {'UNPUSHED':>8}  {'LAST ACTIVE':<11}  WHY")
        for r in risky:
            why = ", ".join(r["reasons"])
            if r["dup_group"]:
                why += f", duplicate #{r['dup_group']}"
            print(f"  {r['display'][:w]:<{w}}  {r['dirty']:>7}  {r['unpushed']:>8}  {ago(r['last_active']):<11}  {why}")
    dups = {}
    for r in repos:
        if r["dup_group"]:
            dups.setdefault(r["dup_group"], []).append(r)
    if dups:
        print("\n  Same project in more than one place:")
        for g, members in dups.items():
            print(f"   #{g}")
            for r in members:
                tag = "  <- most recent" if r.get("dup_newest") else ""
                print(f"     {r['display']}  ({r['dirty']} changes, active {ago(r['last_active'])}){tag}")
    nm = sum(r["node_modules_bytes"] for r in repos)
    print(f"\n  node_modules across all repos: {human(nm)}")
    print("  Open the dashboard to act on these:  python3 radar.py serve\n")


def main():
    ap = argparse.ArgumentParser(description="Find git repos with work that isn't backed up.")
    ap.add_argument("command", nargs="?", default="summary", choices=["summary", "downloads", "serve", "notify"])
    ap.add_argument("--root", action="append", help="folder to scan (repeatable; default: your home folder)")
    ap.add_argument("--downloads", default=str(osutil.user_folder("Downloads")), help="folder the Downloads sweep looks at")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--json", action="store_true", help="print the full scan as JSON")
    args = ap.parse_args()
    State.roots = [Path(r).expanduser().resolve() for r in (args.root or [HOME])]
    State.dl_dir = Path(args.downloads).expanduser().resolve()

    if args.command == "downloads":
        data = downloads.scan(State.dl_dir)
        return print(json.dumps(data, indent=2)) if args.json else downloads.summary(data)
    if args.command == "serve":
        return serve(args.port, not args.no_open)
    data = scan(State.roots, with_sizes=args.command != "notify")
    hidden = load_hidden()
    data["repos"] = [r for r in data["repos"] if r["path"] not in hidden]
    if args.json:
        print(json.dumps(data, indent=2))
    elif args.command == "notify":
        risky = [r for r in data["repos"] if r["at_risk"] and "stale" not in r["reasons"]]
        local = sum(1 for r in risky if "local-only" in r["reasons"])
        if risky:
            here = "this Mac" if osutil.IS_MAC else "this PC"
            msg = f"{len(risky)} repos have work that isn't backed up" + (f", {local} exist only on {here}" if local else "")
        else:
            msg = "Every active repo is committed and pushed."
        if data["blocked"]:
            msg += f". Couldn't read {', '.join(data['blocked'])}, so the count may be low"
        msg += ". Open P2 Radar to review." if os.environ.get("RADAR_APP") else ". Run: radar.py serve"
        if os.environ.get("RADAR_NOTIFY_JSON"):   # Radar.app posts a native notification itself
            print("RADAR_NOTIFY=" + json.dumps({"title": "P2 Radar", "body": msg, "count": len(risky), "localOnly": local}), flush=True)
        else:
            osutil.notify("P2 Radar", msg)
        print(time.strftime("%Y-%m-%d %H:%M"), msg)
    else:
        summary(data)


if __name__ == "__main__":
    sys.exit(main())
