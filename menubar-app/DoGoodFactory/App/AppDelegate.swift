import AppKit

class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var statusIcon: StatusBarIcon!
    private var poller: StatusPoller!
    private var lastState: SystemState?
    private let daemonManager = DaemonManager.shared
    private var persistentMenu: NSMenu!
    var isLaunchingHelper = false
    var launchResultMessage: String?

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: 22)

        statusIcon = StatusBarIcon()
        statusIcon.mode = .loading
        statusIcon.button = statusItem.button

        persistentMenu = NSMenu()
        let loading = NSMenuItem(title: "Loading...", action: nil, keyEquivalent: "")
        loading.isEnabled = false
        persistentMenu.addItem(loading)
        persistentMenu.addItem(NSMenuItem.separator())
        persistentMenu.addItem(NSMenuItem(title: "Quit",
                                       action: #selector(NSApplication.terminate(_:)),
                                       keyEquivalent: "q"))
        statusItem.menu = persistentMenu

        // Load display daemons (factory is managed separately)
        daemonManager.loadAll()

        poller = StatusPoller(interval: 5.0) { [weak self] state in
            self?.updateMenu(state: state)
        }
        poller.start()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        // Do NOT unload daemons on quit — they should persist independently
        return .terminateNow
    }

    private static func pipelineOk() -> Bool {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        task.arguments = ["list", "com.batesai.dogood.factory"]
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = Pipe()
        do { try task.run() } catch { return false }
        task.waitUntilExit()
        let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        if task.terminationStatus != 0 || !out.contains("\"PID\"") { return false }
        for log in ["/tmp/dogood-scout.log", "/tmp/dogood-factory.log"] {
            guard let text = try? String(contentsOfFile: log, encoding: .utf8) else { continue }
            let last = text.split(separator: "\n").suffix(3).joined(separator: "\n").lowercased()
            if last.contains("traceback") || last.contains("cycle error") || last.contains("paused")
                || last.contains("sleeping") { return false }
        }
        return true
    }

    private func updateMenu(state: SystemState) {
        lastState = state
        let allHealthy = state.daemons.allSatisfy { $0.isHealthy || !$0.config.plistExists }
        let factoryOk = state.factoryHealth.map {
            $0.status == .healthy || $0.status == .idle
        } ?? true

        // Red whenever anything isn't working: a daemon down, the factory paused
        // on a limit, or Scout/Fixer's latest log line is an error.
        if !allHealthy || !factoryOk || !Self.pipelineOk() {
            statusIcon.mode = .unhealthy
        } else {
            statusIcon.mode = .healthy
        }

        // Update items in-place on the same NSMenu so the menu
        // refreshes live while it's open (no close/reopen needed)
        MenuBuilder.rebuild(persistentMenu, from: state, delegate: self)
    }

    // MARK: - Public menu refresh

    func refreshMenu() {
        poller.pollNow()
    }

    // MARK: - Daemon Actions

    @objc func toggleDaemon(_ sender: Any) {
        // Extract label from either NSMenuItem or ToggleRowView
        let label: String?
        if let menuItem = sender as? NSMenuItem {
            label = menuItem.representedObject as? String
        } else if let view = sender as? ToggleRowView {
            label = view.daemonLabel
        } else {
            return
        }
        guard let label = label,
              let config = DaemonConfig.all.first(where: { $0.label == label }) else { return }
        let loaded = daemonManager.isLoaded(label)
        let daemonState = lastState?.daemons.first(where: { $0.config.label == label })
        let running = daemonState?.health == .running || daemonState?.health == .betweenCycles
        DispatchQueue.global(qos: .userInitiated).async {
            self.daemonManager.toggle(config, currentlyLoaded: loaded, currentlyRunning: running)
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in
                self?.poller.pollNow()
            }
        }
    }

    // MARK: - Helper Agent Actions

    /// Kill a specific helper agent by PID
    @objc func killHelper(_ sender: Any) {
        guard let view = sender as? HelperToggleView, view.workerPid > 0 else { return }
        let pid = view.workerPid
        DispatchQueue.global(qos: .userInitiated).async {
            WorkerMonitor.killAgent(pid: pid)
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                self?.poller.pollNow()
            }
        }
    }

    /// Launch a new helper by kickstarting the factory daemon
    @objc func launchHelper() {
        guard !isLaunchingHelper else { return }
        isLaunchingHelper = true
        launchResultMessage = nil

        // Capture selected options before async dispatch
        let selectedModel = UserDefaults.standard.selectedAIModel
        let selectedHelperType = UserDefaults.standard.selectedHelperType

        // Immediately rebuild menu to show "Launching..." state
        if let state = lastState {
            MenuBuilder.rebuild(persistentMenu, from: state, delegate: self)
        }
        DispatchQueue.global(qos: .userInitiated).async {
            // Write selected AI model provider to signal file for factory to read
            try? selectedModel.id.write(toFile: "/tmp/dogood-selected-provider", atomically: true, encoding: .utf8)

            // First scan for new issues so the factory has something to work on
            self.runFactoryScan()

            // Clear any pause signals so the factory doesn't skip this run
            let pauseFiles = [
                "/tmp/dogood-plan-pause.json",
                "/tmp/bounty-agent-active.signal"
            ]
            for path in pauseFiles {
                try? FileManager.default.removeItem(atPath: path)
            }

            // If bounty type is selected, create the bounty signal to pause other factory work
            if selectedHelperType == .bounty {
                // Create bounty signal file so factory prioritizes bounty work
                try? "".write(toFile: "/tmp/bounty-agent-active.signal", atomically: true, encoding: .utf8)
            }

            let factory = DaemonConfig.factory
            let loaded = self.daemonManager.isLoaded(factory.label)

            if loaded {
                // If factory is already running (e.g. sleeping on billing pause),
                // we need to restart it so it picks up fresh state
                self.daemonManager.restart(factory)
            } else {
                self.daemonManager.load(factory)
            }
            usleep(1_000_000)
            self.daemonManager.kickstart(factory)

            // Poll factory log for result (up to 90s, checking every 3s)
            let startTime = Date()
            var foundResult = false
            while Date().timeIntervalSince(startTime) < 90.0 {
                usleep(3_000_000)  // 3 second intervals
                if let result = self.checkFactoryResult(since: startTime) {
                    DispatchQueue.main.async {
                        let modelLabel = selectedModel.label
                        self.launchResultMessage = result + " (\(modelLabel))"
                        self.isLaunchingHelper = false
                        self.poller.pollNow()
                    }
                    foundResult = true
                    break
                }
            }
            if !foundResult {
                DispatchQueue.main.async {
                    self.launchResultMessage = "Factory timed out — check logs"
                    self.isLaunchingHelper = false
                    self.poller.pollNow()
                }
            }

            // Clear the result message after 10 seconds
            DispatchQueue.main.asyncAfter(deadline: .now() + 10.0) { [weak self] in
                self?.launchResultMessage = nil
                self?.poller.pollNow()
            }
        }
    }

    /// Run the factory scan command to populate the issue database
    private func runFactoryScan() {
        let process = Process()
        let venvPython = "/Volumes/X10 Pro danielalanbatesatgmail.com /AIcode/12-AI_Coding_Tools/github-helper/.venv/bin/python3.14"
        let workDir = "/Volumes/X10 Pro danielalanbatesatgmail.com /AIcode/12-AI_Coding_Tools/github-helper"

        process.executableURL = URL(fileURLWithPath: venvPython)
        process.arguments = ["-m", "src.cli", "scan", "--min-stars", "1000"]
        process.currentDirectoryURL = URL(fileURLWithPath: workDir)

        // Build environment from factory plist so scan has all needed tokens
        var env: [String: String] = [
            "HOME": "/Users/daniel",
            "PATH": "/Users/daniel/.nvm/versions/node/v22.22.0/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "VIRTUAL_ENV": "\(workDir)/.venv",
            "PYTHONUNBUFFERED": "1"
        ]

        // Read tokens from factory plist so we always have them
        // (the app process itself may not have these env vars)
        let factoryPlist = NSString(string: "~/Library/LaunchAgents/com.batesai.dogood.factory.plist").expandingTildeInPath
        if let plistData = FileManager.default.contents(atPath: factoryPlist),
           let plist = try? PropertyListSerialization.propertyList(from: plistData, format: nil) as? [String: Any],
           let plistEnv = plist["EnvironmentVariables"] as? [String: String] {
            // Copy all tokens from the factory plist
            for (key, value) in plistEnv {
                if env[key] == nil {
                    env[key] = value
                }
            }
        }

        // Also grab GITHUB_TOKEN from gh CLI if not already set
        if env["GITHUB_TOKEN"] == nil {
            let ghProcess = Process()
            let ghPipe = Pipe()
            ghProcess.executableURL = URL(fileURLWithPath: "/opt/homebrew/bin/gh")
            ghProcess.arguments = ["auth", "token"]
            ghProcess.standardOutput = ghPipe
            ghProcess.standardError = Pipe()
            do {
                try ghProcess.run()
                ghProcess.waitUntilExit()
                let tokenData = ghPipe.fileHandleForReading.readDataToEndOfFile()
                let token = String(data: tokenData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                if !token.isEmpty {
                    env["GITHUB_TOKEN"] = token
                }
            } catch { }
        }

        // Override with any values from current process env
        for key in ["ANTHROPIC_API_KEY", "GITHUB_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"] {
            if let val = ProcessInfo.processInfo.environment[key] {
                env[key] = val
            }
        }

        process.environment = env

        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            // Scan failed silently — factory will still try with whatever DB has
        }
    }

    /// Check factory log for completion result (only considers entries after `since`)
    private func checkFactoryResult(since: Date? = nil) -> String? {
        let logPath = "/tmp/dogood-factory.log"
        guard let data = FileManager.default.contents(atPath: logPath),
              let content = String(data: data, encoding: .utf8) else { return nil }

        let lines = content.components(separatedBy: "\n")

        // Only look at lines written after the factory was restarted
        // Use file modification time as a proxy — if the log was last modified
        // before we started, there's no new output yet
        if let since = since {
            let attrs = try? FileManager.default.attributesOfItem(atPath: logPath)
            if let modDate = attrs?[.modificationDate] as? Date, modDate < since {
                return nil  // Log hasn't been updated since we kicked off
            }
        }

        // Look at last 30 lines for a result
        let recentLines = lines.suffix(30)

        for line in recentLines {
            // Check for billing/credit errors
            if line.contains("Credit balance") || line.contains("billing_error") ||
               line.contains("FACTORY PAUSED") {
                return "API credits exhausted — add credits to resume"
            }
            if line.contains("No eligible issues found") {
                return "No eligible issues found"
            }
            if line.contains("Factory complete!") {
                // Check for started count
                for inner in recentLines {
                    if inner.contains("Started:") && !inner.contains("Started:   0") {
                        return "Helper launched successfully"
                    }
                }
                return "Factory complete — no new helpers needed"
            }
            if line.contains("Agent started") || line.contains("Spawning agent") {
                return "Helper launched successfully"
            }
        }
        return nil
    }
}
