#if DEBUG
    import AppKit

    /// Synthetic control API for native UI tests and local layout inspection. No profile is loaded.
    enum BrowserPreview {
        static let fingerprint = "preview"
        static var initialScenario: String {
            ProcessInfo.processInfo.arguments.contains("--design-preview") ? "remove-an-item" : "orders-pending"
        }
        static var names: [String] {
            ProcessInfo.processInfo.arguments.contains("--design-preview")
                ? ["default", "empty-list", "remove-an-item", "service-unavailable", "slow-network"]
                : [initialScenario, "orders-complete", "empty", "partial", "orders/previous", "orders/complete"]
        }

        static func notes(_ name: String) -> String {
            name == "remove-an-item"
                ? "Start with three items. Delete the notebook, then fetch the list again to see the remaining two. Repeated reads keep the current response."
                : "Synthetic scenario for inspecting the native browser."
        }

        @MainActor
        static func makeModel() -> AppModel {
            let defaults = UserDefaults(suiteName: "com.lyrebird.AppKitPreview")!
            defaults.setVolatileDomain(
                [
                    Config.controlURLKey: "http://preview.invalid",
                    Config.lyrebirdPathKey: "/nonexistent/preview-lyrebird",
                    Config.profilePathKey: "",
                    Config.dockOnlyWhileWindowOpenKey: false,
                ], forName: UserDefaults.argumentDomain)
            Config.defaults = defaults
            let configuration = URLSessionConfiguration.ephemeral
            configuration.protocolClasses = [PreviewProtocol.self]
            let model = AppModel(
                client: MockClient(
                    base: URL(string: "http://preview.invalid")!, session: URLSession(configuration: configuration)),
                autoStart: false, expectedFingerprint: fingerprint, discover: { fingerprint })
            model.start()
            return model
        }

        static func snapshot(_ name: String) -> RulesSnapshot {
            if name == "remove-an-item" {
                let initial = JSONValue.object([
                    "items": .array(
                        ["notebook", "pencil", "backpack"].map {
                            .object(["id": .string($0), "name": .string($0.capitalized)])
                        })
                ])
                let remaining = JSONValue.object([
                    "items": .array(
                        ["pencil", "backpack"].map {
                            .object(["id": .string($0), "name": .string($0.capitalized)])
                        })
                ])
                let headers = ["Content-Type": "application/json"]
                return RulesSnapshot(
                    scenario: name, notWhole: [],
                    rules: [
                        RuleRow(
                            id: "ovr_items", match: RuleMatch(method: "GET", path: "/api/items"),
                            rewrite: Rewrite(
                                active: true, mode: "replace", bodyKind: "json",
                                sequence: RewriteSequence(
                                    advanceOn: RuleMatch(method: "DELETE", path: "/api/items/notebook"),
                                    onExhausted: "repeatLast",
                                    steps: [
                                        StepSummary(
                                            status: 200, headers: headers, body: initial, bodyKind: "json",
                                            bodyBytes: (try? JSONEncoder().encode(initial).count)),
                                        StepSummary(
                                            status: 200, headers: headers, body: remaining, bodyKind: "json",
                                            bodyBytes: (try? JSONEncoder().encode(remaining).count)),
                                    ]))),
                        RuleRow(
                            id: "ovr_delete", match: RuleMatch(method: "DELETE", path: "/api/items/notebook"),
                            rewrite: Rewrite(active: true, mode: "replace", status: 204, bodyKind: "none")),
                    ])
            }
            if name == "empty" { return RulesSnapshot(scenario: name, notWhole: [], rules: []) }
            let body = JSONValue.object([
                "items": .array([
                    .object(["id": .string("item-42"), "title": .string("Example order"), "pending": .bool(true)])
                ]),
                "description": .string(
                    String(repeating: "Synthetic response text for testing line wrapping. ", count: 12)),
            ])
            let read = RuleRow(
                id: "ovr_orders", match: RuleMatch(method: "GET", path: "/api/v1/orders/pending"),
                notes: "Reads repeat until the update request arrives.",
                rewrite: Rewrite(
                    active: true, mode: "replace", bodyKind: "json",
                    sequence: RewriteSequence(
                        advanceOn: RuleMatch(method: "PATCH", path: "/api/v1/orders/42"), onExhausted: "error",
                        steps: [
                            StepSummary(
                                status: 200, headers: ["Content-Type": "application/json"], body: body,
                                bodyKind: "json", inherited: ["headers"]),
                            StepSummary(status: 200, body: .object(["items": .array([])]), bodyKind: "json"),
                        ]
                    )))
            let write = RuleRow(
                id: "ovr_update", match: RuleMatch(method: "PATCH", path: "/api/v1/orders/42"),
                rewrite: Rewrite(active: true, mode: "replace", status: 204, bodyKind: "none"))
            let patch = RuleRow(
                id: "ovr_patch", match: RuleMatch(method: "GET", path: "/api/v1/settings"),
                patch: .object(["enabled": .bool(false)]),
                rewrite: Rewrite(active: true, mode: "patch", bodyKind: "json"))
            return RulesSnapshot(
                scenario: name,
                notWhole: name == "partial" ? ["One synthetic rule was dropped: unknown matcher field."] : [],
                rules: [read, write, patch])
        }
    }

    private final class PreviewProtocol: URLProtocol, @unchecked Sendable {
        private static let lock = NSLock()
        private static var active = BrowserPreview.initialScenario
        private static var cleared = false

        override class func canInit(with request: URLRequest) -> Bool {
            true
        }

        override class func canonicalRequest(for request: URLRequest) -> URLRequest {
            request
        }

        override func stopLoading() {}

        override func startLoading() {
            do {
                let data: Data = try Self.lock.withLock {
                    let encoder = JSONEncoder()
                    switch request.url?.path {
                    case "/__mock__/health":
                        return try encoder.encode(
                            Health(
                                activeScenario: Self.active, overrideCount: 3, proxyUp: true, intercepting: true,
                                profileFingerprint: BrowserPreview.fingerprint))
                    case "/__mock__/scenarios":
                        return try encoder.encode(
                            ScenarioList(
                                active: Self.active,
                                scenarios: BrowserPreview.names.map {
                                    ScenarioSummary(
                                        name: $0, overrideCount: $0 == "empty" ? 0 : 3, verified: false,
                                        notes: BrowserPreview.notes($0))
                                }))
                    case "/__mock__/scenarios/active":
                        var body = request.httpBody ?? Data()
                        if let stream = request.httpBodyStream {
                            stream.open()
                            defer { stream.close() }
                            var bytes = [UInt8](repeating: 0, count: 1024)
                            while stream.hasBytesAvailable {
                                let count = stream.read(&bytes, maxLength: bytes.count)
                                if count <= 0 { break }
                                body.append(contentsOf: bytes.prefix(count))
                            }
                        }
                        if let object = try JSONSerialization.jsonObject(with: body) as? [String: String],
                            let name = object["name"]
                        {
                            Self.active = name
                        }
                        return Data("{}".utf8)
                    case "/__mock__/rules":
                        let name =
                            URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first {
                                $0.name == "scenario"
                            }?.value ?? Self.active
                        return try encoder.encode(BrowserPreview.snapshot(name))
                    case "/__mock__/recent":
                        if request.httpMethod == "DELETE" {
                            Self.cleared = true
                            return Data("{}".utf8)
                        }
                        let entries: [RecentEntry] =
                            Self.cleared
                            ? []
                            : [
                                RecentEntry(
                                    id: "event-1", time: "2026-01-01T12:00:00Z", method: "GET",
                                    path: "/api/v1/orders/pending", status: 200, matched: "ovr_orders", selectedStep: 1,
                                    runId: "synthetic-run"),
                                RecentEntry(
                                    id: "event-2", method: "GET",
                                    path: "/api/v1/catalog/items/example-item/availability", status: 200),
                                RecentEntry(id: "event-3", method: "POST", path: "/api/v1/orders", status: 404),
                                RecentEntry(
                                    id: "event-4", method: "PATCH", path: "/api/v1/settings", status: 200,
                                    patchSkipped: "Response was not JSON"),
                            ]
                        return try encoder.encode(entries)
                    default: return Data("{}".utf8)
                    }
                }
                let response = HTTPURLResponse(
                    url: request.url!, statusCode: 200, httpVersion: nil,
                    headerFields: ["Content-Type": "application/json"])!
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: data)
                client?.urlProtocolDidFinishLoading(self)
            } catch { client?.urlProtocol(self, didFailWithError: error) }
        }
    }
#endif
