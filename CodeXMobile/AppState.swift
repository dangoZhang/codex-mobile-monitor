import Foundation
import SwiftUI
import UniformTypeIdentifiers

private enum BridgeURLParser {
    static func makeBaseURL(from raw: String) -> URL? {
        let cleaned = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty else { return nil }
        return URL(string: cleaned)
    }
}

@MainActor
final class AppState: ObservableObject {
    private static let refreshLoopIntervalNanoseconds: UInt64 = 6_000_000_000

    @Published var selectedModel = ComposerModelOption.automatic
    @Published var selectedAccessMode = ComposerAccessMode.workspaceWrite
    @Published var draftAttachments: [DraftAttachment] = []
    @Published var sessions: [SessionSummary] = []
    @Published var selectedSessionID: String?
    @Published var messages: [ChatMessage] = []
    @Published var draft: String = ""
    @Published var statusText: String = "Monitoring"
    @Published var isLoading = false
    @Published var isRunning = false
    @Published var lastOutputLine: String?
    @Published var errorMessage: String?
    @Published var title: String = "Codex Threads"
    @Published var currentCWD: String?
    @Published var currentProjectRoot: String?
    @Published var currentProjectName: String?
    @Published var currentSource: String = "bridge"
    @Published var currentSourceKind: String = "bridge"
    @Published var currentOriginator: String?
    @Published var isImportedSession = false
    @Published var currentDataSource: String = "bridge"
    @Published var currentRolloutPath: String?
    @Published var currentGitBranch: String?
    @Published var currentModelProvider: String?
    @Published var currentCLIVersion: String?
    @Published var currentParentThreadID: String?
    @Published var currentSourceDepth: Int?
    @Published var currentAgentNickname: String?
    @Published var currentAgentRole: String?
    @Published var bridgeReplyAvailable = false

    private let bridge = BridgeClient()
    private var streamTask: Task<Void, Never>?
    private var refreshTask: Task<Void, Never>?
    private var cursors: [String: Int] = [:]
    private(set) var baseURL: URL?

    init() {
        if let raw = UserDefaults.standard.string(forKey: "bridge_url") {
            self.baseURL = BridgeURLParser.makeBaseURL(from: raw)
        }
        selectedModel = ComposerModelOption.from(raw: UserDefaults.standard.string(forKey: "composer_model"))
        if let rawAccessMode = UserDefaults.standard.string(forKey: "composer_access_mode"),
           let accessMode = ComposerAccessMode(rawValue: rawAccessMode) {
            selectedAccessMode = accessMode
        }
    }

    func updateBridgeURL(_ raw: String) {
        UserDefaults.standard.set(raw, forKey: "bridge_url")
        baseURL = BridgeURLParser.makeBaseURL(from: raw)
        restartAutoRefreshLoop()
    }

    func bootstrap() async {
        restartAutoRefreshLoop()
        await refreshSessions(selectFirst: true)
    }

