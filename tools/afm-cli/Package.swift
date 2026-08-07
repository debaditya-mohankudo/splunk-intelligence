// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "afm-cli",
    targets: [
        .executableTarget(name: "afm-cli", path: "Sources/afm-cli")
    ]
)
