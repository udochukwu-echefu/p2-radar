"""One-click document converter for Radar. Output lands next to the
original, never overwriting it.

macOS:   textutil (documents), sips (images), Radar.app's native converter
         (RADAR_APP_BIN --convert) for anything to or from PDF.
Windows: Microsoft Word through COM for documents and PDF (or LibreOffice when
         Word isn't installed), and Windows' own imaging (System.Drawing) for images.
Linux:   LibreOffice when installed.
"""

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import osutil

HOME = Path.home()
ROOTS = [osutil.user_folder(n) for n in ("Downloads", "Desktop", "Documents")]
DOCS = {".docx", ".doc", ".rtf", ".odt", ".html", ".htm", ".txt", ".md", ".markdown", ".rtfd"}
IMAGES = {".heic", ".heif", ".png", ".jpg", ".jpeg", ".webp", ".tiff", ".tif", ".gif", ".bmp"}
TEXTUTIL_OUT = {"docx": "docx", "rtf": "rtf", "txt": "txt", "html": "html", "odt": "odt"}
SIPS_OUT = {"jpg": "jpeg", "png": "png", "heic": "heic", "pdf": "pdf", "tiff": "tiff"}
WORD_FORMATS = {"pdf": 17, "docx": 16, "rtf": 6, "txt": 7, "html": 10}           # WdSaveFormat
GDI_FORMATS = {"jpg": "Jpeg", "png": "Png", "tiff": "Tiff", "bmp": "Bmp", "gif": "Gif"}
GDI_READS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".tif"}


def app_bin():
    b = os.environ.get("RADAR_APP_BIN")
    return b if osutil.IS_MAC and b and os.path.exists(b) else None


def _word():
    if not osutil.IS_WIN:
        return False
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Word.Application\CLSID"))
        return True
    except OSError:
        return False


def _soffice():
    for c in (shutil.which("soffice"), shutil.which("libreoffice"),
              r"C:\Program Files\LibreOffice\program\soffice.exe", "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if c and os.path.exists(c):
            return c
    return None


_CAPS = None


def caps():
    """What this computer can convert, worked out once."""
    global _CAPS
    if _CAPS is None:
        word, office = _word(), _soffice()
        _CAPS = {
            "docs": "textutil" if osutil.IS_MAC else "word" if word else "libreoffice" if office else None,
            "pdf_out": bool(app_bin()) or word or bool(office),
            "pdf_in": bool(app_bin()) or word,
            "pdf_pages": bool(app_bin()),
            "images": "sips" if osutil.IS_MAC else "gdi" if osutil.IS_WIN else None,
            "office": office, "word": word,
        }
    return _CAPS


def native():
    """Whether PDF output is available (shown in the dashboard)."""
    return caps()["pdf_out"]


def targets(path):
    c = caps()
    ext = Path(path).suffix.lower()
    if ext in (".md", ".markdown"):
        t = ["pdf", "docx", "html", "txt"] if c["docs"] else ["html", "txt"]
    elif ext in DOCS:
        t = ["pdf", "docx", "rtf", "txt", "html"] if c["docs"] else []
    elif ext in IMAGES:
        if c["images"] == "sips":
            t = ["jpg", "png", "heic", "pdf"]
        elif c["images"] == "gdi" and ext in GDI_READS:
            t = ["jpg", "png", "tiff"]
        else:
            t = []
    elif ext == ".pdf":
        t = (["txt", "docx"] if c["pdf_in"] else []) + (["png"] if c["pdf_pages"] else [])
    else:
        return []
    own = {".jpeg": "jpg", ".htm": "html", ".tif": "tiff", ".heif": "heic"}.get(ext, ext[1:])
    t = [x for x in t if x != own]
    if not c["pdf_out"]:
        t = [x for x in t if x != "pdf"]
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
    return f"<!doctype html><meta charset='utf-8'><body style='font-family:-apple-system,Segoe UI,Helvetica;font-size:12pt;line-height:1.45'>{body}</body>"


def _run(cmd, timeout=180, env=None):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})}, **osutil.quiet())
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-400:] or f"{cmd[0]} failed")
    return r.stdout


