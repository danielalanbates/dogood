import AppKit

/// Claude models the factory can run. The solver runs on the Claude Agent SDK,
/// so only Claude model ids work here.
enum FactoryModelChoice {
    static let options: [(id: String, label: String)] = [
        ("claude-fable-5-1", "Fable 5.1"),
        ("claude-opus-5", "Opus 5"),
        ("claude-sonnet-5", "Sonnet 5"),
        ("claude-haiku-4-5-20251001", "Haiku 4.5"),
    ]
    static let defaultModel = "claude-fable-5-1"

    /// Read by src/config.py before every issue.
    static let fileURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/BatesAI/shared/dogood_models.json")

    static func load() -> [String: String] {
        guard let data = try? Data(contentsOf: fileURL),
              let dict = try? JSONSerialization.jsonObject(with: data) as? [String: String] else { return [:] }
        return dict
    }

    static func current(_ role: String) -> String { load()[role] ?? defaultModel }

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
        for option in FactoryModelChoice.options {
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
