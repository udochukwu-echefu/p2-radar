"""The few things P2 Radar does differently on macOS, Windows and Linux:
where state lives, folder sizes, the trash/Recycle Bin and undo, showing a
file in Finder/Explorer, opening a terminal, installed apps, notifications.
Everything else in the engine is shared.
"""

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
NAME = "windows" if IS_WIN else "mac" if IS_MAC else "linux"
HOME = Path.home()

LABELS = {
    "mac": {"files": "Finder", "trash": "Trash", "terminal": "Terminal", "mod": "⌘"},
    "windows": {"files": "Explorer", "trash": "Recycle Bin", "terminal": "Terminal", "mod": "Ctrl+"},
    "linux": {"files": "Files", "trash": "Trash", "terminal": "Terminal", "mod": "Ctrl+"},
}[NAME]


def data_dir():
    if os.environ.get("RADAR_DATA"):
        return Path(os.environ["RADAR_DATA"])
    if IS_WIN:
        return Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData/Local") / "Radar"
    if IS_MAC:
        return HOME / "Library/Application Support/Project Radar"
    return Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "radar"


def display(path):
    """~/Documents/x on macOS, ~\\Documents\\x on Windows."""
    s = str(path)
    home = str(HOME)
    if s == home:
        return "~"
    if s.startswith(home + os.sep):
        return "~" + os.sep + os.path.relpath(s, home)
    return s


_SHELL_FOLDERS = {"Desktop": "Desktop", "Documents": "Personal", "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}"}


def user_folder(name):
    """Where Desktop, Documents or Downloads really is. Windows can redirect
    them (often into OneDrive); the registry knows where."""
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                p = Path(os.path.expandvars(winreg.QueryValueEx(k, _SHELL_FOLDERS[name])[0]))
                if p.is_dir():
                    return p
        except (OSError, KeyError):
            pass
        for p in (HOME / name, HOME / "OneDrive" / name):
            if p.is_dir():
                return p
    return HOME / name


def quiet():
    """subprocess options that keep console windows from flashing on Windows."""
    return {"creationflags": 0x08000000} if IS_WIN else {}


# ---------------------------------------------------------------- sizes

def dir_size(path):
    p = str(path)
    try:
        if os.path.isfile(p) or os.path.islink(p):
            return os.lstat(p).st_size
    except OSError:
        return 0
    if not IS_WIN and shutil.which("du"):
        try:
            r = subprocess.run(["du", "-sk", p], capture_output=True, text=True, timeout=120)
            return int(r.stdout.split()[0]) * 1024
        except (subprocess.TimeoutExpired, ValueError, IndexError, OSError):
            pass
    total, stack = 0, [p]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


# ---------------------------------------------------------------- trash + undo

def _hold_dir():
    return data_dir() / "Trash"


def _unique(folder, name):
    dest = Path(folder) / name
    stem, ext = os.path.splitext(name)
    n = 1
    while dest.exists():
        dest = Path(folder) / f"{stem} {n}{ext}"
        n += 1
    return dest


if IS_WIN:
    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int), ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", ctypes.c_wchar_p)]

    def _recycle(path):
        FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
        op = _SHFILEOPSTRUCTW(None, FO_DELETE, os.path.abspath(path) + "\0", None,
                              FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, 0, None, None)
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return rc == 0 and not op.fAnyOperationsAborted and not os.path.exists(path)


def trash(path):
    """Move a file or folder to the system trash and return a token that
    untrash() can use to put it back."""
    # realpath expands Windows short names (C:\\Users\\RUNNER~1) so the Recycle Bin
    # records the same long path that untrash() later looks for.
    path = os.path.realpath(path) if IS_WIN else os.path.abspath(path)
    if IS_MAC and os.path.exists("/usr/bin/trash"):
        r = subprocess.run(["/usr/bin/trash", "-v", "-s", path], capture_output=True, text=True, timeout=300)
        m = re.search(r' to "(.+)"\s*$', r.stdout)
        if r.returncode == 0 and m:
            return m[1]
        if r.returncode != 0:
            raise OSError((r.stderr or r.stdout).strip() or "trash failed")
    if IS_WIN:
        try:
            if _recycle(path):
                return "recycle:" + path
        except OSError:
            pass
    # Fallback: a holding folder Radar can always restore from.
    folder = HOME / ".Trash" if IS_MAC else _hold_dir()
    folder.mkdir(parents=True, exist_ok=True)
    dest = _unique(folder, os.path.basename(path))
    shutil.move(path, dest)
    return str(dest)


