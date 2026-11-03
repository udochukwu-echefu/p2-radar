// Radar.app: a native window around the Radar engine (radar.py).
// The app starts the engine on a free local port, shows its dashboard in a
// WKWebView, badges the Dock icon, lives in the menu bar, watches the disk
// for changes so cards update live, and stops the engine on quit.
// Headless modes: `Radar --notify` (weekly check, native notification) and
// `Radar --convert <in> <out>` (PDF conversions for the engine).

import Cocoa
import PDFKit
import UserNotifications
import WebKit

// The engine runs from Application Support when build.sh has synced it there,
// so dashboard and engine updates don't change the app's signature (which
// would make macOS ask for folder access again). The bundled copy is the fallback.
let syncedEngine = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/Project Radar/engine")
let engineDir = FileManager.default.fileExists(atPath: syncedEngine.appendingPathComponent("radar.py").path)
    ? syncedEngine
    : Bundle.main.resourceURL!.appendingPathComponent("radar")
let engineScript = engineDir.appendingPathComponent("radar.py").path
let appSecret = UUID().uuidString   // lets only this app report file changes to the engine

func findPython() -> String? {
    let candidates = [
        "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ]
    return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
}

func engineEnvironment() -> [String: String] {
    var env = ProcessInfo.processInfo.environment
    // Apps launched from Finder get a bare PATH; git, gh and code live here.
    env["PATH"] = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"  // never write __pycache__ into the signed bundle
    env["RADAR_APP"] = "1"
    env["RADAR_APP_BIN"] = Bundle.main.executablePath ?? ""
    env["RADAR_APP_SECRET"] = appSecret
    return env
}

func printJSON(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) { print(s) }
}

// ------------------------------------------------------------- headless: convert

struct ConvertError: Error { let message: String }

func documentToPDF(_ src: URL, _ dst: URL) throws {
    _ = NSApplication.shared
    let str = try NSAttributedString(url: src, options: [:], documentAttributes: nil)
    let info = NSPrintInfo()
    info.paperSize = NSSize(width: 595, height: 842)   // A4
    info.topMargin = 56; info.bottomMargin = 56; info.leftMargin = 56; info.rightMargin = 56
    info.horizontalPagination = .fit
    info.verticalPagination = .automatic
    info.jobDisposition = .save
    info.dictionary()[NSPrintInfo.AttributeKey.jobSavingURL] = dst
    let view = NSTextView(frame: NSRect(x: 0, y: 0, width: 595 - 112, height: 842 - 112))
    view.textStorage?.setAttributedString(str)
    view.sizeToFit()
    let op = NSPrintOperation(view: view, printInfo: info)
    op.showsPrintPanel = false
    op.showsProgressPanel = false
    if !op.run() { throw ConvertError(message: "Couldn't lay out the document") }
}

func pdfPages(_ src: URL, _ dst: URL) throws -> [String] {
    guard let doc = PDFDocument(url: src) else { throw ConvertError(message: "Couldn't open the PDF") }
    var outputs: [String] = []
    let base = dst.deletingPathExtension()
    for i in 0..<doc.pageCount {
        guard let page = doc.page(at: i) else { continue }
        let b = page.bounds(for: .mediaBox)
        let img = page.thumbnail(of: NSSize(width: b.width * 2, height: b.height * 2), for: .mediaBox)
        guard let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else { continue }
        let out = doc.pageCount == 1 ? dst : URL(fileURLWithPath: base.path + "-\(i + 1).png")
        try png.write(to: out)
        outputs.append(out.path)
    }
    return outputs
}

if let i = CommandLine.arguments.firstIndex(of: "--convert"), CommandLine.arguments.count > i + 2 {
    let src = URL(fileURLWithPath: CommandLine.arguments[i + 1])
    let dst = URL(fileURLWithPath: CommandLine.arguments[i + 2])
    do {
        let inExt = src.pathExtension.lowercased(), outExt = dst.pathExtension.lowercased()
        var outputs = [dst.path]
        if inExt == "pdf" && outExt == "txt" {
            guard let doc = PDFDocument(url: src) else { throw ConvertError(message: "Couldn't open the PDF") }
            try (doc.string ?? "").write(to: dst, atomically: true, encoding: .utf8)
        } else if inExt == "pdf" && outExt == "png" {
            outputs = try pdfPages(src, dst)
        } else if outExt == "pdf" {
            try documentToPDF(src, dst)
        } else {
            throw ConvertError(message: "Radar can't turn .\(inExt) into .\(outExt)")
        }
        printJSON(["ok": true, "outputs": outputs])
        exit(0)
    } catch let e as ConvertError {
        printJSON(["ok": false, "error": e.message]); exit(1)
    } catch {
        printJSON(["ok": false, "error": error.localizedDescription]); exit(1)
    }
}

