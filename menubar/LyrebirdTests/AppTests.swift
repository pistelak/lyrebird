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
