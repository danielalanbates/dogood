import Foundation

class LaunchdMonitor {

    func checkAll(configs: [DaemonConfig]) -> [DaemonState] {
        let listOutput = shell("launchctl list")
        var summaryByLabel: [String: (pid: Int?, exitStatus: Int)] = [:]

        for line in listOutput.split(separator: "\n") {
            let parts = line.split(separator: "\t", maxSplits: 2)
            guard parts.count == 3 else { continue }
            let label = String(parts[2])
            let pid = parts[0] == "-" ? nil : Int(parts[0])
            let exitStatus = Int(parts[1]) ?? -1
            summaryByLabel[label] = (pid, exitStatus)
        }

        return configs.map { config in
            guard let summary = summaryByLabel[config.label] else {
                return DaemonState(config: config, health: .notLoaded,
                                   pid: nil, lastExitStatus: nil)
            }

            if let pid = summary.pid {
                return DaemonState(config: config, health: .running,
                                   pid: pid, lastExitStatus: summary.exitStatus)
            }

            if summary.exitStatus != 0 {
                return DaemonState(config: config, health: .crashed,
                                   pid: nil, lastExitStatus: summary.exitStatus)
            }

            if config.isCyclic {
                return DaemonState(config: config, health: .betweenCycles,
                                   pid: nil, lastExitStatus: 0)
            } else {
                return DaemonState(config: config, health: .stopped,
                                   pid: nil, lastExitStatus: 0)
            }
        }
    }

    private func shell(_ command: String) -> String {
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = ["-c", command]
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        try? process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return String(data: data, encoding: .utf8) ?? ""
    }
}
