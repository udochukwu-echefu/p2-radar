"""One-click document converter for Radar. Uses macOS's own tools: textutil
for document formats, sips for images, and Radar.app's native converter
(RADAR_APP_BIN --convert) for anything to or from PDF. Output lands next to
the original, never overwriting it.
"""

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

HOME = Path.home()
ROOTS = [HOME / "Downloads", HOME / "Desktop", HOME / "Documents"]
DOCS = {".docx", ".doc", ".rtf", ".odt", ".html", ".htm", ".txt", ".md", ".markdown", ".rtfd", ".pages_unsupported"}
IMAGES = {".heic", ".heif", ".png", ".jpg", ".jpeg", ".webp", ".tiff", ".tif", ".gif", ".bmp"}
TEXTUTIL_OUT = {"docx": "docx", "rtf": "rtf", "txt": "txt", "html": "html", "odt": "odt"}
SIPS_OUT = {"jpg": "jpeg", "png": "png", "heic": "heic", "pdf": "pdf", "tiff": "tiff"}


def app_bin():
    b = os.environ.get("RADAR_APP_BIN")
    return b if b and os.path.exists(b) else None


def targets(path):
    ext = Path(path).suffix.lower()
    if ext in (".md", ".markdown"):
        t = ["pdf", "docx", "html", "txt"]
    elif ext in DOCS:
        t = ["pdf", "docx", "rtf", "txt", "html"]
    elif ext in IMAGES:
        t = ["jpg", "png", "heic", "pdf"]
    elif ext == ".pdf":
        t = ["txt", "docx", "png"]
    else:
        return []
    own = {".jpeg": "jpg", ".htm": "html", ".tif": "tiff", ".heif": "heic"}.get(ext, ext[1:])
    t = [x for x in t if x != own]
    if not app_bin():   # PDF in/out needs the native converter
        t = [x for x in t if x != "pdf"] if ext != ".pdf" else []
    return t


def recent(limit=40):
    items = []
    for root in ROOTS:
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for e in entries:
            if e.name.startswith(".") or not e.is_file(follow_symlinks=False):
                continue
            t = targets(e.path)
            if t:
                st = e.stat()
                items.append({"path": e.path, "name": e.name, "folder": root.name, "size": st.st_size,
                              "mtime": int(st.st_mtime), "targets": t})
    items.sort(key=lambda i: -i["mtime"])
    return items[:limit]


def _unique(path):
    p = Path(path)
    if not p.exists():
        return p
    n = 2
    while True:
        q = p.with_name(f"{p.stem} ({n}){p.suffix}")
        if not q.exists():
            return q
        n += 1


def _md_to_html(text):
    """Small Markdown subset: headings, lists, emphasis, code, links, paragraphs."""
    out, para, in_list, in_code = [], [], None, False

    def inline(s):
        s = html.escape(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", s)
        return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)

    def flush():
        nonlocal para, in_list
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para = []
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None

    for line in text.splitlines():
        if line.startswith("```"):
            flush()
            out.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            out.append(html.escape(line))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        lm = re.match(r"^\s*([-*+]|\d+\.)\s+(.*)", line)
        if m:
            flush()
            out.append(f"<h{len(m[1])}>{inline(m[2])}</h{len(m[1])}>")
        elif lm:
            kind = "ol" if lm[1][0].isdigit() else "ul"
            if para:
                flush()
            if in_list != kind:
                if in_list:
                    out.append(f"</{in_list}>")
                out.append(f"<{kind}>")
                in_list = kind
            out.append(f"<li>{inline(lm[2])}</li>")
        elif not line.strip():
            flush()
        else:
            if in_list:
                out.append(f"</{in_list}>")
                in_list = None
            para.append(line.strip())
    flush()
    body = "\n".join(out)
    return f"<!doctype html><meta charset='utf-8'><body style='font-family:-apple-system,Helvetica;font-size:12pt;line-height:1.45'>{body}</body>"


def _run(cmd, timeout=180):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"{cmd[0]} failed")
    return r.stdout


def _native(src, dst):
    b = app_bin()
    if not b:
        raise RuntimeError("PDF conversion needs Radar.app (run it from the app, not the browser).")
    out = _run([b, "--convert", str(src), str(dst)])
    try:
        return [Path(p) for p in json.loads(out.strip().splitlines()[-1]).get("outputs", [])]
    except (ValueError, IndexError):
        return [Path(dst)]


def convert(src, to, out_dir=None):
    src = Path(src)
    ext = src.suffix.lower()
    if to not in targets(src):
        return {"ok": False, "error": f"Can't convert {ext or 'this file'} to {to.upper()}."}
    dst = _unique((Path(out_dir) if out_dir else src.parent) / f"{src.stem}.{to}")
    tmp = Path(tempfile.mkdtemp(prefix="radar-convert-"))
    try:
        work = src
        if ext in (".md", ".markdown"):
            work = tmp / f"{src.stem}.html"
            work.write_text(_md_to_html(src.read_text(errors="replace")))
            if to == "html":
                shutil.copy(work, dst)
                return {"ok": True, "outputs": [str(dst)]}
            if to == "txt":
                shutil.copy(src, dst)
                return {"ok": True, "outputs": [str(dst)]}
        if ext in IMAGES:
            if to == "pdf" and app_bin() is None:
                return {"ok": False, "error": "PDF output needs Radar.app."}
            _run(["sips", "-s", "format", SIPS_OUT[to], str(work), "--out", str(dst)])
            outputs = [dst]
        elif ext == ".pdf":
            if to == "txt":
                outputs = _native(work, dst)
            elif to == "docx":
                txt = tmp / f"{src.stem}.txt"
                _native(work, txt)
                _run(["textutil", "-convert", "docx", str(txt), "-output", str(dst)])
                outputs = [dst]
            else:  # png: one image per page
                outputs = _native(work, dst)
        elif to == "pdf":
            outputs = _native(work, dst)
        else:
            _run(["textutil", "-convert", TEXTUTIL_OUT[to], str(work), "-output", str(dst)])
            outputs = [dst]
        return {"ok": True, "outputs": [str(o) for o in outputs]}
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "error": str(e)[:400]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
