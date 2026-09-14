import Foundation

class WorkerMonitor {

    private let factoryStatusFile = "/tmp/dogood-factory-status.json"

    func discoverWorkers() -> [WorkerInfo] {
        // Merge: status file agents + live process discovery
        var workers = discoverFromStatusFile()
        let liveWorkers = discoverFromProcesses()

        // Merge live PIDs into status-file workers, and add any live workers not in status file
        for live in liveWorkers {
            if let idx = workers.firstIndex(where: {
                $0.repoName == live.repoName && $0.modelLabel == live.modelLabel
            }) {
                // Update PID from live process
                workers[idx] = WorkerInfo(
                    pid: live.pid,
                    issueId: workers[idx].issueId,
                    agentId: workers[idx].agentId,
                    modelLabel: workers[idx].modelLabel,
                    repoName: workers[idx].repoName,
                    workerType: workers[idx].workerType)
            } else {
                // Live agent not in status file — add it
                workers.append(live)
            }
        }

        return workers
    }

    /// Kill a helper agent by PID
    static func killAgent(pid: Int) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/kill")
        process.arguments = ["-TERM", String(pid)]
        try? process.run()
        process.waitUntilExit()
        // Also kill child processes (claude spawns sub-processes)
        let killChildren = Process()
        killChildren.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        killChildren.arguments = ["-TERM", "-P", String(pid)]
        try? killChildren.run()
        killChildren.waitUntilExit()
    }

    // MARK: - Discovery from status JSON

    private func discoverFromStatusFile() -> [WorkerInfo] {
        guard FileManager.default.fileExists(atPath: factoryStatusFile),
              let data = FileManager.default.contents(atPath: factoryStatusFile),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let agents = json["agents"] as? [String: Any] else {
            return []
        }

        // Only trust status file if updated recently (within 5 minutes)
        if let updatedStr = json["updated"] as? String {
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            var updatedDate: Date? = formatter.date(from: updatedStr)
            if updatedDate == nil {
                let df = DateFormatter()
                df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSS"
                df.timeZone = TimeZone.current
                updatedDate = df.date(from: updatedStr)
            }
            if updatedDate == nil {
                let df = DateFormatter()
                df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
                df.timeZone = TimeZone.current
                updatedDate = df.date(from: updatedStr)
            }
            if let date = updatedDate, Date().timeIntervalSince(date) > 300 {
                return []
            }
        }

        return agents.compactMap { (agentId, info) -> WorkerInfo? in
            guard let infoDict = info as? [String: Any] else { return nil }
            let repo = infoDict["repo"] as? String ?? "unknown"
            let issue = infoDict["issue"] as? String ?? ""
            let type = infoDict["type"] as? String ?? "fix"
            let model = infoDict["model"] as? String ?? "unknown"

            let issueNum = Int(issue.replacingOccurrences(of: "#", with: "")) ?? 0

            return WorkerInfo(pid: 0, issueId: issueNum,
                              agentId: agentId, modelLabel: model,
                              repoName: repo, workerType: type)
        }
    }

    // MARK: - Discovery from running processes

    private func discoverFromProcesses() -> [WorkerInfo] {
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/ps")
        process.arguments = ["-eo", "pid,command"]
        process.standardOutput = pipe
        try? process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()

        guard let output = String(data: data, encoding: .utf8) else { return [] }

        var workers: [WorkerInfo] = []

        for line in output.components(separatedBy: .newlines) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            // Look for claude processes with --system-prompt (these are helper agents)
            guard trimmed.contains("claude") && trimmed.contains("--system-prompt") else { continue }

            // Parse PID (first token)
            let parts = trimmed.components(separatedBy: .whitespaces).filter { !$0.isEmpty }
            guard let pid = Int(parts.first ?? "") else { continue }

            // Extract model
            var model = "unknown"
            if let modelRange = trimmed.range(of: "--model ") {
                let afterModel = trimmed[modelRange.upperBound...]
                model = String(afterModel.prefix(while: { !$0.isWhitespace }))
            }

            // Determine worker type from system prompt
            var workerType = "fix"
            let promptLower = trimmed.lowercased()
            if promptLower.contains("bounty") {
                workerType = "bounty"
            } else if promptLower.contains("feedback") || promptLower.contains("revision") {
                workerType = "feedback"
            }

            // Shorten model name for display
            let shortModel: String
            if model.contains("haiku") {
                shortModel = "haiku"
            } else if model.contains("opus") {
                shortModel = "opus"
            } else if model.contains("sonnet") {
                shortModel = "sonnet"
            } else {
                shortModel = model
            }

            let agentId = "pid-\(pid)"
            workers.append(WorkerInfo(
                pid: pid, issueId: 0, agentId: agentId,
                modelLabel: shortModel, repoName: nil,
                workerType: workerType))
        }

        return workers
    }
}
