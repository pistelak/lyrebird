// A synthetic iOS app for Lyrebird's acceptance check. It is not a demo and not a template: it
// exists so that something real, running inside a simulator, exercises the whole path — PAC
// routing on the host, TLS terminated by a CA trusted in the simulator, and an override answering
// locally — and then says what it got.
//
// On every launch it makes exactly one HTTPS GET to a host the acceptance profile intercepts. That
// host never has to resolve or serve anything: the PAC sends it to the proxy, and a `replace`
// override answers without an upstream (the addon runs mitmproxy with `connection_strategy: lazy`
// precisely so that works).
//
// There is no UI-test target, so the app writes its evidence to two JSON arrays in its own
// container, which the harness reads with `xcrun simctl get_app_container`:
//
//   Documents/launches.json   one record per launch, written before any networking — so "the app
//                             was not launched" can be checked against something that does not
//                             depend on a request ever finishing
//   Documents/results.json    one record per launch: the status and body the app received
//                             (`fetch` counts requests within that launch — 1 unless the button
//                             was tapped, so the array's length is the number of launches)
//   Documents/displayed.json  one record per rendered label, written from the value the label
//                             itself is bound to — evidence at the label rather than at the
//                             socket, which is as close to "displayed" as this gets without a
//                             UI-test target
//
// Everything the harness asserts on is in those files. Changing a key here means changing
// engine/tests/acceptance/test_simulator_path.py to match.

import SwiftUI

/// The one request this app makes. The host is `api.example.com` (RFC 2606) because that is what
/// the acceptance profile intercepts; the path is the acceptance scenarios' matcher.
private let fixtureURL = URL(string: "https://api.example.com/api/v1/fixture")!

/// How often a launch retries before recording a failure. Only transport errors are retried — a
/// 503 from an override is an answer, not a failure — and all attempts are folded into the single
/// record for this launch, so the number of records stays the number of launches.
private let maxAttempts = 3
private let retryDelay = Duration.seconds(1)

private func timestamp() -> String {
    ISO8601DateFormatter().string(from: Date())
}

/// Appends one object to a JSON array file in Documents, and says what went wrong rather than
/// losing the record quietly: an unreadable file is reported, never replaced with a fresh array
/// that would drop every earlier launch.
enum Recorder {
    static func append(_ record: [String: Any], to name: String) -> String? {
        guard
            let directory = FileManager.default.urls(
                for: .documentDirectory,
                in: .userDomainMask
            ).first
        else {
            return "no Documents directory in this container"
        }
        let url = directory.appendingPathComponent(name)

        var all: [Any] = []
        if FileManager.default.fileExists(atPath: url.path) {
            do {
                let parsed = try JSONSerialization.jsonObject(with: try Data(contentsOf: url))
                guard let array = parsed as? [Any] else { return "\(name) is not a JSON array" }
                all = array
            } catch {
                return "could not read \(name): \(error)"
            }
        }
        all.append(record)

        do {
            let data = try JSONSerialization.data(withJSONObject: all, options: [.prettyPrinted])
            try data.write(to: url, options: .atomic)
            return nil
        } catch {
            return "could not write \(name): \(error)"
        }
    }
}

@MainActor
final class Probe: ObservableObject {
    /// What the label shows. Also what is recorded as "displayed", so the two cannot disagree.
    @Published private(set) var summary = "requesting \(fixtureURL.absoluteString)…"
    /// A recording failure has nowhere else to go — the harness reads files, not logs — so it is
    /// shown on screen instead of being swallowed.
    @Published private(set) var recordingProblem: String?

    private var launched = false
    private var fetches = 0

    /// The launch request. Guarded so a second `onAppear` (a re-render, the app returning to the
    /// foreground) cannot add a request the scenario did not expect.
    ///
    /// The launch is recorded first, and synchronously. A result appears only once a request has
    /// finished, so an app that was launched and then hung waiting for a network that never came
    /// would leave no trace at all — and "the app was not launched" would pass having proved
    /// nothing. This is that trace.
    func fetchOnLaunch() {
        guard !launched else { return }
        launched = true
        if let problem = Recorder.append(["at": timestamp()], to: "launches.json") {
            recordingProblem = problem
        }
        fetch()
    }

    func fetch() {
        fetches += 1
        let attemptNumber = fetches
        Task { await self.perform(request: attemptNumber) }
    }

    private func perform(request number: Int) async {
        var record: [String: Any] = ["fetch": number, "url": fixtureURL.absoluteString]
        var text = ""

        for attempt in 1...maxAttempts {
            record["attempts"] = attempt
            do {
                var request = URLRequest(url: fixtureURL)
                // The point of the exercise is a live request every launch; a cached 200 would
                // make a scenario switch invisible.
                request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
                let (data, response) = try await URLSession.shared.data(for: request)
                let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                let body = String(data: data, encoding: .utf8) ?? "<\(data.count) bytes, not UTF-8>"
                record["status"] = status
                record["body"] = body
                record["error"] = nil
                text = "HTTP \(status)\n\(body)"
                break
            } catch {
                // A transport error means nothing reached the proxy — usually the PAC not picked
                // up yet, moments after `up` installed it. Retry, and keep the last error so a
                // route that never comes up is recorded as such rather than as an empty result.
                record["status"] = 0
                record["body"] = ""
                record["error"] = String(describing: error)
                text = "request failed: \(error.localizedDescription)"
                if attempt < maxAttempts {
                    try? await Task.sleep(for: retryDelay)
                }
            }
        }

        record["at"] = timestamp()
        if let problem = Recorder.append(record, to: "results.json") {
            recordingProblem = problem
        }
        summary = text
    }

    /// Called with the string the label is rendering, at the moment it renders it.
    func noteDisplayed(_ text: String) {
        if let problem = Recorder.append(["text": text, "at": timestamp()], to: "displayed.json") {
            recordingProblem = problem
        }
    }
}

struct ContentView: View {
    @ObservedObject var probe: Probe

    var body: some View {
        VStack(spacing: 16) {
            Text(probe.summary)
                .font(.system(.footnote, design: .monospaced))
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityIdentifier("fixtureResult")
                // Written from the value this label is bound to, every time it changes: the
                // record says what was on screen, not merely what arrived over the wire.
                .task(id: probe.summary) { probe.noteDisplayed(probe.summary) }

            if let problem = probe.recordingProblem {
                Text(problem)
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .accessibilityIdentifier("fixtureProblem")
            }

            // For driving a sequence by hand. The harness relaunches the app instead, because a
            // tap needs a UI-test target and a relaunch is the thing Lyrebird's users do anyway.
            Button("Fetch again") { probe.fetch() }
                .accessibilityIdentifier("fixtureFetchAgain")

            Spacer()
        }
        .padding()
        .onAppear { probe.fetchOnLaunch() }
    }
}

@main
struct FixtureApp: App {
    @StateObject private var probe = Probe()

    var body: some Scene {
        WindowGroup {
            ContentView(probe: probe)
        }
    }
}