    func refreshSessions(selectFirst: Bool = false) async {
        guard let baseURL else {
            errorMessage = "先填写 Mac Bridge 地址"
            return
        }
        isLoading = true
        defer { isLoading = false }
        do {
            let fetched = try await bridge.fetchSessions(baseURL: baseURL)
            sessions = fetched
            if let selectedSessionID, fetched.contains(where: { $0.id == selectedSessionID }) {
                await loadSession(id: selectedSessionID, reconnect: false)
            } else if selectFirst, let first = fetched.first {
                await loadSession(id: first.id, reconnect: true)
            } else if fetched.isEmpty {
                selectedSessionID = nil
                messages = []
            }
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func loadSession(id: String, reconnect: Bool = true) async {
        guard let baseURL else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            let detail = try await bridge.fetchSession(baseURL: baseURL, sessionID: id)
            apply(detail: detail)
            if reconnect {
                connectStream(for: id, after: detail.lastEventSequence)
            }
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func sendDraft() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty || !draftAttachments.isEmpty else { return }
        guard let sessionID = selectedSessionID else {
            errorMessage = "先选择一个 Codex 线程"
            return
        }
        guard let baseURL else { return }
        guard bridgeReplyAvailable else {
            errorMessage = "当前线程只允许监督，不允许回写"
            return
        }

        isRunning = true
        statusText = "Running"
        let attachments = draftAttachments
        do {
            try await bridge.sendMessage(
                baseURL: baseURL,
                sessionID: sessionID,
                text: text,
                model: selectedModel.rawValue.isEmpty ? nil : selectedModel.rawValue,
                accessMode: selectedAccessMode.rawValue,
                cwd: currentCWD,
                attachments: attachments
            )
            draft = ""
            clearDraftAttachments()
            errorMessage = nil
        } catch {
            isRunning = false
            statusText = "Failed"
            errorMessage = error.localizedDescription
        }
    }

    func updateSelectedModel(_ option: ComposerModelOption) {
        selectedModel = option
        UserDefaults.standard.set(option.rawValue, forKey: "composer_model")
    }

    func updateSelectedAccessMode(_ mode: ComposerAccessMode) {
        selectedAccessMode = mode
        UserDefaults.standard.set(mode.rawValue, forKey: "composer_access_mode")
    }

    func addAttachment(data: Data, suggestedName: String, contentType: String, isImage: Bool) throws {
        let draftsDirectory = FileManager.default.temporaryDirectory.appendingPathComponent("CodeXMobileDrafts", isDirectory: true)
        try FileManager.default.createDirectory(at: draftsDirectory, withIntermediateDirectories: true)

        let sanitizedName = sanitizedAttachmentName(from: suggestedName, fallbackExtension: fallbackExtension(for: contentType, isImage: isImage))
        let destination = draftsDirectory.appendingPathComponent("\(UUID().uuidString)-\(sanitizedName)")
        try data.write(to: destination, options: .atomic)

        let attachment = DraftAttachment(
            id: UUID(),
            fileURL: destination,
            displayName: sanitizedName,
            contentType: resolvedContentType(raw: contentType, filename: sanitizedName, isImage: isImage),
            isImage: isImage,
            byteCount: Int64(data.count)
        )
        draftAttachments.append(attachment)
    }

    func importAttachments(from urls: [URL]) throws {
        for url in urls {
            let hasAccess = url.startAccessingSecurityScopedResource()
            defer {
                if hasAccess {
                    url.stopAccessingSecurityScopedResource()
                }
            }

            let data = try Data(contentsOf: url)
            let resourceValues = try? url.resourceValues(forKeys: [.contentTypeKey, .fileSizeKey])
            let resourceType = resourceValues?.contentType
            let contentType = resourceType?.preferredMIMEType ?? resolvedContentType(raw: "", filename: url.lastPathComponent, isImage: false)
            let isImage = resourceType?.conforms(to: .image) ?? false
            try addAttachment(
                data: data,
                suggestedName: url.lastPathComponent,
                contentType: contentType,
                isImage: isImage
            )
        }
    }

    func removeAttachment(id: UUID) {
        guard let index = draftAttachments.firstIndex(where: { $0.id == id }) else { return }
        let attachment = draftAttachments.remove(at: index)
        try? FileManager.default.removeItem(at: attachment.fileURL)
    }

    func clearDraftAttachments() {
        let attachments = draftAttachments
        draftAttachments = []
        for attachment in attachments {
            try? FileManager.default.removeItem(at: attachment.fileURL)
        }
    }

    private func apply(detail: SessionDetail) {
        selectedSessionID = detail.id
        title = detail.title
        currentCWD = detail.cwd
        currentProjectRoot = detail.projectRoot
        currentProjectName = detail.projectName
        currentSource = detail.source
        currentSourceKind = detail.sourceKind ?? detail.source
        currentOriginator = detail.originator
        isImportedSession = detail.imported
        currentDataSource = detail.dataSource
        currentRolloutPath = detail.rolloutPath
        currentGitBranch = detail.gitBranch
        currentModelProvider = detail.modelProvider
        currentCLIVersion = detail.cliVersion
        currentParentThreadID = detail.parentThreadID
        currentSourceDepth = detail.sourceDepth
        currentAgentNickname = detail.agentNickname
        currentAgentRole = detail.agentRole
        bridgeReplyAvailable = detail.bridgeReplyAvailable
        messages = detail.messages
        isRunning = detail.running
        statusText = detail.running ? "Running" : "Monitoring"
        cursors[detail.id] = detail.lastEventSequence
        upsertSummary(from: detail)
    }

    private func connectStream(for sessionID: String, after: Int) {
        guard let baseURL else { return }
        streamTask?.cancel()
        let state = self
        streamTask = bridge.streamEvents(
            baseURL: baseURL,
            sessionID: sessionID,
            after: cursors[sessionID] ?? after,
            onEvent: { event in
                await state.receiveEvent(event)
            },
            onFailure: { error in
                await state.receiveStreamFailure(error)
            }
        )
    }

    private func receiveEvent(_ event: BridgeEvent) {
        handle(event: event)
    }

    private func receiveStreamFailure(_ error: Error) {
        if Task.isCancelled { return }
        statusText = "Disconnected"
        errorMessage = error.localizedDescription
        guard let selectedSessionID else { return }
        let after = cursors[selectedSessionID] ?? 0
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            guard self.selectedSessionID == selectedSessionID else { return }
            self.connectStream(for: selectedSessionID, after: after)
        }
    }

    private func handle(event: BridgeEvent) {
        cursors[event.sessionID] = event.seq
        switch event.type {
        case "assistant.message.completed", "user.message.created", "system.message.created", "run.failed":
            if let message = event.payload.message {
                appendIfNeeded(message)
                Task {
                    await refreshSessions(selectFirst: false)
                }
            }
        case "run.started":
            isRunning = true
            statusText = "Running"
        case "run.output":
            lastOutputLine = event.payload.line
        case "run.state":
            isRunning = event.payload.running ?? false
            statusText = isRunning ? "Running" : "Monitoring"
        case "session.thread":
            statusText = "Connected"
        case "session.synced":
            Task {
                await loadSession(id: event.sessionID, reconnect: false)
                await refreshSessions(selectFirst: false)
            }
        default:
            break
        }
    }

    private func appendIfNeeded(_ message: ChatMessage) {
        guard !messages.contains(where: { $0.id == message.id }) else { return }
        messages.append(message)
        messages.sort { $0.createdAt < $1.createdAt }
    }

    private func upsertSummary(from detail: SessionDetail) {
        let summary = SessionSummary(
            id: detail.id,
            title: detail.title,
            createdAt: detail.createdAt,
            updatedAt: detail.updatedAt,
            threadID: detail.threadID,
            cwd: detail.cwd,
            projectRoot: detail.projectRoot,
            projectName: detail.projectName,
            source: detail.source,
            sourceKind: detail.sourceKind,
            gitBranch: detail.gitBranch,
            originator: detail.originator,
            imported: detail.imported,
            desktopThread: detail.desktopThread,
            dataSource: detail.dataSource,
            rolloutPath: detail.rolloutPath,
            modelProvider: detail.modelProvider,
            cliVersion: detail.cliVersion,
            parentThreadID: detail.parentThreadID,
            sourceDepth: detail.sourceDepth,
            agentNickname: detail.agentNickname,
            agentRole: detail.agentRole,
            bridgeReplyAvailable: detail.bridgeReplyAvailable,
            running: detail.running,
            messageCount: detail.messageCount,
            lastMessagePreview: detail.lastMessagePreview
        )
        if let index = sessions.firstIndex(where: { $0.id == summary.id }) {
            sessions[index] = summary
        } else {
            sessions.insert(summary, at: 0)
        }
        sessions.sort { $0.updatedAt > $1.updatedAt }
    }

    private func restartAutoRefreshLoop() {
        refreshTask?.cancel()
        guard baseURL != nil else { return }

        refreshTask = Task { @MainActor [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: Self.refreshLoopIntervalNanoseconds)
                if Task.isCancelled { break }
                await self.refreshSessions(selectFirst: false)
            }
        }
    }

}

