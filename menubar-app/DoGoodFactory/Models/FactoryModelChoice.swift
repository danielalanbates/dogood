import AppKit

/// Claude models the factory can run. The solver runs on the Claude Agent SDK,
/// so only Claude model ids work here.
/// Model ids match src/llm.py: `claude-*` (Claude Code), `gemini-*` (Gemini API, text only),
/// `agy:*` (Antigravity CLI, can edit code).
enum FactoryModelChoice {
    struct Option { let id: String; let label: String; let group: String }
    struct Role { let key: String; let title: String; let defaultModel: String; let needsTools: Bool }

    static let roles: [Role] = [
        Role(key: "scout", title: "Scout (finds issues)", defaultModel: "gemini-flash-latest", needsTools: false),
        Role(key: "primary", title: "Fixer (fixes issues)", defaultModel: "agy:gemini-3.8-flash-high", needsTools: true),
        Role(key: "reviewer", title: "95% Reviewer", defaultModel: "agy:gemini-3.1-pro-high", needsTools: false),
        Role(key: "replies", title: "Maintainer Replies", defaultModel: "gemini-flash-latest", needsTools: false),
        Role(key: "chat", title: "Telegram Chat", defaultModel: "gemini-flash-latest", needsTools: false),
    ]

    static let options: [Option] = [
        Option(id: "claude-fable-5-1", label: "Fable 5.1", group: "Claude"),
        Option(id: "claude-opus-5", label: "Opus 5", group: "Claude"),
        Option(id: "claude-sonnet-5", label: "Sonnet 5", group: "Claude"),
        Option(id: "claude-haiku-4-5-20251001", label: "Haiku 4.5", group: "Claude"),
        Option(id: "gemini-flash-latest", label: "Gemini Flash", group: "Gemini API"),
        Option(id: "gemini-pro-latest", label: "Gemini Pro", group: "Gemini API"),
        Option(id: "agy:gemini-3.8-flash-high", label: "Gemini 3.8 Flash (High)", group: "Antigravity"),
        Option(id: "agy:gemini-3.1-pro-high", label: "Gemini 3.1 Pro (High)", group: "Antigravity"),
        Option(id: "agy:gpt-oss-120b-medium", label: "GPT-OSS 120B", group: "Antigravity"),
    ]

    static func role(_ key: String) -> Role { roles.first { $0.key == key }! }

    static func options(for role: Role) -> [Option] {
        role.needsTools ? options.filter { !$0.id.hasPrefix("gemini-") } : options
    }

    /// Read by src/config.py before every issue.
    static let fileURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/BatesAI/shared/dogood_models.json")

    static func load() -> [String: String] {
        guard let data = try? Data(contentsOf: fileURL),
              let dict = try? JSONSerialization.jsonObject(with: data) as? [String: String] else { return [:] }
        return dict
    }

    static func current(_ roleKey: String) -> String {
        let r = role(roleKey)
        guard let saved = load()[roleKey], options(for: r).contains(where: { $0.id == saved }) else {
            return r.defaultModel
        }
        return saved
    }

    static func set(_ role: String, _ model: String) {
        var dict = load()
        dict[role] = model
        try? FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        if let data = try? JSONSerialization.data(withJSONObject: dict, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: fileURL, options: .atomic)
        }
    }

    static func label(for id: String) -> String {
        options.first { $0.id == id }?.label ?? id
    }
}

final class FactoryModelMenuTarget: NSObject {
    static let shared = FactoryModelMenuTarget()

    @objc func choose(_ sender: NSMenuItem) {
        guard let pair = sender.representedObject as? [String], pair.count == 2 else { return }
        FactoryModelChoice.set(pair[0], pair[1])
    }

    func menuItem(title: String, role: String) -> NSMenuItem {
        let current = FactoryModelChoice.current(role)
        let item = NSMenuItem(title: "\(title): \(FactoryModelChoice.label(for: current))",
                              action: nil, keyEquivalent: "")
        let sub = NSMenu()
        var lastGroup = ""
        for option in FactoryModelChoice.options(for: FactoryModelChoice.role(role)) {
            if option.group != lastGroup {
                if !lastGroup.isEmpty { sub.addItem(NSMenuItem.separator()) }
                let header = NSMenuItem(title: option.group, action: nil, keyEquivalent: "")
                header.isEnabled = false
                sub.addItem(header)
                lastGroup = option.group
            }
            let choice = NSMenuItem(title: option.label, action: #selector(choose(_:)), keyEquivalent: "")
            choice.target = self
            choice.representedObject = [role, option.id]
            choice.state = option.id == current ? .on : .off
            sub.addItem(choice)
        }
        item.submenu = sub
        return item
    }
}
