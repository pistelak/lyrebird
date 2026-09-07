import Foundation

/// Answers the client's requests from the test that installed the handler, and records what was
/// sent. Registered on an ephemeral `URLSessionConfiguration`, so nothing here reaches a network.
///
/// The handler and the recording live behind a lock: `URLSession` calls `startLoading()` on its
/// own queue while the test reads `requests` on the main actor. Every test that uses it lives in
/// one class and awaits its requests before returning, so no two tests share the handler at once.
final class StubURLProtocol: URLProtocol {
    typealias Handler = @Sendable (URLRequest) throws -> (HTTPURLResponse, Data)

    private static let lock = NSLock()
    private static var handler: Handler?
    private static var recorded: [URLRequest] = []

    /// Installs the handler and clears the recording, so each test starts from an empty log.
    static func install(_ handler: @escaping Handler) {
        lock.lock()
        defer { lock.unlock() }
        self.handler = handler
        recorded = []
    }

    static func reset() {
        lock.lock()
        defer { lock.unlock() }
        handler = nil
        recorded = []
    }

    static var requests: [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }

    /// A session built on this protocol and nothing else — no cache, no cookies, no network.
    static func session() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    /// `URLSession` moves a request's body into a stream before a protocol sees it, so
    /// `httpBody` on a recorded request is nil and the body has to be drained from the stream.
    static func body(of request: URLRequest) -> Data {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: buffer.count)
            if read <= 0 { break }
            data.append(buffer, count: read)
        }
        return data
    }

    override class func canInit(with request: URLRequest) -> Bool { true }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.lock.lock()
        Self.recorded.append(request)
        let handler = Self.handler
        Self.lock.unlock()

        guard let handler else {
            // No handler is a test bug, not a server condition; fail the request loudly rather
            // than hanging the caller.
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

/// Fixture helpers shared by the activate tests.
enum Stub {
    /// RFC 2606 reserved: no fixture may name a host somebody actually runs.
    static let base = URL(string: "http://lyrebird.test:8088")!

    static func response(_ request: URLRequest, _ status: Int) -> HTTPURLResponse {
        HTTPURLResponse(
            url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"])!
    }

    /// Plausible bodies for the three polled reads, so a refresh triggered by `activate` succeeds
    /// instead of failing for a reason the test did not intend.
    static func read(_ request: URLRequest) -> (HTTPURLResponse, Data) {
        let path = request.url?.path ?? ""
        let body: String
        switch path {
        case "/__mock__/health":
            body = #"{"activeScenario":"baseline","proxyUp":true,"intercepting":true}"#
        case "/__mock__/scenarios":
            body = #"{"active":"baseline","scenarios":[]}"#
        default:
            body = "[]"
        }
        return (response(request, 200), Data(body.utf8))
    }
}
