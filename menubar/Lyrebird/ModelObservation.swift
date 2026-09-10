import Observation

/// Invalidating queued callbacks prevents rendering after close; see windowLifecycleUsesNativeOwnership.
@MainActor
final class ModelObservation {
    private var generation = 0
    private var render: (() -> Void)?

    func start(_ render: @escaping () -> Void) {
        stop()
        self.render = render
        track(generation)
    }

    func stop() {
        generation &+= 1
        render = nil
    }

    private func track(_ expected: Int) {
        guard generation == expected, let render else { return }
        withObservationTracking {
            render()
        } onChange: { [weak self] in
            Task { @MainActor [weak self] in self?.track(expected) }
        }
    }
}