private extension AppState {
    func sanitizedAttachmentName(from name: String, fallbackExtension: String) -> String {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let source = trimmed.isEmpty ? "attachment\(fallbackExtension)" : trimmed
        let pattern = #"[^A-Za-z0-9._-]+"#
        let sanitized = source.replacingOccurrences(of: pattern, with: "-", options: .regularExpression)
        let finalName = sanitized.trimmingCharacters(in: CharacterSet(charactersIn: "-."))
        if finalName.isEmpty {
            return "attachment\(fallbackExtension)"
        }
        if URL(fileURLWithPath: finalName).pathExtension.isEmpty, !fallbackExtension.isEmpty {
            return finalName + fallbackExtension
        }
        return finalName
    }

    func fallbackExtension(for contentType: String, isImage: Bool) -> String {
        if let type = UTType(mimeType: contentType), let preferred = type.preferredFilenameExtension {
            return ".\(preferred)"
        }
        return isImage ? ".jpg" : ""
    }

    func resolvedContentType(raw: String, filename: String, isImage: Bool) -> String {
        if !raw.isEmpty {
            return raw
        }
        if let type = UTType(filenameExtension: URL(fileURLWithPath: filename).pathExtension),
           let mimeType = type.preferredMIMEType {
            return mimeType
        }
        return isImage ? "image/jpeg" : "application/octet-stream"
    }
}

