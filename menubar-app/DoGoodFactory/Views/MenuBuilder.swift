import AppKit

// MARK: - Custom toggle view (prevents menu from closing on click)

class ToggleRowView: NSView {
    var daemonLabel: String = ""
    private weak var delegate: AppDelegate?
    private let button = NSButton()
    private var isHovering = false

    init(daemonLabel: String, isOn: Bool, delegate: AppDelegate?) {
        self.daemonLabel = daemonLabel
        self.delegate = delegate
        super.init(frame: NSRect(x: 0, y: 0, width: 260, height: 24))

        let title = isOn ? "Turn Off" : "Turn On"
        let color = isOn ? NSColor.systemRed : NSColor.systemGreen

        button.title = title
        button.bezelStyle = .inline
        button.isBordered = false
        button.font = NSFont.systemFont(ofSize: 11)
        button.contentTintColor = color
        button.target = self
        button.action = #selector(didClick)
        button.frame = NSRect(x: 28, y: 2, width: 60, height: 20)
        addSubview(button)

        // Enable hover tracking
        wantsLayer = true
        layer?.cornerRadius = 4
        trackHover()
    }

    required init?(coder: NSCoder) { fatalError() }

    private func trackHover() {
        let trackingArea = NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeAlways],
            owner: self,
            userInfo: nil)
        addTrackingArea(trackingArea)
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        // Rebuild tracking areas if bounds change
    }

    override func mouseEntered(with event: NSEvent) {
        isHovering = true
        needsDisplay = true
    }

    override func mouseExited(with event: NSEvent) {
        isHovering = false
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        if isHovering {
            NSColor.selectedControlColor.withAlphaComponent(0.15).setFill()
            bounds.fill()
        }
        super.draw(dirtyRect)
    }

    @objc private func didClick() {
        delegate?.toggleDaemon(self)
    }
}

// MARK: - Helper toggle view (prevents menu from closing on click)

class HelperToggleView: NSView {
    let workerPid: Int
    let agentId: String
    private weak var delegate: AppDelegate?
    private let button = NSButton()
    private var isHovering = false

    init(workerPid: Int, agentId: String, delegate: AppDelegate?) {
        self.workerPid = workerPid
        self.agentId = agentId
        self.delegate = delegate
        super.init(frame: NSRect(x: 0, y: 0, width: 260, height: 24))

        button.title = "Turn Off"
        button.bezelStyle = .inline
        button.isBordered = false
        button.font = NSFont.systemFont(ofSize: 11)
        button.contentTintColor = NSColor.systemRed
        button.target = self
        button.action = #selector(didClick)
        button.frame = NSRect(x: 28, y: 2, width: 60, height: 20)
        addSubview(button)

        // Enable hover tracking
        wantsLayer = true
        layer?.cornerRadius = 4
        trackHover()
    }

    required init?(coder: NSCoder) { fatalError() }

    private func trackHover() {
        let trackingArea = NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeAlways],
            owner: self,
            userInfo: nil)
        addTrackingArea(trackingArea)
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
    }

    override func mouseEntered(with event: NSEvent) {
        isHovering = true
        needsDisplay = true
    }

    override func mouseExited(with event: NSEvent) {
        isHovering = false
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        if isHovering {
            NSColor.selectedControlColor.withAlphaComponent(0.15).setFill()
            bounds.fill()
        }
        super.draw(dirtyRect)
    }

    @objc private func didClick() {
        delegate?.killHelper(self)
    }
}

// MARK: - Launch helper button (prevents menu from closing on click)

class LaunchHelperView: NSView {
    private weak var delegate: AppDelegate?
    private let button = NSButton()
    private let spinner = NSProgressIndicator()
    private let statusLabel = NSTextField(labelWithString: "")
    private let isProcessing: Bool
    private let helperType: HelperType
    private var isHovering = false

    init(delegate: AppDelegate?, isProcessing: Bool = false, helperType: HelperType = .general) {
        self.delegate = delegate
        self.isProcessing = isProcessing
        self.helperType = helperType
        super.init(frame: NSRect(x: 0, y: 0, width: 260, height: 28))

        if isProcessing {
            // Show spinner + "Launching..." text
            spinner.style = .spinning
            spinner.controlSize = .small
            spinner.frame = NSRect(x: 20, y: 5, width: 16, height: 16)
            spinner.startAnimation(nil)
            addSubview(spinner)

            statusLabel.stringValue = "Scanning issues & launching..."
            statusLabel.font = NSFont.systemFont(ofSize: 12, weight: .medium)
            statusLabel.textColor = NSColor.secondaryLabelColor
            statusLabel.frame = NSRect(x: 42, y: 4, width: 180, height: 22)
            addSubview(statusLabel)
        } else {
            button.title = "＋ Launch \(helperType.emoji) \(helperType.displayName)"
            button.bezelStyle = .inline
            button.isBordered = false
            button.font = NSFont.systemFont(ofSize: 12, weight: .medium)
            button.contentTintColor = NSColor.systemGreen
            button.target = self
            button.action = #selector(didClick)
            button.frame = NSRect(x: 20, y: 4, width: 220, height: 22)
            addSubview(button)

            // Enable hover tracking
            wantsLayer = true
            layer?.cornerRadius = 4
            trackHover()
        }
    }