def _native(src, dst):
    b = app_bin()
    if not b:
        raise RuntimeError("PDF conversion needs the P2 Radar app (run it from the app, not the browser).")
    out = _run([b, "--convert", str(src), str(dst)])
    try:
        return [Path(p) for p in json.loads(out.strip().splitlines()[-1]).get("outputs", [])]
    except (ValueError, IndexError):
        return [Path(dst)]


_WORD_PS = r"""
$ErrorActionPreference = 'Stop'
$w = New-Object -ComObject Word.Application
$w.Visible = $false; $w.DisplayAlerts = 0
try {
  $d = $w.Documents.Open($env:RADAR_SRC, $false, $true)
  $d.SaveAs2($env:RADAR_DST, [int]$env:RADAR_FMT)
  $d.Close(0)
} finally { $w.Quit() }
"""

_GDI_PS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$img = [System.Drawing.Image]::FromFile($env:RADAR_SRC)
try { $img.Save($env:RADAR_DST, [System.Drawing.Imaging.ImageFormat]::$($env:RADAR_FMT)) } finally { $img.Dispose() }
"""


def _powershell(script, env, timeout=240):
    _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], timeout=timeout, env=env)


def _office(src, dst, to):
    """LibreOffice writes <stem>.<ext> into a folder; move it to the unique name."""
    out = Path(tempfile.mkdtemp(prefix="radar-lo-"))
    fmt = {"txt": "txt:Text", "html": "html"}.get(to, to)
    _run([caps()["office"], "--headless", "--convert-to", fmt, "--outdir", str(out), str(src)], timeout=300)
    produced = next(out.iterdir(), None)
    if not produced:
        raise RuntimeError("LibreOffice didn't produce a file")
    shutil.move(str(produced), dst)
    shutil.rmtree(out, ignore_errors=True)


def _document(work, dst, to):
    backend = caps()["docs"]
    if backend == "textutil":
        if to == "pdf":
            return _native(work, dst)
        _run(["textutil", "-convert", TEXTUTIL_OUT[to], str(work), "-output", str(dst)])
    elif backend == "word":
        _powershell(_WORD_PS, {"RADAR_SRC": str(work), "RADAR_DST": str(dst), "RADAR_FMT": str(WORD_FORMATS[to])})
    elif backend == "libreoffice":
        _office(work, dst, to)
    else:
        raise RuntimeError("No document converter here. Install Microsoft Word or LibreOffice.")
    return [dst]


def convert(src, to, out_dir=None):
    src = Path(src)
    ext = src.suffix.lower()
    if to not in targets(src):
        return {"ok": False, "error": f"Can't convert {ext or 'this file'} to {to.upper()} on this computer."}
    dst = _unique((Path(out_dir) if out_dir else src.parent) / f"{src.stem}.{to}")
    tmp = Path(tempfile.mkdtemp(prefix="radar-convert-"))
    try:
        work = src
        if ext in (".md", ".markdown"):
            work = tmp / f"{src.stem}.html"
            work.write_text(_md_to_html(src.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
            if to in ("html", "txt"):
                shutil.copy(work if to == "html" else src, dst)
                return {"ok": True, "outputs": [str(dst)]}
        if ext in IMAGES:
            if caps()["images"] == "sips":
                _run(["sips", "-s", "format", SIPS_OUT[to], str(work), "--out", str(dst)])
            else:
                _powershell(_GDI_PS, {"RADAR_SRC": str(work), "RADAR_DST": str(dst), "RADAR_FMT": GDI_FORMATS[to]})
            outputs = [dst]
        elif ext == ".pdf":
            if app_bin():   # macOS: PDFKit in Radar.app
                if to == "txt":
                    outputs = _native(work, dst)
                elif to == "docx":
                    txt = tmp / f"{src.stem}.txt"
                    _native(work, txt)
                    _run(["textutil", "-convert", "docx", str(txt), "-output", str(dst)])
                    outputs = [dst]
                else:
                    outputs = _native(work, dst)
            else:           # Windows: Word opens PDFs and re-saves them
                outputs = _document(work, dst, to)
        else:
            outputs = _document(work, dst, to)
        return {"ok": True, "outputs": [str(o) for o in outputs]}
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "error": str(e)[:400]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
