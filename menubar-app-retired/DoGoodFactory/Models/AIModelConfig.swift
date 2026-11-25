import Foundation

// MARK: - Available AI Models (from Bates AI Wrapper)

struct AIModel: Identifiable, Equatable, Codable {
    let id: String          // Unique identifier (provider key)
    let label: String       // Display name in menu
    let cliCommand: String  // CLI command prefix (for reference)
    let modelFlag: String   // Flag to pass to factory (--model_tier value)

    enum CodingKeys: String, CodingKey {
        case id
        case label = "name"
        case cliCommand
        case modelFlag
    }

    init(id: String, label: String, cliCommand: String, modelFlag: String) {
        self.id = id
        self.label = label
        self.cliCommand = cliCommand
        self.modelFlag = modelFlag
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.id = try container.decode(String.self, forKey: .id)
        self.label = try container.decode(String.self, forKey: .label)
        self.cliCommand = (try? container.decode(String.self, forKey: .cliCommand)) ?? "openrouter API"
        self.modelFlag = (try? container.decode(String.self, forKey: .modelFlag)) ?? self.id
    }

    static var all: [AIModel] {
        let path = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/BatesAI/shared/architect_preferences.json")
        
        if let data = try? Data(contentsOf: path),
           let models = try? JSONDecoder().decode([AIModel].self, from: data) {
            return models
        }
        
        // Fallback to hardcoded list if JSON loading fails
        return [
            AIModel(id: "claude", label: "Claude (Opus 4.5)", cliCommand: "claude --model claude-opus-4-5", modelFlag: "claude-opus-4-5"),
            AIModel(id: "qwen36max", label: "Qwen 3.6 Max (Reasoning)", cliCommand: "qwen -m qwen3-max-thinking --yolo", modelFlag: "qwen3-max-thinking"),
            AIModel(id: "qwen", label: "Qwen (QwQ Plus)", cliCommand: "qwen -m qwq-plus --yolo", modelFlag: "qwq-plus"),
            AIModel(id: "gemini", label: "Gemini (3 Pro)", cliCommand: "gemini --model gemini-3-pro-preview", modelFlag: "gemini-3-pro-preview"),
            AIModel(id: "openai", label: "OpenAI (o3 / Codex)", cliCommand: "codex -m o3", modelFlag: "o3"),
            AIModel(id: "openrouter", label: "OpenRouter (Qwen 3.6+ Free)", cliCommand: "openrouter API", modelFlag: "openrouter"),
            AIModel(id: "groq", label: "Groq (Llama 3.3 70B)", cliCommand: "groq API", modelFlag: "groq"),
            AIModel(id: "cerebras", label: "Cerebras (Llama 3.1 70B)", cliCommand: "cerebras API", modelFlag: "cerebras"),
        ]
    }

    static let `default` = AIModel.all.first!  // Claude (Opus 4.5) or first from JSON
}

// MARK: - Helper Launch Types

enum HelperType: String, Identifiable, CaseIterable {
    case general = "general"
    case bounty = "bounty"

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .general: return "General Active Helper"
        case .bounty: return "Bounty Watch Active Helper"
        }
    }

    var emoji: String {
        switch self {
        case .general: return "🔧"
        case .bounty: return "💰"
        }
    }
}

// MARK: - UserDefaults Persistence

extension UserDefaults {
    private enum Keys {
        static let selectedModel = "dogood_selected_ai_model"
        static let selectedHelperType = "dogood_selected_helper_type"
    }

    var selectedAIModel: AIModel {
        get {
            if let saved = string(forKey: Keys.selectedModel),
               let model = AIModel.all.first(where: { $0.id == saved }) {
                return model
            }
            return .default
        }
        set {
            set(newValue.id, forKey: Keys.selectedModel)
        }
    }

    var selectedHelperType: HelperType {
        get {
            if let saved = string(forKey: Keys.selectedHelperType),
               let type = HelperType(rawValue: saved) {
                return type
            }
            return .general
        }
        set {
            set(newValue.rawValue, forKey: Keys.selectedHelperType)
        }
    }
}
