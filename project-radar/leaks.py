"""Secrets check for Radar: credential files that git tracks, that git would
pick up on the next `git add .`, or that sit loose in Desktop, Documents and
Downloads. Fixes are local and reversible: untrack + .gitignore, or Trash.
"""

import fnmatch
import hashlib
import os
import subprocess
import time
from pathlib import Path

import osutil

HOME = Path.home()
PATTERNS = [".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa", "id_rsa.*", "id_ed25519", "id_ed25519.*",
            "client_secret*.json", "credentials*.json", "*service-account*.json", "*serviceaccount*.json",
            "*.keystore", "*.jks", ".pypirc", "*.ovpn", "*.mobileprovision", "secrets.y*ml", ".netrc"]
SAFE = {".env.example", ".env.sample", ".env.template", ".env.defaults", ".env.dist", "example.env", "sample.env"}
LOOSE_ROOTS = [osutil.user_folder(n) for n in ("Desktop", "Documents", "Downloads")]


def looks_secret(path):
    name = os.path.basename(path).lower()
    if name in SAFE or name.endswith((".example", ".sample", ".template")):
        return False
    return any(fnmatch.fnmatch(name, p) for p in PATTERNS)


def _git(repo, *args, timeout=30):
    r = subprocess.run(["git", "-C", repo, "-c", "core.quotePath=false", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout, **osutil.quiet())
    return r.returncode, r.stdout, r.stderr


def _fid(*parts):
    return hashlib.sha1("\0".join(parts).encode()).hexdigest()[:12]


def _finding(kind, path, repo=None, rel=None, remote=None, name=None):
    sev = {"tracked": "high" if remote else "medium", "unignored": "medium", "loose": "low"}[kind]
    return {"id": _fid(kind, path), "kind": kind, "severity": sev, "path": path, "repo": repo, "rel": rel,
            "remote": remote, "name": name or os.path.basename(path),
            "display": osutil.display(path)}


def scan(repos, loose_roots=None):
    t0 = time.time()
    findings = []
    for r in repos or []:
        repo = r["path"]
        try:
            _, tracked, _ = _git(repo, "ls-files", "-z")
            _, others, _ = _git(repo, "ls-files", "--others", "--exclude-standard", "-z", timeout=60)
        except subprocess.TimeoutExpired:
            continue
        for rel in filter(None, tracked.split("\0")):
            if looks_secret(rel) and "/node_modules/" not in f"/{rel}":
                findings.append(_finding("tracked", os.path.normpath(os.path.join(repo, rel)), repo, rel, r.get("remote_url"), r["name"]))
        for rel in filter(None, others.split("\0")):
            if looks_secret(rel) and "/node_modules/" not in f"/{rel}":
                findings.append(_finding("unignored", os.path.normpath(os.path.join(repo, rel)), repo, rel, None, r["name"]))
    repo_paths = [r["path"] for r in repos or []]
    for root in LOOSE_ROOTS if loose_roots is None else loose_roots:
        for dirpath, dirs, files in os.walk(root):
            depth = os.path.relpath(dirpath, root).count(os.sep)
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"
                       and not os.path.isdir(os.path.join(dirpath, d, ".git")) and depth < 1]
            for f in files:
                p = os.path.join(dirpath, f)
                if looks_secret(f) and not any(p.startswith(rp + os.sep) for rp in repo_paths):
                    findings.append(_finding("loose", p))
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["display"]))
    return {"scanned_at": int(time.time()), "seconds": round(time.time() - t0, 1), "findings": findings}


def untrack(f):
    """Stop tracking a committed secret and ignore it from now on. The file
    stays on disk; history still holds it, so the key should be rotated."""
    code, _, err = _git(f["repo"], "rm", "--cached", "--quiet", "--", f["rel"])
    if code != 0:
        return {"ok": False, "error": err.strip()}
    ignore(f, quiet=True)
    return {"ok": True, "message": f"{f['rel']} is no longer tracked and is in .gitignore. Snapshot to commit that, "
                                   "and rotate the key: it's still in the repo's history."}


def ignore(f, quiet=False):
    gi = Path(f["repo"]) / ".gitignore"
    line = "/" + f["rel"]
    existing = gi.read_text().splitlines() if gi.exists() else []
    if line not in existing and f["rel"] not in existing:
        with gi.open("a") as fh:
            if existing and existing[-1].strip():
                fh.write("\n")
            fh.write(f"# added by Radar: looks like a secret\n{line}\n")
    return {"ok": True, "message": f"Added {f['rel']} to .gitignore."} if not quiet else {"ok": True}
