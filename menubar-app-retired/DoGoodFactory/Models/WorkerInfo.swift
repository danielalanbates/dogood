struct WorkerInfo {
    let pid: Int
    let issueId: Int
    let agentId: String
    let modelLabel: String
    let repoName: String?
    let workerType: String  // "fix", "feedback", "bounty"

    init(pid: Int, issueId: Int, agentId: String, modelLabel: String,
         repoName: String? = nil, workerType: String = "fix") {
        self.pid = pid
        self.issueId = issueId
        self.agentId = agentId
        self.modelLabel = modelLabel
        self.repoName = repoName
        self.workerType = workerType
    }

    var typeEmoji: String {
        switch workerType {
        case "feedback": return "\u{1F4AC}"  // speech bubble
        case "bounty":   return "\u{1F4B0}"  // money bag
        default:         return "\u{1F527}"  // wrench
        }
    }

    var typeTag: String {
        switch workerType {
        case "bounty":   return " (bounty)"
        case "feedback": return " (feedback)"
        default:         return ""
        }
    }

    var displayName: String {
        let shortId = String(agentId.prefix(8))
        let repo = repoName ?? ""
        if !repo.isEmpty {
            return "\(typeEmoji) \(repo) [\(modelLabel)\(typeTag)] (\(shortId))"
        }
        return "\(typeEmoji) #\(issueId) [\(modelLabel)\(typeTag)] (\(shortId))"
    }
}
