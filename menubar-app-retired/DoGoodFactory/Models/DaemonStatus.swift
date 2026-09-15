enum DaemonHealth {
    case running
    case betweenCycles
    case stopped
    case crashed
    case notLoaded

    var isHealthy: Bool {
        switch self {
        case .running, .betweenCycles: return true
        case .stopped, .crashed, .notLoaded: return false
        }
    }

    var statusText: String {
        switch self {
        case .running: return "Running"
        case .betweenCycles: return "Between Cycles"
        case .stopped: return "Stopped"
        case .crashed: return "Crashed"
        case .notLoaded: return "Not Loaded"
        }
    }
}

struct DaemonState {
    let config: DaemonConfig
    let health: DaemonHealth
    let pid: Int?
    let lastExitStatus: Int?

    var isHealthy: Bool { health.isHealthy }
}
