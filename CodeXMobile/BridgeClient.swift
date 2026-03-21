import Foundation

struct SendMessageBody: Encodable {
    let text: String
    let model: String?
    let accessMode: String?
    let cwd: String?

    enum CodingKeys: String, CodingKey {
        case text
        case model
        case accessMode = "access_mode"
        case cwd
    }
}

enum BridgeError: LocalizedError {
    case invalidURL
    case invalidResponse
    case server(String)

    var errorDescription: String? {
        switch self {
        case .invalidURL:
            return "Bridge URL 无效"
        case .invalidResponse:
            return "Bridge 响应无效"
        case .server(let message):
            return message
        }
    }
}

final class BridgeClient {
    private let session: URLSession
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    init() {
        let configuration = URLSessionConfiguration.default
        configuration.timeoutIntervalForRequest = 120
        self.session = URLSession(configuration: configuration)
        self.decoder = JSONDecoder()
        self.decoder.dateDecodingStrategy = .iso8601WithFractionalSeconds
        self.encoder = JSONEncoder()
    }

    func fetchSessions(baseURL: URL) async throws -> [SessionSummary] {
        let (data, response) = try await session.data(from: baseURL.bridgeEndpoint("api/sessions"))
        try validate(response: response, data: data)
        return try decoder.decode(SessionListResponse.self, from: data).sessions
    }

    func fetchSession(baseURL: URL, sessionID: String) async throws -> SessionDetail {
        let url = baseURL.bridgeEndpoint("api/sessions/\(sessionID)")
        let (data, response) = try await session.data(from: url)
        try validate(response: response, data: data)
        return try decoder.decode(SessionDetail.self, from: data)
    }

    func fetchBoards(baseURL: URL) async throws -> [BoardSummary] {
        let (data, response) = try await session.data(from: baseURL.bridgeEndpoint("api/boards"))
        try validate(response: response, data: data)
        return try decoder.decode(BoardListResponse.self, from: data).boards
    }

    func fetchBoardFolders(baseURL: URL) async throws -> [BoardFolderSummary] {
        let (data, response) = try await session.data(from: baseURL.bridgeEndpoint("api/board-folders"))
        try validate(response: response, data: data)
        return try decoder.decode(BoardFolderListResponse.self, from: data).folders
    }

    func fetchBoards(baseURL: URL, folderPath: String?) async throws -> [BoardSummary] {
        var components = URLComponents(url: baseURL.bridgeEndpoint("api/boards"), resolvingAgainstBaseURL: false)
        if let folderPath, !folderPath.isEmpty {
            components?.queryItems = [URLQueryItem(name: "folder", value: folderPath)]
        }
        guard let url = components?.url else {
            throw BridgeError.invalidURL
        }
        let (data, response) = try await session.data(from: url)
        try validate(response: response, data: data)
        return try decoder.decode(BoardListResponse.self, from: data).boards
    }

    func fetchBoard(baseURL: URL, boardID: String) async throws -> BoardDetail {
        let url = baseURL.bridgeEndpoint("api/boards/\(boardID)")
        let (data, response) = try await session.data(from: url)
        try validate(response: response, data: data)
        return try decoder.decode(BoardDetail.self, from: data)
    }

    func sendMessage(
        baseURL: URL,
        sessionID: String,
        text: String,
        model: String?,
        accessMode: String?,
        cwd: String?,
        attachments: [DraftAttachment]
    ) async throws {
        var request = URLRequest(url: baseURL.bridgeEndpoint("api/sessions/\(sessionID)/messages"))
        request.httpMethod = "POST"
        if attachments.isEmpty {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try encoder.encode(
                SendMessageBody(
                    text: text,
                    model: model,
                    accessMode: accessMode,
                    cwd: cwd
                )
            )
        } else {
            let boundary = "CodeXMobile-\(UUID().uuidString)"
            request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
            request.httpBody = try makeMultipartBody(
                boundary: boundary,
                text: text,
                model: model,
                accessMode: accessMode,
                cwd: cwd,
                attachments: attachments
            )
        }
        let (data, response) = try await session.data(for: request)
        try validate(response: response, data: data)
    }

