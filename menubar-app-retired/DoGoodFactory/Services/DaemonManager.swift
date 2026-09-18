import Foundation

class DaemonManager {
    static let shared = DaemonManager()

    private let uid = getuid()

    func loadAll() {
        for config in DaemonConfig.all {
            load(config)
        }
    }

    func unloadAll() {
        for config in DaemonConfig.all {
            unload(config)
        }
    }

    func load(_ config: DaemonConfig) {
        guard config.plistExists else { return }
        shell("launchctl enable gui/\(uid)/\(config.label) 2>&1")
        if isLoaded(config.label) { return }
        shell("launchctl bootstrap gui/\(uid) \"\(config.plistPath)\" 2>&1")
    }

    func unload(_ config: DaemonConfig) {
        // 1. Capture the daemon PID from launchctl BEFORE booting it out.
        //    `launchctl list <label>` prints a plist-ish dict with `"PID" = N;`.
        let pidLine = shell("launchctl list \"\(config.label)\" 2>&1 | awk '/\"PID\"/ {gsub(/[^0-9]/,\"\",$3); print $3}'")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let rootPid = Int(pidLine) ?? 0

        // 2. Bootout the launchd job (stops respawn).
        if isLoaded(config.label) {
            shell("launchctl bootout gui/\(uid)/\(config.label) 2>&1")
        }

        // 3. Kill the entire process tree. launchd sends SIGTERM on bootout,
        //    but child helpers (claude CLI, python workers) often survive
        //    because they detached from the parent's process group. We
        //    SIGTERM the pgid, wait briefly, then SIGKILL anything left.
        if rootPid > 0 {
            // pkill -TERM -g <pgid> kills the whole group launchd assigned
            shell("/bin/kill -TERM -\(rootPid) 2>/dev/null; pkill -TERM -P \(rootPid) 2>/dev/null; true")
            usleep(750_000)
            shell("/bin/kill -KILL -\(rootPid) 2>/dev/null; pkill -KILL -P \(rootPid) 2>/dev/null; true")
        }

        // 4. Belt-and-suspenders: kill any surviving process whose command
        //    line references this daemon's label (catches double-forked helpers).
        shell("pkill -TERM -f \"\(config.label)\" 2>/dev/null; true")
        usleep(500_000)
        shell("pkill -KILL -f \"\(config.label)\" 2>/dev/null; true")
    }

    func restart(_ config: DaemonConfig) {
        guard config.plistExists else { return }
        unload(config)
        usleep(500_000)
        load(config)
    }

    func toggle(_ config: DaemonConfig, currentlyLoaded: Bool, currentlyRunning: Bool) {
        if currentlyRunning {
            // Running → stop it
            unload(config)
        } else {
            // Ensure enabled and loaded, then kickstart
            shell("launchctl enable gui/\(uid)/\(config.label) 2>&1")
            if !isLoaded(config.label) {
                shell("launchctl bootstrap gui/\(uid) \"\(config.plistPath)\" 2>&1")
                usleep(500_000)
            }
            kickstart(config)
        }
    }

    func kickstart(_ config: DaemonConfig) {
        shell("launchctl kickstart gui/\(uid)/\(config.label) 2>&1")
    }

    func isLoaded(_ label: String) -> Bool {
        let output = shell("launchctl list \"\(label)\" 2>&1")
        return !output.contains("Could not find service")
    }

    @discardableResult
    private func shell(_ command: String) -> String {
        let process = Process()
        let pipe = Pipe()

        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = ["-c", command]
        process.standardOutput = pipe
        process.standardError = pipe

        do {
            try process.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            return String(data: data, encoding: .utf8) ?? ""
        } catch {
            print("Process error: \(error)")
            return ""
        }
    }
}