@MainActor
final class BoardState: ObservableObject {
    private static let refreshLoopIntervalNanoseconds: UInt64 = 8_000_000_000

    @Published var folders: [BoardFolderSummary] = []
    @Published var selectedFolderPath: String?
    @Published var boards: [BoardSummary] = []
    @Published var selectedBoardID: String?
    @Published var board: BoardDetail?
    @Published var isLoading = false
    @Published var errorMessage: String?

    private let bridge = BridgeClient()
    private var refreshTask: Task<Void, Never>?
    private(set) var baseURL: URL?

    init() {
        if let raw = UserDefaults.standard.string(forKey: "bridge_url") {
            self.baseURL = BridgeURLParser.makeBaseURL(from: raw)
        }
    }

    func updateBridgeURL(_ raw: String) {
        UserDefaults.standard.set(raw, forKey: "bridge_url")
        baseURL = BridgeURLParser.makeBaseURL(from: raw)
        restartAutoRefreshLoop()
    }

    func bootstrap() async {
        restartAutoRefreshLoop()
        await refreshBoards(selectFirst: true)
    }

    func refreshBoards(selectFirst: Bool = false) async {
        guard let baseURL else {
            errorMessage = "先填写 Mac Bridge 地址"
            folders = []
            selectedFolderPath = nil
            boards = []
            board = nil
            selectedBoardID = nil
            return
        }

        isLoading = true
        defer { isLoading = false }

        do {
            let fetchedFolders = try await bridge.fetchBoardFolders(baseURL: baseURL)
            folders = fetchedFolders

            if let selectedFolderPath, fetchedFolders.contains(where: { $0.path == selectedFolderPath }) {
                self.selectedFolderPath = selectedFolderPath
            } else {
                self.selectedFolderPath = fetchedFolders.first?.path
            }

            let fetched = try await bridge.fetchBoards(baseURL: baseURL, folderPath: self.selectedFolderPath)
            boards = fetched

            if let selectedBoardID, fetched.contains(where: { $0.id == selectedBoardID }) {
                await loadBoard(id: selectedBoardID)
            } else if let first = fetched.first, selectFirst || selectedBoardID != nil || board == nil {
                await loadBoard(id: first.id)
            } else if fetched.isEmpty {
                selectedBoardID = nil
                board = nil
            }

            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func selectFolder(path: String) async {
        guard path != selectedFolderPath else { return }
        selectedFolderPath = path
        selectedBoardID = nil
        board = nil
        await refreshBoards(selectFirst: true)
    }

    func loadBoard(id: String) async {
        guard let baseURL else { return }

        isLoading = true
        defer { isLoading = false }

        do {
            board = try await bridge.fetchBoard(baseURL: baseURL, boardID: id)
            selectedBoardID = id
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func restartAutoRefreshLoop() {
        refreshTask?.cancel()
        guard baseURL != nil else { return }

        refreshTask = Task { @MainActor [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: Self.refreshLoopIntervalNanoseconds)
                if Task.isCancelled { break }
                await self.refreshBoards(selectFirst: false)
            }
        }
    }
}
