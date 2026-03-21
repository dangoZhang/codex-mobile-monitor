import Foundation

enum ComposerAccessMode: String, CaseIterable, Identifiable {
    case readOnly = "read-only"
    case workspaceWrite = "workspace-write"
    case dangerFullAccess = "danger-full-access"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .readOnly:
            return "只读"
        case .workspaceWrite:
            return "工作区写入"
        case .dangerFullAccess:
            return "完全访问"
        }
    }

    var subtitle: String {
        switch self {
        case .readOnly:
            return "允许分析和回复，不允许修改文件"
        case .workspaceWrite:
            return "允许修改当前项目工作区"
        case .dangerFullAccess:
            return "允许不受沙箱限制地执行"
        }
    }
}

enum ComposerModelOption: String, CaseIterable, Identifiable {
    case automatic = ""
    case gpt54 = "gpt-5.4"
    case gpt53Codex = "gpt-5.3-codex"
    case gpt52 = "gpt-5.2"
    case o3 = "o3"

    var id: String { rawValue.isEmpty ? "automatic" : rawValue }

    var title: String {
        switch self {
        case .automatic:
            return "跟随桌面默认"
        case .gpt54:
            return "GPT-5.4"
        case .gpt53Codex:
            return "GPT-5.3-Codex"
        case .gpt52:
            return "GPT-5.2"
        case .o3:
            return "O3"
        }
    }

    static func from(raw: String?) -> ComposerModelOption {
        guard let raw, let matched = Self.allCases.first(where: { $0.rawValue == raw }) else {
            return .automatic
        }
        return matched
    }
}

struct DraftAttachment: Identifiable, Hashable {
    let id: UUID
    let fileURL: URL
    let displayName: String
    let contentType: String
    let isImage: Bool
    let byteCount: Int64
}

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
    let projectRoot: String?
    let projectName: String?
    let source: String
    let sourceKind: String?
    let gitBranch: String?
    let originator: String?
    let imported: Bool
    let desktopThread: Bool
    let dataSource: String
    let rolloutPath: String?
    let modelProvider: String?
    let cliVersion: String?
    let parentThreadID: String?
    let sourceDepth: Int?
    let agentNickname: String?
    let agentRole: String?
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
        case projectRoot = "project_root"
        case projectName = "project_name"
        case source
        case sourceKind = "source_kind"
        case gitBranch = "git_branch"
        case originator
        case imported
        case desktopThread = "desktop_thread"
        case dataSource = "data_source"
        case rolloutPath = "rollout_path"
        case modelProvider = "model_provider"
        case cliVersion = "cli_version"
        case parentThreadID = "parent_thread_id"
        case sourceDepth = "source_depth"
        case agentNickname = "agent_nickname"
        case agentRole = "agent_role"
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
    let projectRoot: String?
    let projectName: String?
    let source: String
    let sourceKind: String?
    let gitBranch: String?
    let originator: String?
    let imported: Bool
    let desktopThread: Bool
    let dataSource: String
    let rolloutPath: String?
    let modelProvider: String?
    let cliVersion: String?
    let parentThreadID: String?
    let sourceDepth: Int?
    let agentNickname: String?
    let agentRole: String?
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
        case projectRoot = "project_root"
        case projectName = "project_name"
        case source
        case sourceKind = "source_kind"
        case gitBranch = "git_branch"
        case originator
        case imported
        case desktopThread = "desktop_thread"
        case dataSource = "data_source"
        case rolloutPath = "rollout_path"
        case modelProvider = "model_provider"
        case cliVersion = "cli_version"
        case parentThreadID = "parent_thread_id"
        case sourceDepth = "source_depth"
        case agentNickname = "agent_nickname"
        case agentRole = "agent_role"
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

struct BoardListResponse: Decodable {
    let boards: [BoardSummary]
}

struct BoardFolderListResponse: Decodable {
    let folders: [BoardFolderSummary]
}

struct BoardFolderSummary: Decodable, Identifiable, Hashable {
    let id: String
    let name: String
    let path: String
    let boardCount: Int
    let updatedAt: Date

    private enum CodingKeys: String, CodingKey {
        case id
        case name
        case path
        case boardCount = "board_count"
        case updatedAt = "updated_at"
    }
}

struct BoardSummary: Decodable, Identifiable, Hashable {
    let id: String
    let title: String
    let path: String
    let folderPath: String
    let updatedAt: Date
    let taskCount: Int
    let threadCount: Int
    let totals: BoardTotals

    private enum CodingKeys: String, CodingKey {
        case id
        case title
        case path
        case folderPath = "folder_path"
        case updatedAt = "updated_at"
        case taskCount = "task_count"
        case threadCount = "thread_count"
        case totals
    }
}

struct BoardDetail: Decodable {
    let id: String
    let title: String
    let path: String
    let folderPath: String
    let generatedAt: Date
    let updatedAt: Date
    let taskCount: Int
    let threadCount: Int
    let baseBranch: String?
    let targetRepoRoot: String?
    let repo: BoardRepoSnapshot?
    let totals: BoardTotals
    let columns: [BoardColumn]
    let threads: [BoardThread]

