import AppKit

/// Animated status bar icon: black rectangle with a bouncing green column
/// when healthy, or a flashing red fill when unhealthy.
class StatusBarIcon: NSView {

    enum Mode {
        case healthy   // Green column bounces left to right
        case unhealthy // Red fill flashes on/off
        case paused    // Static amber: process alive, waiting on a limit
        case reviewing // Static yellow: Reviewer is judging a fix
        case readyToPost // Static orange: a fix is waiting for Daniel's yes
        case loading   // Static gray
    }

    var mode: Mode = .loading {
        didSet {
            if mode != oldValue { resetAnimation() }
            render()
        }
    }

    /// Status item buttons on macOS 26 don't display custom subviews, so each frame
    /// is rendered into the button's image instead.
    weak var button: NSStatusBarButton? {
        didSet { render() }
    }

    private func render() {
        guard let button = button else { return }
        let image = NSImage(size: NSSize(width: iconWidth, height: iconHeight + 2), flipped: false) { [weak self] _ in
            self?.draw(.zero)
            return true
        }
        image.isTemplate = false
        button.image = image
    }

    private let iconWidth: CGFloat = 18
    private let iconHeight: CGFloat = 12
    private var displayLink: CVDisplayLink?
    private var phase: CGFloat = 0        // 0...1 animation progress
    private var direction: CGFloat = 1    // +1 right, -1 left (bounce)
    private let speed: CGFloat = 0.024    // per frame (~0.5s per sweep at 5fps)

    override var intrinsicContentSize: NSSize {
        NSSize(width: iconWidth, height: iconHeight)
    }

    override init(frame: NSRect) {
        super.init(frame: NSRect(x: 0, y: 0, width: 18, height: 12))
        wantsLayer = true
        startTimer()
    }

    required init?(coder: NSCoder) { fatalError() }

    deinit { stopTimer() }

    // MARK: - Drawing

    override func draw(_ dirtyRect: NSRect) {
        let rect = NSRect(x: 0, y: 1, width: iconWidth, height: iconHeight)
        let radius: CGFloat = 2

        // Black background
        let bg = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
        NSColor.black.setFill()
        bg.fill()

        // Slight border
        NSColor.gray.withAlphaComponent(0.3).setStroke()
        bg.lineWidth = 0.5
        bg.stroke()

        switch mode {
        case .healthy:
            drawBouncingColumn(in: rect, radius: radius)
        case .unhealthy:
            drawFlashingRed(in: rect, radius: radius)
        case .paused:
            let fill = NSBezierPath(roundedRect: rect.insetBy(dx: 0.5, dy: 0.5), xRadius: radius, yRadius: radius)
            NSColor.systemOrange.setFill()
            fill.fill()
        case .reviewing, .readyToPost:
            let fill = NSBezierPath(roundedRect: rect.insetBy(dx: 0.5, dy: 0.5), xRadius: radius, yRadius: radius)
            (mode == .reviewing ? NSColor.systemYellow : NSColor.systemOrange).setFill()
            fill.fill()
        case .loading:
            drawStaticGray(in: rect, radius: radius)
        }
    }

    private func drawBouncingColumn(in rect: NSRect, radius: CGFloat) {
        let columnWidth: CGFloat = 4
        let maxX = rect.width - columnWidth
        let x = rect.origin.x + maxX * phase

        // Clip to rounded rect
        NSGraphicsContext.saveGraphicsState()
        let clip = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
        clip.addClip()

        // Running: whole icon light green, with a brighter column sweeping across.
        NSColor(calibratedRed: 0.56, green: 0.93, blue: 0.56, alpha: 1).setFill()
        NSBezierPath(rect: rect).fill()
        let columnRect = NSRect(x: x, y: rect.origin.y, width: columnWidth, height: rect.height)
        NSColor.white.withAlphaComponent(0.7).setFill()
        NSBezierPath(rect: columnRect).fill()

        NSGraphicsContext.restoreGraphicsState()
    }

    private func drawFlashingRed(in rect: NSRect, radius: CGFloat) {
        // phase oscillates 0...1, use sine for smooth flash
        let alpha = CGFloat(0.3 + 0.7 * sin(Double(phase) * .pi))
        let fill = NSBezierPath(roundedRect: rect.insetBy(dx: 0.5, dy: 0.5),
                                xRadius: radius, yRadius: radius)
        NSColor.systemRed.withAlphaComponent(alpha).setFill()
        fill.fill()
    }

    private func drawStaticGray(in rect: NSRect, radius: CGFloat) {
        let fill = NSBezierPath(roundedRect: rect.insetBy(dx: 0.5, dy: 0.5),
                                xRadius: radius, yRadius: radius)
        NSColor.systemGray.withAlphaComponent(0.4).setFill()
        fill.fill()
    }

    // MARK: - Animation

    private func resetAnimation() {
        phase = 0
        direction = 1
    }

    private var timer: Timer?

    private func startTimer() {
        // 5fps — minimal CPU for a status bar icon
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            self?.tick()
        }
    }

    private func stopTimer() {
        timer?.invalidate()
        timer = nil
    }

    private func tick() {
        switch mode {
        case .healthy:
            phase += speed * direction
            if phase >= 1 { phase = 1; direction = -1 }
            if phase <= 0 { phase = 0; direction = 1 }
        case .unhealthy:
            // Slow flash cycle (~2s full cycle at 5fps)
            phase += 0.05
            if phase >= 1 { phase = 0 }
        case .loading, .paused, .reviewing, .readyToPost:
            return  // No animation
        }
        render()
    }
}
