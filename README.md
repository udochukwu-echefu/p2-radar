# Radar

A small Mac app that keeps an eye on your work and your disk.

- **Projects**: finds every git repo in your home folder and shows which ones have work that isn't backed up: no remote at all, uncommitted changes, or commits that were never pushed. Back them up in one click.
- **Cleanup**: sorts Downloads, Desktop and storage (dependency folders, package and app caches, Xcode data) and pre-ticks only what's safe. Everything goes to the Trash, with undo.
- **Secrets**: finds credential files tracked in git, about to be committed, or lying around in Desktop, Documents and Downloads.
- **Convert**: one-click conversion between Word, RTF, text, Markdown, HTML, PDF and image formats.

Everything runs locally. Radar's dashboard is served on `127.0.0.1` only, and nothing leaves your Mac unless you press Push or Back up.

## Install

Requirements: macOS 14 or later, Python 3.8+ (from [python.org](https://www.python.org/downloads/macos/) or Homebrew), and the Xcode command line tools (`xcode-select --install`) to build the app. The [GitHub CLI](https://cli.github.com) (`gh`) is optional and enables "Back up to GitHub".

```bash
git clone https://github.com/udochukwu-echefu/project-radar.git
cd project-radar
./mac-app/build.sh
open ~/Applications/Radar.app
```

On first launch macOS asks for access to Desktop, Documents and Downloads, and for permission to send notifications. Allow them.

Optional weekly check (Mondays at 10:00, posts a notification when work isn't backed up):

```bash
./mac-app/weekly.sh install
```

### Without the app

The engine also runs on its own and serves the same dashboard in your browser (PDF conversion and the menu bar need the app):

```bash
python3 project-radar/radar.py serve       # dashboard at http://127.0.0.1:8765
python3 project-radar/radar.py             # project summary in the terminal
python3 project-radar/radar.py downloads   # Downloads dry run
```

## Features

### Projects
- Groups repos into **Only on this Mac**, **Uncommitted**, **Not pushed** and **Backed up**, with a backup-progress bar on each card.
- **Snapshot** commits current changes after showing every file. Files that look like secrets (`.env`, keys, `client_secret*.json`), anything over 50 MB, and `node_modules`/`venv`/caches are held back automatically.
- **Back up all** reviews every local-only project at once: an editable repo name each, then a snapshot and a new **private** GitHub repo for all of them. **Push all** does the same for unpushed commits.
- **Copies**: finds the same project in several folders. **Compare** shows the commits each copy has that the others don't; **Bring into newest** fetches a copy's branches as `radar/<copy>/<branch>` without merging; **Remove** only appears when a copy has nothing unique and no unsaved work.
- **Live**: cards update as files change, including commits and pushes made elsewhere.
- **⌘K** jumps to any project: ↵ opens it in VS Code, ⌘↵ in Terminal, ⌥↵ in Finder, ⇧↵ shows its card.
- Repos untouched for a year are filed under **Old** so they don't crowd current work.

### Cleanup
| Area | Pre-ticked | Left for you |
|---|---|---|
| Downloads | Installers for apps you already have, exact duplicates, unfinished downloads, unpacked archives, old screenshots | Big videos and archives, credential files, everything else |
| Desktop | Screenshots and recordings older than a month | Everything else. Git projects are never ticked. |
| Storage | npm/pip caches, re-downloadable caches, `node_modules` in projects idle for 2 months, Xcode DerivedData, AI-tool folders that only hold a copied skill | App caches of apps you use, Docker (information only) |

Undo puts the last batch back until you empty the Trash.

### Secrets
Credential files tracked in git (with **Stop tracking**, which removes them from the index and adds them to `.gitignore`; rotate the key, since it's still in history), not ignored yet (**Add to .gitignore**), or loose in your folders (**Move to Trash**).

### Convert
Drop files onto the window or pick a recent file. Documents to and from PDF use macOS's own text system and PDFKit; images use `sips`; document formats use `textutil`. Output is saved next to the original and never overwrites it.

### Mac app
Dock badge and a menu bar item with the projects that need attention; it keeps running in the menu bar when the window is closed. Light and dark themes.

## How it works

```
mac-app/main.swift        native shell: window (WKWebView), menu bar, notifications,
                          FSEvents file watching, PDF conversion (Radar --convert)
project-radar/radar.py    scanning, git actions, local server, live updates (server-sent events)
project-radar/downloads.py   Downloads/Desktop sweep        storage.py   storage sweep
project-radar/leaks.py       secrets check                  convert.py   converter
project-radar/dashboard.html the dashboard
```

The engine is Python standard library only. The server answers only on `127.0.0.1`, checks the `Host` header, and requires a per-launch token for every action. State (scan cache, hidden projects, the undo log) lives in `~/Library/Application Support/Project Radar/`.

```bash
./mac-app/build.sh            # rebuild and reinstall the app
./mac-app/build.sh --engine   # update only the engine and dashboard, without re-signing
```

### Keeping permissions across rebuilds
An ad-hoc signed app can lose its folder permissions when it's rebuilt. Create a self-signed code-signing certificate once and `build.sh` uses it automatically: Keychain Access › Certificate Assistant › Create a Certificate…, name it `Radar Local Signing`, type **Self-Signed Root**, certificate type **Code Signing**. Or set `RADAR_SIGN_ID` to another identity.

## License

[MIT](LICENSE)
