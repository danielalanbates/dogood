import Foundation

struct DaemonConfig {
    let label: String
    let displayName: String
    let isCyclic: Bool

    var plistPath: String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return "\(home)/Library/LaunchAgents/\(label).plist"
    }

    var plistExists: Bool {
        FileManager.default.fileExists(atPath: plistPath)
    }

    // MARK: - Do Good Factory Daemons

    /// Daemons shown in the menu UI
    static let all: [DaemonConfig] = [
        DaemonConfig(label: "com.batesai.dogood.feedback",
                     displayName: "Feedback Daemon", isCyclic: true),
        DaemonConfig(label: "com.batesai.dogood.telegramd",
                     displayName: "Telegram Daemon", isCyclic: false),
        DaemonConfig(label: "com.batesai.dogood.bountywatch",
                     displayName: "Bounty Watch", isCyclic: false),
    ]

    /// Factory orchestrator — not shown in menu, used internally to launch helpers
    static let factory = DaemonConfig(
        label: "com.batesai.dogood.factory",
        displayName: "Factory", isCyclic: false)
}
