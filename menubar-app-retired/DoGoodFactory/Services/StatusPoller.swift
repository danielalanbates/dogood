import Foundation

struct SystemState {
    let daemons: [DaemonState]
    let workers: [WorkerInfo]
    let timestamp: Date
    let factoryPaused: Bool
    let factoryHealth: FactoryHealth?
}

class StatusPoller {
    private let interval: TimeInterval
    private let onUpdate: (SystemState) -> Void
    private var timer: Timer?
    private let launchdMonitor = LaunchdMonitor()
    private let workerMonitor = WorkerMonitor()
    private let healthChecker = FactoryHealthChecker()
    private var pollCount = 0
    private var cachedHealth: FactoryHealth?

    init(interval: TimeInterval = 5.0,
         onUpdate: @escaping (SystemState) -> Void) {
        self.interval = interval
        self.onUpdate = onUpdate
    }

    func start() {
        poll()
        timer = Timer.scheduledTimer(withTimeInterval: interval,
                                     repeats: true) { [weak self] _ in
            self?.poll()
        }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    /// Trigger an immediate refresh (e.g. after toggling a daemon)
    func pollNow() {
        poll()
    }

    private func poll() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let daemons = self.launchdMonitor.checkAll(configs: DaemonConfig.all)
            let workers = self.workerMonitor.discoverWorkers()
            let paused = FileManager.default.fileExists(atPath: "/tmp/bounty-agent-active.signal")

            // Run health check every 12th poll (~1 minute at 5s interval)
            self.pollCount += 1
            if self.pollCount % 12 == 1 {
                self.cachedHealth = self.healthChecker.check()
            }

            let state = SystemState(daemons: daemons, workers: workers,
                                    timestamp: Date(), factoryPaused: paused,
                                    factoryHealth: self.cachedHealth)
            DispatchQueue.main.async {
                self.onUpdate(state)
            }
        }
    }
}