    required init?(coder: NSCoder) { fatalError() }

    private func trackHover() {
        let trackingArea = NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeAlways],
            owner: self,
            userInfo: nil)
        addTrackingArea(trackingArea)
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
    }

    override func mouseEntered(with event: NSEvent) {
        isHovering = true
        needsDisplay = true
    }

    override func mouseExited(with event: NSEvent) {
        isHovering = false
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        if isHovering {
            NSColor.selectedControlColor.withAlphaComponent(0.15).setFill()
            bounds.fill()
        }
        super.draw(dirtyRect)
    }

    @objc private func didClick() {
        // Set the helper type in UserDefaults before launching
        UserDefaults.standard.selectedHelperType = helperType
        delegate?.launchHelper()
    }
}


class MenuBuilder {

    /// Update an existing menu in-place so it refreshes live while open
    static func rebuild(_ menu: NSMenu, from state: SystemState, delegate: AppDelegate? = nil) {
        menu.removeAllItems()
        populateMenu(menu, from: state, delegate: delegate)
    }

    static func build(from state: SystemState, delegate: AppDelegate? = nil) -> NSMenu {
        let menu = NSMenu()
        populateMenu(menu, from: state, delegate: delegate)
        return menu
    }

    // MARK: - Shared menu population logic

    private static func populateMenu(_ menu: NSMenu, from state: SystemState, delegate: AppDelegate? = nil) {
        // Header
        let header = NSMenuItem(title: "Do Good Factory", action: nil, keyEquivalent: "")
        header.isEnabled = false
        header.attributedTitle = NSAttributedString(
            string: "Do Good Factory",
            attributes: [.font: NSFont.boldSystemFont(ofSize: 13)])
        menu.addItem(header)

        // Summary line — count running AND betweenCycles as "up"
        let totalCount = state.daemons.count
        let runningCount = state.daemons.filter({ $0.health == .running || $0.health == .betweenCycles }).count
        let crashedCount = state.daemons.filter({ $0.health == .crashed }).count
        var summaryParts: [String] = ["\(runningCount)/\(totalCount) running"]
        if crashedCount > 0 { summaryParts.append("\(crashedCount) crashed") }
        let summaryItem = NSMenuItem(title: "    \(summaryParts.joined(separator: " \u{2022} "))",
                                     action: nil, keyEquivalent: "")
        summaryItem.isEnabled = false
        menu.addItem(summaryItem)

        // Factory health
        if state.factoryPaused {
            let paused = NSMenuItem(title: "\u{1F7E1}  Factory paused",
                                    action: nil, keyEquivalent: "")
            paused.isEnabled = false
            menu.addItem(paused)
        }

        if let health = state.factoryHealth {
            let healthItem = NSMenuItem(
                title: "\(health.emoji)  \(health.summaryLine)",
                action: nil, keyEquivalent: "")
            healthItem.isEnabled = false
            menu.addItem(healthItem)
        }

        // ── Daemons ──
        menu.addItem(NSMenuItem.separator())

        let daemonsHeader = NSMenuItem(title: "Daemons", action: nil, keyEquivalent: "")
        daemonsHeader.isEnabled = false
        daemonsHeader.attributedTitle = NSAttributedString(
            string: "Daemons",
            attributes: [
                .font: NSFont.boldSystemFont(ofSize: 11),
                .foregroundColor: NSColor.secondaryLabelColor
            ])
        menu.addItem(daemonsHeader)

        for daemon in state.daemons {
            let circle: String
            switch daemon.health {
            case .running:       circle = "\u{1F7E2}"
            case .betweenCycles: circle = "\u{1F7E2}"
            case .stopped:       circle = "\u{26AA}"
            case .crashed:       circle = "\u{1F534}"
            case .notLoaded:     circle = "\u{2B1B}"
            }

            var suffix = ""
            switch daemon.health {
            case .running: break
            case .betweenCycles: suffix = "  (cycling)"
            case .stopped: suffix = "  (stopped)"
            case .crashed: suffix = "  (crashed: \(daemon.lastExitStatus ?? -1))"
            case .notLoaded: suffix = "  (not loaded)"
            }

            // Daemon row with status
            let daemonItem = NSMenuItem(
                title: "\(circle)  \(daemon.config.displayName)\(suffix)",
                action: nil, keyEquivalent: "")
            daemonItem.isEnabled = false
            menu.addItem(daemonItem)

            // On/Off toggle — uses custom NSView so menu stays open
            if let delegate = delegate {
                let isOn = daemon.health == .running || daemon.health == .betweenCycles
                let toggleView = ToggleRowView(
                    daemonLabel: daemon.config.label,
                    isOn: isOn,
                    delegate: delegate)
                let toggleItem = NSMenuItem()
                toggleItem.view = toggleView
                menu.addItem(toggleItem)
            }
        }

        // ── Active Helpers (workers) ──
        let allWorkers = state.workers.sorted(by: { $0.issueId < $1.issueId })

        menu.addItem(NSMenuItem.separator())

        let workersHeader = NSMenuItem(
            title: "Active Helpers (\(allWorkers.count))",
            action: nil, keyEquivalent: "")
        workersHeader.isEnabled = false
        workersHeader.attributedTitle = NSAttributedString(
            string: "Active Helpers (\(allWorkers.count))",
            attributes: [
                .font: NSFont.boldSystemFont(ofSize: 11),
                .foregroundColor: NSColor.secondaryLabelColor
            ])
        menu.addItem(workersHeader)

        if allWorkers.isEmpty {
            let none = NSMenuItem(title: "    No active helpers",
                                  action: nil, keyEquivalent: "")
            none.isEnabled = false
            menu.addItem(none)
        } else {
            for worker in allWorkers {
                // Helper row with status
                let item = NSMenuItem(
                    title: "\u{1F7E2}  \(worker.displayName)",
                    action: nil, keyEquivalent: "")
                item.isEnabled = false
                menu.addItem(item)

                // On/Off toggle for each helper — uses custom NSView so menu stays open
                if let delegate = delegate, worker.pid > 0 {
                    let toggleView = HelperToggleView(
                        workerPid: worker.pid,
                        agentId: worker.agentId,
                        delegate: delegate)
                    let toggleItem = NSMenuItem()
                    toggleItem.view = toggleView
                    menu.addItem(toggleItem)
                }
            }
        }

        // Launch helper buttons directly in Active Helpers section
        if let delegate = delegate {
            menu.addItem(NSMenuItem.separator())

            let launchHeader = NSMenuItem(
                title: "Launch Helpers",
                action: nil, keyEquivalent: "")
            launchHeader.isEnabled = false
            launchHeader.attributedTitle = NSAttributedString(
                string: "Launch Helpers",
                attributes: [
                    .font: NSFont.boldSystemFont(ofSize: 11),
                    .foregroundColor: NSColor.secondaryLabelColor
                ])
            menu.addItem(launchHeader)

            let generalLaunchView = LaunchHelperView(delegate: delegate, isProcessing: delegate.isLaunchingHelper, helperType: .general)
            let generalLaunchItem = NSMenuItem()
            generalLaunchItem.view = generalLaunchView
            menu.addItem(generalLaunchItem)

            let bountyLaunchView = LaunchHelperView(delegate: delegate, isProcessing: delegate.isLaunchingHelper, helperType: .bounty)
            let bountyLaunchItem = NSMenuItem()
            bountyLaunchItem.view = bountyLaunchView
            menu.addItem(bountyLaunchItem)
        }

        // Launch result message (shown after factory completes)
        if let delegate = delegate, let resultMsg = delegate.launchResultMessage {
            let resultItem = NSMenuItem(title: "    \(resultMsg)", action: nil, keyEquivalent: "")
            resultItem.isEnabled = false
            let color: NSColor = resultMsg.contains("launched") ? .systemGreen : .systemOrange
            resultItem.attributedTitle = NSAttributedString(
                string: "    \(resultMsg)",
                attributes: [.font: NSFont.systemFont(ofSize: 11), .foregroundColor: color])
            menu.addItem(resultItem)
        }

        // ── AI Models ──
        menu.addItem(NSMenuItem.separator())
        let modelsHeader = NSMenuItem(title: "AI Models", action: nil, keyEquivalent: "")
        modelsHeader.isEnabled = false
        menu.addItem(modelsHeader)
        menu.addItem(FactoryModelMenuTarget.shared.menuItem(title: "Solver", role: "primary"))
        menu.addItem(FactoryModelMenuTarget.shared.menuItem(title: "95% Reviewer", role: "reviewer"))

        // ── Options ──
        menu.addItem(NSMenuItem.separator())

        let optionsHeader = NSMenuItem(
            title: "Options",
            action: nil, keyEquivalent: "")
        optionsHeader.isEnabled = false
        optionsHeader.attributedTitle = NSAttributedString(
            string: "Options",
            attributes: [
                .font: NSFont.boldSystemFont(ofSize: 11),
                .foregroundColor: NSColor.secondaryLabelColor
            ])
        menu.addItem(optionsHeader)

        if let delegate = delegate {
            let modelView = ModelSelectorView(delegate: delegate)
            let modelItem = NSMenuItem()
            modelItem.view = modelView
            menu.addItem(modelItem)

            let helperTypeView = HelperTypeSelectorView(delegate: delegate)
            let helperTypeItem = NSMenuItem()
            helperTypeItem.view = helperTypeView
            menu.addItem(helperTypeItem)
        }

        // Timestamp
        let formatter = DateFormatter()
        formatter.timeStyle = .medium
        let ts = formatter.string(from: state.timestamp)
        let timeItem = NSMenuItem(title: "Updated: \(ts)", action: nil, keyEquivalent: "")
        timeItem.isEnabled = false
        menu.addItem(timeItem)

        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Quit Do Good Factory",
                                action: #selector(NSApplication.terminate(_:)),
                                keyEquivalent: "q"))
    }
}