def _recycle_info(info):
    """Original path and deletion time from a Recycle Bin $I file
    (version 1: Vista/7, version 2: Windows 10 and later)."""
    data = Path(info).read_bytes()
    version = int.from_bytes(data[0:8], "little")
    deleted = int.from_bytes(data[16:24], "little")
    if version >= 2:
        n = int.from_bytes(data[24:28], "little")
        original = data[28:28 + 2 * n].decode("utf-16-le")
    else:
        original = data[24:24 + 520].decode("utf-16-le")
    return original.split("\0")[0], deleted


def _restore_recycled(deleted_path, original):
    """Find the newest Recycle Bin entry for deleted_path and move it back.
    Reads the bin's own $I records, so it doesn't depend on Explorer settings or language."""
    drive = os.path.splitdrive(deleted_path)[0] or os.path.splitdrive(str(HOME))[0]
    want = os.path.normcase(os.path.realpath(deleted_path))
    best = None
    try:
        sids = [e.path for e in os.scandir(drive + "\\$Recycle.Bin") if e.is_dir()]
    except OSError:
        return False
    for sid in sids:
        try:
            infos = [e.path for e in os.scandir(sid) if e.name.startswith("$I")]
        except OSError:
            continue   # another user's bin
        for info in infos:
            try:
                orig, when = _recycle_info(info)
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if os.path.normcase(os.path.realpath(orig)) == want and (best is None or when > best[1]):
                best = (info, when)
    if not best:
        return False
    info = Path(best[0])
    data = info.with_name("$R" + info.name[2:])
    if not data.exists():
        return False
    shutil.move(str(data), original)
    info.unlink(missing_ok=True)
    return True


def untrash(token, original):
    """Put a trashed item back. Returns True when it was restored."""
    if os.path.exists(original):
        return False
    if token.startswith("recycle:"):
        return IS_WIN and _restore_recycled(token[len("recycle:"):], original)
    if os.path.exists(token):
        shutil.move(token, original)
        return True
    return False


# ---------------------------------------------------------------- opening things

def reveal(path):
    if IS_MAC:
        subprocess.Popen(["open", "-R", path])
    elif IS_WIN:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


def open_path(path):
    if IS_MAC:
        subprocess.Popen(["open", path])
    elif IS_WIN:
        os.startfile(path)  # noqa: the default app for this file or folder
    else:
        subprocess.Popen(["xdg-open", path])


def open_editor(path):
    code = shutil.which("code")
    if code:
        subprocess.Popen([code, path], **quiet())
    else:
        open_path(path)


def open_terminal(path):
    if IS_MAC:
        subprocess.Popen(["open", "-a", "Terminal", path])
    elif IS_WIN:
        wt = shutil.which("wt")
        if wt:
            subprocess.Popen([wt, "-d", path])
        else:
            subprocess.Popen(["cmd", "/c", "start", "cmd", "/K", "cd", "/d", path])
    else:
        term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal")
        if term:
            subprocess.Popen([term], cwd=path)


# ---------------------------------------------------------------- installed apps

def _tokens(name):
    return re.findall(r"[a-z]+", name.lower())


def installed_apps():
    """First word of each installed app's name -> display name."""
    names = {}
    if IS_MAC:
        for d in ("/Applications", str(HOME / "Applications"), "/System/Applications"):
            try:
                for e in os.scandir(d):
                    if e.name.endswith(".app"):
                        t = _tokens(e.name[:-4])
                        if t:
                            names.setdefault(t[0], e.name)
            except OSError:
                pass
    elif IS_WIN:
        import winreg
        roots = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                 (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                 (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")]
        for hive, key in roots:
            try:
                with winreg.OpenKey(hive, key) as k:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sub:
                                dn = winreg.QueryValueEx(sub, "DisplayName")[0]
                        except OSError:
                            continue
                        t = _tokens(dn)
                        if t:
                            names.setdefault(t[0], dn)
            except OSError:
                pass
    return names


# ---------------------------------------------------------------- notifications

_TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $x.GetElementsByTagName('text'); $t[0].AppendChild($x.CreateTextNode($env:RADAR_TITLE)) > $null; $t[1].AppendChild($x.CreateTextNode($env:RADAR_BODY)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""


def notify(title, body):
    try:
        if IS_MAC:
            subprocess.run(["osascript", "-e", "on run argv", "-e",
                            "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run", title, body], timeout=20)
        elif IS_WIN:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST_PS], timeout=30,
                           env={**os.environ, "RADAR_TITLE": title, "RADAR_BODY": body}, **quiet())
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, body], timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        pass


def info():
    return {"os": NAME, **LABELS}


if __name__ == "__main__":
    print(json.dumps({**info(), "data": str(data_dir()), "home": str(HOME), "time": time.time()}, indent=2))
