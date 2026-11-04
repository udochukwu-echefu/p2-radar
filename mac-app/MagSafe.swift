// The MagSafe 3 charging light (SMC key ACLC) on Apple silicon MacBooks.
//
// Reading it works as the user, through the helper bundled in the app.
// Changing it needs root, and macOS resets it on replug, wake and charge
// changes, so a one-time setup (one admin password prompt) installs a root
// daemon that keeps re-applying the mode written in a file the user owns
// (it re-reads the file every second).
// After that, picking a mode only rewrites that file.

import AppKit

enum MagSafe {
    static let label = "com.udo.radar.magsafe-led"
    // Root runs this copy, so it lives in a root-owned folder, never inside the app bundle.
    static let helper = "/Library/PrivilegedHelperTools/\(label)"
    static let plist = "/Library/LaunchDaemons/\(label).plist"
    static let log = "/var/log/radar-magsafe-led.log"
    static let modeFile = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Project Radar/magsafe-mode")
    static var bundled: String? { Bundle.main.path(forResource: "magsafe-led", ofType: nil) }

    enum Mode: String, CaseIterable {
        case off, green, orange, system
        var title: String {
            switch self {
            case .off: return "Off"
            case .green: return "Green"
            case .orange: return "Orange"
            case .system: return "Let macOS Decide"
            }
        }
    }

    /// What the light shows right now ("off", "green", "orange", "system" or "other"),
    /// or nil when this Mac has no MagSafe 3 light.
    static func status() -> String? {
        guard let bundled else { return nil }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: bundled)
        p.arguments = ["status"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        guard p.terminationStatus == 0, let text = String(data: data, encoding: .utf8) else { return nil }
        return text.split(separator: " ").first.map(String.init)
    }

    /// The daemon counts as installed when its launchd job file exists.
    static var installed: Bool { FileManager.default.fileExists(atPath: plist) }

    static var wanted: Mode? {
        guard let text = try? String(contentsOf: modeFile, encoding: .utf8) else { return nil }
        return Mode(rawValue: text.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    static func setWanted(_ mode: Mode) throws {
        try FileManager.default.createDirectory(at: modeFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        try (mode.rawValue + "\n").write(to: modeFile, atomically: true, encoding: .utf8)
    }

    /// Copies the helper to its root-owned home, writes the launchd job and starts it.
    static func install() -> String? {
        guard let commands = installCommands() else { return "The helper is missing from the app. Rebuild it with build.sh." }
        if wanted == nil { try? setWanted(.system) }   // start by changing nothing
        return runAsAdmin(commands, prompt: "P2 Radar needs your password once to control the MagSafe light.")
    }

    static func installCommands() -> [String]? {
        guard let bundled else { return nil }
        let job: [String: Any] = [
            "Label": label,
            "ProgramArguments": [helper, "daemon", "--config", modeFile.path],
            "RunAtLoad": true,
            "KeepAlive": true,
            "StandardErrorPath": log,
        ]
        guard let json = try? JSONSerialization.data(withJSONObject: job),
              let jobText = String(data: json, encoding: .utf8) else { return nil }
        return [
            "set -e",
            "/bin/mkdir -p /Library/PrivilegedHelperTools",
            "/usr/bin/install -m 755 -o root -g wheel \(sh(bundled)) \(sh(helper))",
            "/usr/bin/printf '%s' \(sh(jobText)) > \(sh(plist))",
            "/usr/bin/plutil -convert xml1 \(sh(plist))",
            "/usr/sbin/chown root:wheel \(sh(plist))",
            "/bin/chmod 644 \(sh(plist))",
            "/bin/launchctl bootout system \(sh(plist)) 2>/dev/null || true",
            "/bin/launchctl bootstrap system \(sh(plist))",
        ]
    }

    /// Stops the daemon, hands the light back to macOS and removes everything setup added.
    static func uninstall() -> String? {
        runAsAdmin(uninstallCommands, prompt: "P2 Radar needs your password to remove its MagSafe light control.")
    }

    static var uninstallCommands: [String] {
        [
            "/bin/launchctl bootout system \(sh(plist)) 2>/dev/null || true",
            "\(sh(helper)) on >/dev/null 2>&1 || true",
            "/bin/rm -f \(sh(helper)) \(sh(plist)) \(sh(log))",
        ]
    }

    /// Runs shell commands as root through macOS's own password prompt.
    /// Returns nil on success, "" when the user cancelled, or the error.
    private static func runAsAdmin(_ commands: [String], prompt: String) -> String? {
        let script = commands.joined(separator: "; ")
        let source = "do shell script \(applescript(script)) with prompt \(applescript(prompt)) with administrator privileges"
        var error: NSDictionary?
        NSAppleScript(source: source)?.executeAndReturnError(&error)
        guard let error else { return nil }
        if (error[NSAppleScript.errorNumber] as? Int) == -128 { return "" }   // cancelled
        return error[NSAppleScript.errorMessage] as? String ?? "Something went wrong."
    }

    /// A shell word: single-quoted, with any single quote closed, escaped and reopened.
    private static func sh(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }

    /// An AppleScript string literal.
    private static func applescript(_ s: String) -> String {
        "\"" + s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }
}
