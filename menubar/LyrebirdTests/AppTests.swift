import AppKit
import Testing

@testable import Lyrebird

/// These suites share preferences, URL protocol handlers, and application activation policy.
@Suite(.serialized)
struct AppTests {}

@MainActor
func withAppTestEnvironment(_ body: @MainActor () async throws -> Void) async throws {
    try TestDefaults.install()
    let originalApply = DockPresence.apply
    DockPresence.apply = { _ in }
    DockPresence.reset()
    StubURLProtocol.reset()
    defer {
        StubURLProtocol.reset()
        DockPresence.apply = originalApply
        DockPresence.reset()
        TestDefaults.restore()
    }
    try await body()
}

/// Models start without polling or shell discovery; tests supply the fingerprint they mean.
@MainActor
func makeModel(
    expecting fingerprint: String?, discover: (@Sendable () async throws -> String)? = nil
) -> AppModel {
    AppModel(client: Stub.makeClient(), autoStart: false, expectedFingerprint: fingerprint, discover: discover)
}

@MainActor
func fields(_ view: NSView) -> [NSTextField] {
    (view as? NSTextField).map { [$0] } ?? view.subviews.flatMap(fields)
}

@MainActor
func bitmap(_ view: NSView, draw: () -> Void) throws -> Data {
    let bitmap = try #require(
        NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: Int(view.bounds.width), pixelsHigh: Int(view.bounds.height),
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
            bytesPerRow: 0, bitsPerPixel: 0))
    let context = try #require(NSGraphicsContext(bitmapImageRep: bitmap))
    NSGraphicsContext.saveGraphicsState()
    defer { NSGraphicsContext.restoreGraphicsState() }
    NSGraphicsContext.current = context
    view.effectiveAppearance.performAsCurrentDrawingAppearance {
        BrowserAppearance.pane.setFill()
        view.bounds.fill()
        draw()
    }
    return try #require(bitmap.representation(using: .png, properties: [:]))
}

/// Keep exact PNG equality as the oracle; decode only to explain a failure in pixel coordinates.
@MainActor
func bitmapDifference(_ actual: Data, _ expected: Data, appearance: NSAppearance.Name) -> String {
    guard actual != expected else { return "" }
    let context = "appearance=\(appearance.rawValue)"
    guard let lhs = NSBitmapImageRep(data: actual), let rhs = NSBitmapImageRep(data: expected) else {
        return "\(context); could not decode PNGs (actual \(actual.count) bytes, expected \(expected.count) bytes)"
    }
    let dimensions =
        "actual=\(lhs.pixelsWide)×\(lhs.pixelsHigh), expected=\(rhs.pixelsWide)×\(rhs.pixelsHigh); \(context)"
    var bounds = NSRect.null
    var count = 0
    for y in 0..<max(lhs.pixelsHigh, rhs.pixelsHigh) {
        for x in 0..<max(lhs.pixelsWide, rhs.pixelsWide) {
            // A missing pixel differs from any pixel, including a transparent one.
            let left = x < lhs.pixelsWide && y < lhs.pixelsHigh ? lhs.colorAt(x: x, y: y) : nil
            let right = x < rhs.pixelsWide && y < rhs.pixelsHigh ? rhs.colorAt(x: x, y: y) : nil
            if left != right {
                bounds = bounds.union(NSRect(x: CGFloat(x), y: CGFloat(y), width: 1, height: 1))
                count += 1
            }
        }
    }
    return count == 0
        ? "\(dimensions); PNG bytes differ but decoded colors match"
        : "\(dimensions); \(count) differing pixels; bounds=\(bounds) (pixel origin and size)"
}
