// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "DoGoodFactory",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "DoGoodFactory",
            path: "DoGoodFactory"
        ),
    ]
)
