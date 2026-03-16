import Foundation

struct SessionListResponse: Decodable {
    let sessions: [SessionSummary]
}

struct SessionSummary: Decodable, Identifiable, Hashable {
    let id: String
    let title: String
    let createdAt: Date
    let updatedAt: Date
    let threadID: String?
    let cwd: String?
    let source: String
    let originator: String?
    let imported: Bool
    let desktopThread: Bool
    let dataSource: String
    let rolloutPath: String?
    let modelProvider: String?
    let cliVersion: String?
    let bridgeReplyAvailable: Bool
    let running: Bool
    let messageCount: Int
    let lastMessagePreview: String

    private enum CodingKeys: String, CodingKey {
        case id
        case title
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case threadID = "thread_id"
        case cwd
        case source
        case originator
        case imported
        case desktopThread = "desktop_thread"
        case dataSource = "data_source"
        case rolloutPath = "rollout_path"
        case modelProvider = "model_provider"
        case cliVersion = "cli_version"
        case bridgeReplyAvailable = "bridge_reply_available"
        case running
        case messageCount = "message_count"
        case lastMessagePreview = "last_message_preview"
    }
}

struct SessionDetail: Decodable {
    let id: String
    let title: String
    let createdAt: Date
    let updatedAt: Date
    let threadID: String?
    let cwd: String?
    let source: String
    let originator: String?
    let imported: Bool
    let desktopThread: Bool
    let dataSource: String
    let rolloutPath: String?
    let modelProvider: String?
    let cliVersion: String?
    let bridgeReplyAvailable: Bool
    let running: Bool
    let messageCount: Int
    let lastMessagePreview: String
    let messages: [ChatMessage]
    let lastEventSequence: Int

    private enum CodingKeys: String, CodingKey {
        case id
        case title
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case threadID = "thread_id"
        case cwd
        case source
        case originator
        case imported
        case desktopThread = "desktop_thread"
        case dataSource = "data_source"
        case rolloutPath = "rollout_path"
        case modelProvider = "model_provider"
        case cliVersion = "cli_version"
        case bridgeReplyAvailable = "bridge_reply_available"
        case running
        case messageCount = "message_count"
        case lastMessagePreview = "last_message_preview"
        case messages
        case lastEventSequence = "last_event_sequence"
    }
}

struct ChatMessage: Decodable, Identifiable, Hashable {
    let id: String
    let role: String
    let text: String
    let createdAt: Date
    let state: String
    let kind: String
    let phase: String?
    let toolName: String?

    private enum CodingKeys: String, CodingKey {
        case id
        case role
        case text
        case createdAt = "created_at"
        case state
        case kind
        case phase
        case toolName = "tool_name"
    }
}

struct BridgeEvent: Decodable {
    let seq: Int
    let type: String
    let timestamp: Date
    let sessionID: String
    let payload: Payload

    private enum CodingKeys: String, CodingKey {
        case seq
        case type
        case timestamp
        case sessionID = "session_id"
        case payload
    }

    struct Payload: Decodable {
        let message: ChatMessage?
        let line: String?
        let threadID: String?
        let running: Bool?

        private enum CodingKeys: String, CodingKey {
            case message
            case line
            case threadID = "thread_id"
            case running
        }
    }
}