// ------------------------------------------------------------- headless: weekly notify

if CommandLine.arguments.contains("--notify") {
    guard let python = findPython() else {
        print("Radar: Python 3 not found")
        exit(1)
    }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: python)
    p.arguments = [engineScript, "notify"]
    var env = engineEnvironment()
    env["RADAR_NOTIFY_JSON"] = "1"
    p.environment = env
    let out = Pipe()
    p.standardOutput = out
    try p.run()
    p.waitUntilExit()
    let text = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
    print(text, terminator: "")
    guard let line = text.split(separator: "\n").first(where: { $0.hasPrefix("RADAR_NOTIFY=") }),
          let data = line.dropFirst("RADAR_NOTIFY=".count).data(using: .utf8),
          let info = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { exit(p.terminationStatus) }

    let center = UNUserNotificationCenter.current()
    let sem = DispatchSemaphore(value: 0)
    var allowed = false
    center.getNotificationSettings { s in
        allowed = [.authorized, .provisional].contains(s.authorizationStatus)
        sem.signal()
    }
    _ = sem.wait(timeout: .now() + 5)
    let body = info["body"] as? String ?? ""
    if allowed {
        let content = UNMutableNotificationContent()
        content.title = info["title"] as? String ?? "Radar"
        content.body = body
        content.sound = .default
        center.add(UNNotificationRequest(identifier: "weekly-\(Date().timeIntervalSince1970)", content: content, trigger: nil)) { _ in sem.signal() }
        _ = sem.wait(timeout: .now() + 10)
    } else {
        // Notifications not allowed for Radar yet: fall back to a plain banner.
        let o = Process()
        o.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        o.arguments = ["-e", "on run argv", "-e", "display notification (item 1 of argv) with title \"Radar\"", "-e", "end run", body]
        try? o.run(); o.waitUntilExit()
    }
    exit(0)
}

// ------------------------------------------------------------- app

let pageBackground = NSColor(name: nil) { appearance in
    appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        ? NSColor(srgbRed: 0.039, green: 0.039, blue: 0.039, alpha: 1)   // dashboard --bg, oklch(0.145 0 0)
        : NSColor(srgbRed: 0.965, green: 0.967, blue: 0.973, alpha: 1)   // oklch(0.975 0.002 270)
}

func statusPage(_ title: String, _ detail: String, spinner: Bool) -> String {
    """
    <!doctype html><meta charset="utf-8"><style>
    :root { color-scheme: light dark; --bg:oklch(0.975 0.002 270); --text:oklch(0.22 0.008 270); --muted:oklch(0.46 0.01 270); --line:oklch(0.915 0.004 270); --blue:oklch(0.56 0.19 258); }
    @media (prefers-color-scheme: dark) { :root { --bg:oklch(0.145 0 0); --text:oklch(0.965 0 0); --muted:oklch(0.74 0 0); --line:oklch(0.255 0 0); --blue:oklch(0.64 0.17 258); } }
    html,body { margin:0; height:100%; background:var(--bg); color:var(--text); font:15px/1.5 -apple-system, system-ui, sans-serif; -webkit-user-select:none; }
    body { display:grid; place-items:center; text-align:center; }
    h1 { font-size:17px; letter-spacing:-.01em; font-weight:600; margin:16px 0 4px; }
    p { color:var(--muted); margin:0; max-width:46ch; }
    code { font: 12.5px ui-monospace, Menlo, monospace; }
    .s { width:26px; height:26px; margin:0 auto; border-radius:50%; border:2.5px solid var(--line); border-top-color:var(--blue); animation:r .9s linear infinite; }
    @keyframes r { to { transform:rotate(360deg) } }
    </style><body><div>\(spinner ? "<div class=s></div>" : "")<h1>\(title)</h1><p>\(detail)</p></div></body>
    """
}

