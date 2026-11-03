"""End-to-end tests for Radar's engine. Standard library only; runs the same
on macOS, Windows and Linux against throwaway repos and folders.

    python -m unittest discover -s tests -v
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="radar-tests-"))
os.environ["RADAR_DATA"] = str(TMP / "data")   # keep state, logs and the holding folder out of the real profile
for k, v in {"GIT_AUTHOR_NAME": "Radar Test", "GIT_AUTHOR_EMAIL": "test@example.com",
             "GIT_COMMITTER_NAME": "Radar Test", "GIT_COMMITTER_EMAIL": "test@example.com"}.items():
    os.environ.setdefault(k, v)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "project-radar"))

import convert    # noqa: E402
import downloads  # noqa: E402
import leaks      # noqa: E402
import osutil     # noqa: E402
import radar      # noqa: E402

PNG_1PX = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def sh(*args, cwd=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"{args}: {r.stderr or r.stdout}")
    return r.stdout


def make_repo(path, files=None, commit=True):
    path.mkdir(parents=True, exist_ok=True)
    sh("git", "init", "-q", "-b", "main", str(path))
    for rel, content in (files or {}).items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(content if isinstance(content, bytes) else content.encode())
    if commit:
        sh("git", "-C", str(path), "add", "-A")
        sh("git", "-C", str(path), "commit", "-q", "--allow-empty", "-m", "init")
    return path


class Projects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = TMP / "projects"
        cls.local = make_repo(cls.root / "local-only", {"app.js": "x"})
        (cls.local / "new.js").write_text("y")
        cls.bare = TMP / "remote.git"
        sh("git", "init", "-q", "--bare", "-b", "main", str(cls.bare))
        cls.synced = make_repo(cls.root / "synced", {"readme.md": "hi"})
        sh("git", "-C", str(cls.synced), "remote", "add", "origin", str(cls.bare))
        sh("git", "-C", str(cls.synced), "push", "-q", "-u", "origin", "main")
        cls.copy = cls.root / "synced-copy"
        sh("git", "clone", "-q", str(cls.bare), str(cls.copy))

    def scan(self):
        return {r["name"]: r for r in radar.scan([self.root])["repos"]}

    def test_scan_sorts_risk(self):
        repos = self.scan()
        self.assertIn("local-only", repos["local-only"]["reasons"])
        self.assertIn("uncommitted", repos["local-only"]["reasons"])
        self.assertFalse(repos["synced"]["at_risk"])
        self.assertEqual(repos["synced"]["dup_group"], repos["synced-copy"]["dup_group"])
        self.assertIsNotNone(repos["synced"]["dup_group"])

    def test_snapshot_holds_back_secrets(self):
        repo = make_repo(self.root / "with-secret", {"a.txt": "a"})
        (repo / ".env").write_text("SECRET=1")
        (repo / "b.txt").write_text("b")
        (repo / "node_modules" / "x").mkdir(parents=True)
        (repo / "node_modules" / "x" / "i.js").write_text("m")
        prev = radar.commit_preview(str(repo))
        self.assertEqual([f["path"] for f in prev["include"]], ["b.txt"])
        self.assertEqual({f["path"] for f in prev["exclude"]}, {".env", "node_modules/x/i.js"})
        self.assertTrue(radar.wip_commit(str(repo))["ok"])
        self.assertIn("WIP snapshot", sh("git", "-C", str(repo), "log", "-1", "--format=%s"))

    def test_push(self):
        (self.synced / "more.md").write_text("more")
        sh("git", "-C", str(self.synced), "add", "-A")
        sh("git", "-C", str(self.synced), "commit", "-q", "-m", "more")
        self.assertEqual(radar.inspect(str(self.synced))["unpushed"], 1)
        self.assertTrue(radar.push(str(self.synced))["ok"])
        self.assertEqual(radar.inspect(str(self.synced))["unpushed"], 0)

    def test_copy_analysis(self):
        # Its own pair of copies, so the branches it fetches don't touch the other tests' repos.
        base = TMP / "copies"
        bare = base / "origin.git"
        sh("git", "init", "-q", "--bare", "-b", "main", str(bare))
        first = make_repo(base / "first", {"a.md": "a"})
        sh("git", "-C", str(first), "remote", "add", "origin", str(bare))
        sh("git", "-C", str(first), "push", "-q", "-u", "origin", "main")
        second = base / "second"
        sh("git", "clone", "-q", str(bare), str(second))
        (second / "only.md").write_text("c")
        sh("git", "-C", str(second), "add", "-A")
        sh("git", "-C", str(second), "commit", "-q", "-m", "only in the copy")
        res = {Path(a["path"]).name: a for a in radar.copy_analysis([radar.inspect(str(p)) for p in (first, second)])}
        self.assertEqual(res["second"]["unique"], 1)
        self.assertEqual(res["second"]["commits"][0]["subject"], "only in the copy")
        self.assertTrue(res["first"]["contained"])
        self.assertTrue(radar.copy_fetch(str(first), str(second))["ok"])
        self.assertIn("radar/second/main", sh("git", "-C", str(first), "branch", "--list", "radar/*"))

    def test_secrets(self):
        make_repo(self.root / "leaky", {".env": "KEY=1", ".env.example": "KEY=", "src/app.py": "pass"})
        found = leaks.scan([radar.inspect(str(self.root / "leaky"))], loose_roots=[])["findings"]
        self.assertEqual([(f["kind"], f["rel"]) for f in found], [("tracked", ".env")])
        self.assertTrue(leaks.untrack(found[0])["ok"])
        self.assertIn("/.env", (self.root / "leaky" / ".gitignore").read_text())


class Sweep(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=TMP, prefix="dl-"))
        (self.dir / "report.pdf").write_bytes(b"same")
        (self.dir / "report (1).pdf").write_bytes(b"same")
        (self.dir / "big.crdownload").write_bytes(b"x")
        old = self.dir / "Screenshot 2026-01-02 101112.png"
        old.write_bytes(PNG_1PX)
        t = time.time() - 90 * 86400
        os.utime(old, (t, t))
        make_repo(self.dir / "a-project", {"x.txt": "x"})

    def test_classify(self):
        items = {i["name"]: i for i in downloads.scan(self.dir)["items"]}
        self.assertEqual(items["report (1).pdf"]["category"], "duplicate")
        self.assertTrue(items["report (1).pdf"]["suggest"])
        self.assertEqual(items["report.pdf"]["category"], "other")
        self.assertEqual(items["big.crdownload"]["category"], "partial")
        self.assertEqual(items["Screenshot 2026-01-02 101112.png"]["category"], "screenshot")
        self.assertTrue(items["Screenshot 2026-01-02 101112.png"]["suggest"])
        self.assertEqual(items["a-project"]["category"], "project")
        self.assertFalse(items["a-project"]["suggest"])

    def test_trash_and_undo(self):
        targets = [str(self.dir / "report (1).pdf"), str(self.dir / "big.crdownload")]
        res = downloads.sweep(targets)
        self.assertTrue(res["ok"], res)
        self.assertFalse(any(os.path.exists(p) for p in targets))
        res = downloads.undo()
        self.assertTrue(res["ok"], res)
        self.assertTrue(all(os.path.exists(p) for p in targets), res)
        self.assertEqual(downloads.undo().get("error"), "Nothing to undo.")


class Convert(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=TMP, prefix="conv-"))

    def test_markdown_to_html(self):
        md = self.dir / "notes.md"
        md.write_text("# Title\n\nSome **bold** text.\n\n- a\n- b\n")
        res = convert.convert(md, "html")
        self.assertTrue(res["ok"], res)
        out = Path(res["outputs"][0]).read_text()
        self.assertIn("<h1>Title</h1>", out)
        self.assertIn("<b>bold</b>", out)
        self.assertEqual(convert.convert(md, "html")["outputs"][0], str(self.dir / "notes (2).html"))

    @unittest.skipUnless(convert.caps()["images"], "no image converter on this system")
    def test_image(self):
        png = self.dir / "dot.png"
        png.write_bytes(PNG_1PX)
        self.assertIn("jpg", convert.targets(png))
        res = convert.convert(png, "jpg")
        self.assertTrue(res["ok"], res)
        self.assertGreater(os.path.getsize(res["outputs"][0]), 0)

    @unittest.skipUnless(convert.caps()["docs"] in ("textutil", "word", "libreoffice"), "no document converter on this system")
    def test_document(self):
        md = self.dir / "doc.md"
        md.write_text("# Hello\n\nWorld\n")
        res = convert.convert(md, "docx")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["outputs"][0].endswith(".docx"))


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = TMP / "server-root"
        make_repo(root / "demo", {"a.txt": "a"})
        radar.State.roots = [root]
        radar.State.dl_dir = Path(tempfile.mkdtemp(dir=TMP, prefix="srv-dl-"))
        cls.httpd = radar.ThreadingHTTPServer(("127.0.0.1", 0), radar.Handler)
        radar.State.port = cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        radar.full_scan()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def req(self, path, body=None, headers=None):
        h = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=h, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_scan_and_platform(self):
        code, d = self.req("/api/scan")
        self.assertEqual(code, 200)
        self.assertEqual([r["name"] for r in d["repos"]], ["demo"])
        self.assertEqual(d["platform"]["os"], osutil.NAME)

    def test_security(self):
        self.assertEqual(self.req("/api/action", {"action": "push", "path": "x"})[0], 403)              # no token
        self.assertEqual(self.req("/api/scan", headers={"Host": "evil.example"})[0], 403)                # DNS rebinding
        code, d = self.req("/api/action", {"action": "preview", "path": "/etc"}, {"X-Radar-Token": radar.State.token})
        self.assertEqual(code, 400)                                                                      # unknown repo
        self.assertEqual(self.req("/api/changed", {"paths": []})[0], 403)                                 # app-only endpoint

    def test_sweep_endpoint(self):
        code, d = self.req("/api/sweep?kind=downloads")
        self.assertEqual(code, 200)
        self.assertEqual(d["items"], [])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