    func streamEvents(
        baseURL: URL,
        sessionID: String,
        after: Int,
        onEvent: @escaping @Sendable (BridgeEvent) async -> Void,
        onFailure: @escaping @Sendable (Error) async -> Void
    ) -> Task<Void, Never> {
        Task.detached(priority: .background) {
            do {
                var components = URLComponents(url: baseURL.bridgeEndpoint("api/sessions/\(sessionID)/events"), resolvingAgainstBaseURL: false)
                components?.queryItems = [URLQueryItem(name: "after", value: String(after))]
                guard let url = components?.url else {
                    throw BridgeError.invalidURL
                }

                let request = URLRequest(url: url)
                let (bytes, response) = try await self.session.bytes(for: request)
                try self.validate(response: response, data: Data())

                var currentID: String?
                var dataLines: [String] = []
                for try await line in bytes.lines {
                    if line.isEmpty {
                        guard !dataLines.isEmpty else {
                            currentID = nil
                            continue
                        }
                        let merged = dataLines.joined(separator: "\n")
                        if let payload = merged.data(using: .utf8) {
                            let event = try self.decoder.decode(BridgeEvent.self, from: payload)
                            await onEvent(event)
                        }
                        currentID = nil
                        dataLines = []
                        continue
                    }

                    if line.hasPrefix("id:") {
                        currentID = String(line.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                    } else if line.hasPrefix("data:") {
                        dataLines.append(String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces))
                    } else if line.hasPrefix(":") {
                        continue
                    }
                }
                _ = currentID
            } catch {
                await onFailure(error)
            }
        }
    }

    private func validate(response: URLResponse, data: Data) throws {
        guard let http = response as? HTTPURLResponse else {
            throw BridgeError.invalidResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let message = object["error"] as? String {
                throw BridgeError.server(message)
            }
            throw BridgeError.server("Bridge 返回状态码 \(http.statusCode)")
        }
    }

    private func makeMultipartBody(
        boundary: String,
        text: String,
        model: String?,
        accessMode: String?,
        cwd: String?,
        attachments: [DraftAttachment]
    ) throws -> Data {
        var body = Data()
        let fields = [
            ("text", text),
            ("model", model ?? ""),
            ("access_mode", accessMode ?? ""),
            ("cwd", cwd ?? "")
        ]

        for (name, value) in fields where !value.isEmpty {
            body.appendFormField(named: name, value: value, boundary: boundary)
        }

        for attachment in attachments {
            let data = try Data(contentsOf: attachment.fileURL)
            body.appendFileField(
                named: "attachments",
                filename: attachment.displayName,
                mimeType: attachment.contentType,
                data: data,
                boundary: boundary
            )
        }

        body.appendString("--\(boundary)--\r\n")
        return body
    }
}

private extension JSONDecoder.DateDecodingStrategy {
    static let iso8601WithFractionalSeconds = custom { decoder in
        let container = try decoder.singleValueContainer()
        let raw = try container.decode(String.self)
        if let date = ISO8601DateFormatter.fractional.date(from: raw) ?? ISO8601DateFormatter.simple.date(from: raw) {
            return date
        }
        throw DecodingError.dataCorruptedError(in: container, debugDescription: "Invalid ISO8601 date: \(raw)")
    }
}

private extension ISO8601DateFormatter {
    static let fractional: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    static let simple: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()
}

private extension URL {
    func bridgeEndpoint(_ path: String) -> URL {
        path
            .split(separator: "/")
            .reduce(self) { partial, component in
                partial.appendingPathComponent(String(component))
            }
    }
}

private extension Data {
    mutating func appendString(_ value: String) {
        append(Data(value.utf8))
    }

    mutating func appendFormField(named name: String, value: String, boundary: String) {
        appendString("--\(boundary)\r\n")
        appendString("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n")
        appendString("\(value)\r\n")
    }

    mutating func appendFileField(named name: String, filename: String, mimeType: String, data: Data, boundary: String) {
        appendString("--\(boundary)\r\n")
        appendString("Content-Disposition: form-data; name=\"\(name)\"; filename=\"\(filename)\"\r\n")
        appendString("Content-Type: \(mimeType)\r\n\r\n")
        append(data)
        appendString("\r\n")
    }
}