func htmlEscape(_ s: String) -> String {
    s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;")
}

func jsString(_ s: String) -> String {
    let d = try? JSONSerialization.data(withJSONObject: [s])
    return d.flatMap { String(data: $0, encoding: .utf8) }.map { String($0.dropFirst().dropLast()) } ?? "\"\""
}

// Radar's app icon reduced to a menu bar template: the squircle outline,
// the dot matrix inside it with the sweep's dots solid and the rest faint,
// and the centre dot and one blip drawn larger.
func menuBarIcon() -> NSImage {
    let img = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
        NSColor.black.setStroke()
        let body = NSBezierPath(roundedRect: NSRect(x: 0.9, y: 0.9, width: 16.2, height: 16.2), xRadius: 4.6, yRadius: 4.6)
        body.lineWidth = 1.4
        body.stroke()
        let c = CGPoint(x: 9, y: 9), step: CGFloat = 2.75
        let lead: CGFloat = 52 * .pi / 180, trail: CGFloat = 80 * .pi / 180
        func square(_ p: CGPoint, _ s: CGFloat, _ alpha: CGFloat) {
            NSColor.black.withAlphaComponent(alpha).setFill()
            NSBezierPath(roundedRect: NSRect(x: p.x - s / 2, y: p.y - s / 2, width: s, height: s), xRadius: s * 0.3, yRadius: s * 0.3).fill()
        }
        for i in -2...2 {
            for j in -2...2 {
                let p = CGPoint(x: c.x + CGFloat(i) * step, y: c.y + CGFloat(j) * step)
                if hypot(p.x - c.x, p.y - c.y) > 6.3 || (i == 0 && j == 0) || (i == 1 && j == 2) { continue }
                var behind = lead - atan2(p.y - c.y, p.x - c.x)
                while behind < 0 { behind += 2 * .pi }
                square(p, 1.6, behind <= trail ? 1 : 0.4)
            }
        }
        square(c, 2.6, 1)                                               // you
        square(CGPoint(x: c.x + step, y: c.y + 2 * step), 2.5, 1)       // a project only on this Mac
        return true
    }
    img.isTemplate = true
    img.accessibilityDescription = "Radar"
    return img
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler,
                         NSMenuDelegate, UNUserNotificationCenterDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var engine: Process?
    var port: Int?
    var stdoutBuffer = ""
    var stderrBuffer = ""
    var quitting = false
    var statusItem: NSStatusItem!
    var summary: [String: Any] = [:]
    var watcher: FSEventStreamRef?
    var pendingPaths = Set<String>()
    var flushWork: DispatchWorkItem?
    let watchQueue = DispatchQueue(label: "radar.watch")

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        buildStatusItem()

        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "radar")
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setValue(false, forKey: "drawsBackground")  // no white flash before the page paints
        web.underPageBackgroundColor = pageBackground

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1320, height: 880),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Radar"
        window.titlebarAppearsTransparent = true
        window.backgroundColor = pageBackground
        window.minSize = NSSize(width: 720, height: 540)
        window.isReleasedWhenClosed = false   // closing hides it; Radar keeps running in the menu bar
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("RadarMain")
        window.makeKeyAndOrderFront(nil)

        web.loadHTMLString(statusPage("Starting Radar…", "Scanning your projects and folders.", spinner: true), baseURL: nil)
        startEngine()
        startWatching()

        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
        Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in self?.refreshSummary() }
        NSApp.activate()
    }

    // ---------------------------------------------------------- engine

    func startEngine() {
        guard let python = findPython() else {
            return showError("Radar needs Python 3", "Install it from <code>python.org</code>, then open Radar again.")
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [engineScript, "serve", "--no-open", "--port", "0"]
        p.environment = engineEnvironment()
        p.currentDirectoryURL = engineDir

        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let chunk = String(decoding: h.availableData, as: UTF8.self)
            DispatchQueue.main.async { self?.engineSaid(chunk) }
        }
        err.fileHandleForReading.readabilityHandler = { [weak self] h in
            let chunk = String(decoding: h.availableData, as: UTF8.self)
            DispatchQueue.main.async { self?.stderrBuffer = String(((self?.stderrBuffer ?? "") + chunk).suffix(4000)) }
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self, !self.quitting else { return }
                let tail = htmlEscape(String(self.stderrBuffer.suffix(1200)))
                self.showError("Radar stopped unexpectedly",
                               "Exit code \(proc.terminationStatus). Press ⌘⇧R to restart.<br><br><code style='white-space:pre-wrap;text-align:left;display:block'>\(tail)</code>")
            }
        }
        do {
            try p.run()
            engine = p
        } catch {
            showError("Couldn't start Radar", htmlEscape(error.localizedDescription))
        }
    }

    func engineSaid(_ chunk: String) {
        guard port == nil else { return }
        stdoutBuffer += chunk
        if let range = stdoutBuffer.range(of: #"RADAR_PORT=(\d+)"#, options: .regularExpression),
           let p = Int(stdoutBuffer[range].dropFirst("RADAR_PORT=".count)) {
            port = p
            web.load(URLRequest(url: URL(string: "http://127.0.0.1:\(p)/")!))
            refreshSummary()
        }
    }

    func showError(_ title: String, _ detail: String) {
        web.loadHTMLString(statusPage(title, detail, spinner: false), baseURL: nil)
    }

    @objc func restartEngine() {
        quitting = true
        engine?.terminate()
        engine?.waitUntilExit()
        quitting = false
        port = nil
        stdoutBuffer = ""
        stderrBuffer = ""
        web.loadHTMLString(statusPage("Restarting Radar…", "One moment.", spinner: true), baseURL: nil)
        startEngine()
    }

    // ---------------------------------------------------------- live file watching

    func startWatching() {
        var ctx = FSEventStreamContext(version: 0, info: Unmanaged.passUnretained(self).toOpaque(), retain: nil, release: nil, copyDescription: nil)
        let callback: FSEventStreamCallback = { _, info, count, paths, _, _ in
            guard let info else { return }
            let me = Unmanaged<AppDelegate>.fromOpaque(info).takeUnretainedValue()
            let list = unsafeBitCast(paths, to: NSArray.self) as? [String] ?? []
            me.noteChanges(list)
        }
        let flags = FSEventStreamCreateFlags(kFSEventStreamCreateFlagUseCFTypes | kFSEventStreamCreateFlagNoDefer)
        guard let stream = FSEventStreamCreate(nil, callback, &ctx, [NSHomeDirectory()] as CFArray,
                                               FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 1.0, flags) else { return }
        FSEventStreamSetDispatchQueue(stream, watchQueue)
        FSEventStreamStart(stream)
        watcher = stream
    }

    nonisolated func noteChanges(_ paths: [String]) {
        let skip = ["/Library/", "/.Trash", "/node_modules/", "/.git/objects/", "/.npm/", "/.cache/", "/Project Radar/"]
        let relevant = paths.filter { p in !skip.contains { p.contains($0) } }
        guard !relevant.isEmpty else { return }
        DispatchQueue.main.async {
            self.pendingPaths.formUnion(relevant)
            self.flushWork?.cancel()
            let work = DispatchWorkItem { [weak self] in self?.flushChanges() }
            self.flushWork = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2, execute: work)
        }
    }

    func flushChanges() {
        guard let port, !pendingPaths.isEmpty else { return }
        let paths = Array(pendingPaths.prefix(2000))
        pendingPaths.removeAll()
        var req = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/api/changed")!)
        req.httpMethod = "POST"
        req.setValue(appSecret, forHTTPHeaderField: "X-Radar-App")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["paths": paths])
        URLSession.shared.dataTask(with: req).resume()
    }

    // ---------------------------------------------------------- menu bar

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = statusItem.button {
            b.image = menuBarIcon()
            b.imagePosition = .imageLeading
        }
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu
    }

    func refreshSummary() {
        guard let port else { return }
        URLSession.shared.dataTask(with: URL(string: "http://127.0.0.1:\(port)/api/summary")!) { [weak self] data, _, _ in
            guard let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            DispatchQueue.main.async {
                self?.summary = obj
                let n = obj["atRisk"] as? Int ?? 0
                self?.statusItem.button?.title = n > 0 ? " \(n)" : ""
                NSApp.dockTile.badgeLabel = n > 0 ? String(n) : nil
            }
        }.resume()
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let n = summary["atRisk"] as? Int ?? 0
        let local = summary["localOnly"] as? Int ?? 0
        let head = NSMenuItem(title: n > 0 ? "\(n) projects need attention" + (local > 0 ? " · \(local) only on this Mac" : "") : "Everything is backed up",
                              action: nil, keyEquivalent: "")
        head.isEnabled = false
        menu.addItem(head)
        let items = summary["items"] as? [[String: Any]] ?? []
        if !items.isEmpty { menu.addItem(.separator()) }
        for it in items.prefix(8) {
            let name = it["name"] as? String ?? "", line = it["line"] as? String ?? ""
            let mi = NSMenuItem(title: "\(name)  —  \(line)", action: #selector(focusProject(_:)), keyEquivalent: "")
            mi.representedObject = it["path"] as? String
            mi.target = self
            let dot = NSImage(systemSymbolName: (it["lane"] as? Int ?? 0) == 0 ? "exclamationmark.circle" : "circle.dashed", accessibilityDescription: nil)
            mi.image = dot
            menu.addItem(mi)
        }
        menu.addItem(.separator())
        menu.addItem(withTitle: "Open Radar", action: #selector(showMain), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Go to Project…", action: #selector(openPalette), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Rescan", action: #selector(rescan), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Radar", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        refreshSummary()
    }

    @objc func showMain() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate()
    }

    @objc func focusProject(_ sender: NSMenuItem) {
        guard let path = sender.representedObject as? String else { return }
        showMain()
        web.evaluateJavaScript("window.radarFocus && radarFocus(\(jsString(path)))")
    }

    @objc func openPalette() {
        showMain()
        web.evaluateJavaScript("window.openPalette && openPalette()")
    }

    @objc func rescan() {
        web.evaluateJavaScript("typeof rescan === 'function' && rescan()")
    }

    // ---------------------------------------------------------- web view

    // Keep the window on the local dashboard; anything else opens in the browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        let local = url.host == "127.0.0.1" && url.port == port
        if url.scheme == "about" || local || action.targetFrame?.isMainFrame == false {
            return decisionHandler(.allow)
        }
        if url.isFileURL { return decisionHandler(.cancel) }   // a file dropped outside the drop zone
        NSWorkspace.shared.open(url)
        decisionHandler(.cancel)
    }

    // <input type="file"> in the converter
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.beginSheetModal(for: window) { r in completionHandler(r == .OK ? panel.urls : nil) }
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any] else { return }
        if let n = body["atRisk"] as? Int {
            NSApp.dockTile.badgeLabel = n > 0 ? String(n) : nil
            statusItem.button?.title = n > 0 ? " \(n)" : ""
            refreshSummary()
        }
        if let theme = body["theme"] as? String {
            // Keep the title bar in step with the page's light/dark toggle.
            window.appearance = NSAppearance(named: theme == "light" ? .aqua : .darkAqua)
        }
    }

    // ---------------------------------------------------------- notifications

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        DispatchQueue.main.async { self.showMain() }
        completionHandler()
    }

    // ---------------------------------------------------------- lifecycle

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { showMain() }
        return true
    }

    func applicationWillTerminate(_ note: Notification) {
        quitting = true
        if let watcher { FSEventStreamStop(watcher); FSEventStreamInvalidate(watcher); FSEventStreamRelease(watcher) }
        engine?.terminate()
    }

    func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Radar", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Radar", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Radar", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Go to Project…", action: #selector(openPalette), keyEquivalent: "k")
        view.addItem(withTitle: "Rescan", action: #selector(rescan), keyEquivalent: "r")
        view.addItem(withTitle: "Restart Engine", action: #selector(restartEngine), keyEquivalent: "R")
        view.addItem(.separator())
        view.addItem(withTitle: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f").keyEquivalentModifierMask = [.command, .control]
        viewItem.submenu = view
        main.addItem(viewItem)

        let windowItem = NSMenuItem()
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Radar", action: #selector(showMain), keyEquivalent: "0")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        windowItem.submenu = win
        main.addItem(windowItem)
        NSApp.windowsMenu = win

        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
