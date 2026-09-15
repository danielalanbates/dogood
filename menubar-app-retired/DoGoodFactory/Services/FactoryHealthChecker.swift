import Foundation

/// Health state of the agent factory.
struct FactoryHealth {
    let status: FactoryHealthStatus
    let activeAgents: Int
    let recentSuccesses: Int
    let recentFailures: Int
    let pauseReason: String?  // e.g. "Rate limited until 11am"
    let lastActivity: Date?

    var summaryLine: String {
        switch status {
        case .healthy:
            if activeAgents > 0 {
                return "\(activeAgents) agents active, \(recentSuccesses) PRs recent"
            } else {
                return "Spawning next agent, \(recentSuccesses) PRs recent"
            }
        case .rateLimited:
            return pauseReason ?? "Rate limited"
        case .allFailing:
            return "\(recentFailures) failures, 0 successes"
        case .idle:
            return "No recent activity"
        case .unknown:
            return "Unable to read status"
        }
    }

    var emoji: String {
        switch status {
        case .healthy: return "\u{1F7E2}"     // green
        case .rateLimited: return "\u{1F7E1}" // yellow
        case .allFailing: return "\u{1F534}"  // red
        case .idle: return "\u{26AA}"          // white
        case .unknown: return "\u{2B1C}"       // white square
        }
    }
}

enum FactoryHealthStatus {
    case healthy       // Agents are running and producing PRs
    case rateLimited   // Plan-level rate limit active
    case allFailing    // Agents running but all failing
    case idle          // No recent activity
    case unknown       // Can't determine
}

/// Checks factory health by reading log files and signal files.
class FactoryHealthChecker {
    private let factoryLog = "/tmp/dogood-factory.log"
    private let pauseFile = "/tmp/dogood-plan-pause.json"
    private let statusFile = "/tmp/dogood-factory-status.json"

    func check() -> FactoryHealth {
        // Check plan-level rate limit pause first
        if let pauseInfo = readPauseFile() {
            return FactoryHealth(
                status: .rateLimited,
                activeAgents: 0,
                recentSuccesses: 0,
                recentFailures: 0,
                pauseReason: pauseInfo,
                lastActivity: nil
            )
        }

        // Parse recent factory log entries
        let logStats = parseRecentLog()

        if !logStats.factoryRunning, factoryProcessAlive(), let reason = lastSleepReason() {
            return FactoryHealth(
                status: .rateLimited,
                activeAgents: 0,
                recentSuccesses: logStats.successes,
                recentFailures: logStats.failures,
                pauseReason: reason,
                lastActivity: nil
            )
        }

        let status: FactoryHealthStatus
        if logStats.successes > 0 || logStats.active > 0 {
            status = .healthy
        } else if logStats.factoryRunning {
            // Factory process is alive but between agents — still healthy
            status = .healthy
        } else if logStats.failures > 0 && logStats.successes == 0 {
            status = .allFailing
        } else if logStats.successes == 0 && logStats.failures == 0 && logStats.active == 0 {
            status = .idle
        } else {
            status = .unknown
        }

        return FactoryHealth(
            status: status,
            activeAgents: logStats.active,
            recentSuccesses: logStats.successes,
            recentFailures: logStats.failures,
            pauseReason: nil,
            lastActivity: logStats.lastActivity
        )
    }

    private func factoryProcessAlive() -> Bool {
        guard let text = try? String(contentsOfFile: "/tmp/dogood-factory.pid", encoding: .utf8),
              let pid = Int32(text.trimmingCharacters(in: .whitespacesAndNewlines)) else { return false }
        return kill(pid, 0) == 0
    }

    /// The factory logs "Sleeping Nh, will auto-resume" while paused on limits.
    private func lastSleepReason() -> String? {
        guard let data = FileManager.default.contents(atPath: factoryLog),
              let content = String(data: data, encoding: .utf8) else { return nil }
        let lines = content.suffix(3000).components(separatedBy: "\n").filter { !$0.isEmpty }
        guard let last = lines.last, last.contains("Sleeping") || last.contains("[PR SAFETY]") else { return nil }
        let paused = lines.last { $0.contains("FACTORY PAUSED") } ?? last
        return paused.replacingOccurrences(of: "[DIAGNOSIS]", with: "").trimmingCharacters(in: .whitespaces)
    }

    private func readPauseFile() -> String? {
        guard FileManager.default.fileExists(atPath: pauseFile),
              let data = FileManager.default.contents(atPath: pauseFile),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        let reset = json["reset"] as? String ?? "unknown"
        return "Rate limited \u{2014} resets \(reset)"
    }

    private struct LogStats {
        var active: Int = 0
        var successes: Int = 0
        var failures: Int = 0
        var skipped: Int = 0
        var factoryRunning: Bool = false
        var lastActivity: Date?
    }

    private func parseRecentLog() -> LogStats {
        var stats = LogStats()

        guard let data = FileManager.default.contents(atPath: factoryLog),
              let content = String(data: data, encoding: .utf8) else {
            return stats
        }

        // Only look at the last ~5000 chars (recent activity)
        let tail: String
        if content.count > 5000 {
            tail = String(content.suffix(5000))
        } else {
            tail = content
        }

        // Find the most recent "Agent Factory starting" to scope to current run
        let lines = tail.components(separatedBy: "\n")
        var currentRunLines: [String] = []
        for line in lines {
            if line.contains("Agent Factory starting") {
                currentRunLines = []  // Reset — new run started
            }
            currentRunLines.append(line)
        }

        for line in currentRunLines {
            if line.contains("PR created") || line.contains("pr_created") {
                stats.successes += 1
            } else if line.contains("failed —") || line.contains("PLAN RATE LIMIT") {
                stats.failures += 1
            } else if line.contains("skipped —") {
                stats.skipped += 1
            }
        }

        // Read from factory status file (written by orchestrator)
        let statusInfo = readStatusFile()
        stats.active = statusInfo.active
        stats.factoryRunning = statusInfo.running
        stats.lastActivity = Date()  // We just read the log, so it's recent

        return stats
    }

    private func readStatusFile() -> (active: Int, running: Bool) {
        guard FileManager.default.fileExists(atPath: statusFile),
              let data = FileManager.default.contents(atPath: statusFile),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return (countActiveAgentsFromPS(), false)
        }
        let active = json["active_agents"] as? Int ?? 0
        let running = json["factory_running"] as? Bool ?? false
        return (active, running)
    }

    private func countActiveAgentsFromPS() -> Int {
        // Fallback: count via ps
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/ps")
        process.arguments = ["-e", "-o", "command"]
        let pipe = Pipe()
        process.standardOutput = pipe

        do {
            try process.run()
            process.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            guard let output = String(data: data, encoding: .utf8) else { return 0 }
            return output.components(separatedBy: "\n")
                .filter { $0.contains("src.cli solve") || $0.contains("src.cli solve-feedback") }
                .count
        } catch {
            return 0
        }
    }
}
