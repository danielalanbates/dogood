import AppKit

// MARK: - Model Selector View (simple clickable row that cycles through models)

class ModelSelectorView: NSView {
    private weak var delegate: AppDelegate?
    private let labelView: NSTextField
    private let valueView: NSTextField

    init(delegate: AppDelegate?) {
        self.delegate = delegate
        self.labelView = NSTextField(labelWithString: "AI Model:")
        self.valueView = NSTextField(labelWithString: "")

        super.init(frame: NSRect(x: 0, y: 0, width: 260, height: 24))

        labelView.font = NSFont.systemFont(ofSize: 11, weight: .regular)
        labelView.textColor = NSColor.secondaryLabelColor
        labelView.frame = NSRect(x: 20, y: 2, width: 70, height: 20)
        labelView.alignment = .left
        addSubview(labelView)

        valueView.font = NSFont.systemFont(ofSize: 11, weight: .medium)
        valueView.textColor = NSColor.systemBlue
        valueView.frame = NSRect(x: 90, y: 2, width: 150, height: 20)
        valueView.alignment = .left
        addSubview(valueView)

        updateDisplay()

        // Make the whole view clickable
        let clickGesture = NSClickGestureRecognizer(target: self, action: #selector(didClick))
        addGestureRecognizer(clickGesture)

        wantsLayer = true
        layer?.cornerRadius = 4
        trackHover()
    }

    required init?(coder: NSCoder) { fatalError() }

    private var isHovering = false
    private func trackHover() {
        let trackingArea = NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeAlways],
            owner: self,
            userInfo: nil)
        addTrackingArea(trackingArea)
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

    private func updateDisplay() {
        valueView.stringValue = UserDefaults.standard.selectedAIModel.label
    }

    @objc private func didClick() {
        let currentModel = UserDefaults.standard.selectedAIModel
        let currentIndex = AIModel.all.firstIndex(of: currentModel) ?? 0
        let nextIndex = (currentIndex + 1) % AIModel.all.count
        let nextModel = AIModel.all[nextIndex]
        UserDefaults.standard.selectedAIModel = nextModel
        updateDisplay()
        delegate?.refreshMenu()
    }
}

// MARK: - Helper Type Selector View (simple clickable row that cycles through types)

class HelperTypeSelectorView: NSView {
    private weak var delegate: AppDelegate?
    private let labelView: NSTextField
    private let valueView: NSTextField

    init(delegate: AppDelegate?) {
        self.delegate = delegate
        self.labelView = NSTextField(labelWithString: "Helper Type:")
        self.valueView = NSTextField(labelWithString: "")

        super.init(frame: NSRect(x: 0, y: 0, width: 260, height: 24))

        labelView.font = NSFont.systemFont(ofSize: 11, weight: .regular)
        labelView.textColor = NSColor.secondaryLabelColor
        labelView.frame = NSRect(x: 20, y: 2, width: 70, height: 20)
        labelView.alignment = .left
        addSubview(labelView)

        valueView.font = NSFont.systemFont(ofSize: 11, weight: .medium)
        valueView.textColor = NSColor.systemBlue
        valueView.frame = NSRect(x: 90, y: 2, width: 150, height: 20)
        valueView.alignment = .left
        addSubview(valueView)

        updateDisplay()

        let clickGesture = NSClickGestureRecognizer(target: self, action: #selector(didClick))
        addGestureRecognizer(clickGesture)

        wantsLayer = true
        layer?.cornerRadius = 4
        trackHover()
    }

    required init?(coder: NSCoder) { fatalError() }

    private var isHovering = false
    private func trackHover() {
        let trackingArea = NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeAlways],
            owner: self,
            userInfo: nil)
        addTrackingArea(trackingArea)
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

    private func updateDisplay() {
        let currentType = UserDefaults.standard.selectedHelperType
        valueView.stringValue = "\(currentType.emoji) \(currentType.displayName)"
    }

    @objc private func didClick() {
        let currentType = UserDefaults.standard.selectedHelperType
        let currentIndex = HelperType.allCases.firstIndex(of: currentType) ?? 0
        let nextIndex = (currentIndex + 1) % HelperType.allCases.count
        let nextType = HelperType.allCases[nextIndex]
        UserDefaults.standard.selectedHelperType = nextType
        updateDisplay()
        delegate?.refreshMenu()
    }
}