    private enum CodingKeys: String, CodingKey {
        case id
        case title
        case path
        case folderPath = "folder_path"
        case generatedAt = "generated_at"
        case updatedAt = "updated_at"
        case taskCount = "task_count"
        case threadCount = "thread_count"
        case baseBranch = "base_branch"
        case targetRepoRoot = "target_repo_root"
        case repo
        case totals
        case columns
        case threads
    }
}

struct BoardTotals: Decodable, Hashable {
    let blocked: Int
    let inProgress: Int
    let todo: Int
    let done: Int

    private enum CodingKeys: String, CodingKey {
        case blocked
        case inProgress = "in_progress"
        case todo
        case done
    }
}

struct BoardColumn: Decodable, Identifiable, Hashable {
    let id: String
    let status: String
    let title: String
    let count: Int
    let tasks: [BoardTask]
}

struct BoardTask: Decodable, Identifiable, Hashable {
    let id: String
    let thread: String
    let title: String
    let owner: String
    let status: String
    let dependsOn: String
    let output: String
    let lineNo: Int
    let slot: String
    let displayName: String
    let role: String
    let autoBranch: Bool
    let latestLog: BoardLogEntry?
    let branches: BoardBranches?
    let runtime: BoardRuntimeSnapshot?

    private enum CodingKeys: String, CodingKey {
        case id
        case thread
        case title
        case owner
        case status
        case dependsOn = "depends_on"
        case output
        case lineNo = "line_no"
        case slot
        case displayName = "display_name"
        case role
        case autoBranch = "auto_branch"
        case latestLog = "latest_log"
        case branches
        case runtime
    }
}

struct BoardThread: Decodable, Identifiable, Hashable {
    let thread: String
    let slot: String
    let displayName: String
    let role: String
    let autoBranch: Bool
    let task: BoardTask?
    let lastLog: BoardLogEntry?
    let runtimeStart: BoardRuntimeStart?
    let lastInvocation: BoardInvocation?
    let branches: BoardBranches?
    let runtime: BoardRuntimeSnapshot?

    var id: String { thread }

    private enum CodingKeys: String, CodingKey {
        case thread
        case slot
        case displayName = "display_name"
        case role
        case autoBranch = "auto_branch"
        case task
        case lastLog = "last_log"
        case runtimeStart = "runtime_start"
        case lastInvocation = "last_invocation"
        case branches
        case runtime
    }
}

struct BoardRuntimeSnapshot: Decodable, Hashable {
    let sessionCount: Int
    let subagentCount: Int
    let running: Bool
    let stale: Bool
    let staleReason: String?
    let updatedAt: Date?
    let gitBranch: String?
    let latestTitle: String?
    let lastMessagePreview: String?
    let sourceKind: String?
    let agentNickname: String?
    let agentRole: String?

    private enum CodingKeys: String, CodingKey {
        case sessionCount = "session_count"
        case subagentCount = "subagent_count"
        case running
        case stale
        case staleReason = "stale_reason"
        case updatedAt = "updated_at"
        case gitBranch = "git_branch"
        case latestTitle = "latest_title"
        case lastMessagePreview = "last_message_preview"
        case sourceKind = "source_kind"
        case agentNickname = "agent_nickname"
        case agentRole = "agent_role"
    }
}

struct BoardLogEntry: Decodable, Hashable {
    let timestamp: String
    let type: String
    let message: String
    let lineNo: Int

    private enum CodingKeys: String, CodingKey {
        case timestamp
        case type
        case message
        case lineNo = "line_no"
    }
}

struct BoardRuntimeStart: Decodable, Hashable {
    let timestamp: String
    let message: String
    let lineNo: Int

    private enum CodingKeys: String, CodingKey {
        case timestamp
        case message
        case lineNo = "line_no"
    }
}

struct BoardInvocation: Decodable, Hashable {
    let startTimestamp: String
    let endTimestamp: String
    let elapsedSeconds: Int
    let endType: String
    let startLineNo: Int
    let endLineNo: Int

    private enum CodingKeys: String, CodingKey {
        case startTimestamp = "start_timestamp"
        case endTimestamp = "end_timestamp"
        case elapsedSeconds = "elapsed_seconds"
        case endType = "end_type"
        case startLineNo = "start_line_no"
        case endLineNo = "end_line_no"
    }
}

struct BoardBranches: Decodable, Hashable {
    let expectedPrefix: String
    let local: [BoardBranchRef]
    let remote: [String]

    private enum CodingKeys: String, CodingKey {
        case expectedPrefix = "expected_prefix"
        case local
        case remote
    }
}

struct BoardBranchRef: Decodable, Hashable {
    let name: String
    let worktree: String?
}

struct BoardRepoSnapshot: Decodable, Hashable {
    let configured: Bool
    let error: String?
    let currentBranch: String?
    let dirty: Bool?
    let legacyLocalBranches: [String]?

    private enum CodingKeys: String, CodingKey {
        case configured
        case error
        case currentBranch = "current_branch"
        case dirty
        case legacyLocalBranches = "legacy_local_branches"
    }
}
