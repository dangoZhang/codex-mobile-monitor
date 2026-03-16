import Foundation
import SwiftUI

@MainActor
final class AppState: ObservableObject {
    @Published var sessions: [SessionSummary] = []
    @Published var selectedSessionID: String?
    @Published var messages: [ChatMessage] = []
    @Published var draft: String = ""
    @Published var statusText: String = "Monitoring"
    @Published var isLoading = false
    @Published var isRunning = false
    @Published var lastOutputLine: String?
    @Published var errorMessage: String?
    @Published var title: String = "Desktop Threads"
    @Published var currentCWD: String?
    @Published var currentSource: String = "bridge"
    @Published var currentOriginator: String?
    @Published var isImportedSession = false
    @Published var currentDataSource: String = "bridge"
    @Published var currentRolloutPath: String?
    @Published var currentModelProvider: String?
    @Published var currentCLIVersion: String?
    @Published var bridgeReplyAvailable = false

    private let bridge = BridgeClient()
    private var streamTask: Task<Void, Never>?
    private var cursors: [String: Int] = [:]
    private(set) var baseURL: URL?

    init() {
        if let raw = UserDefaults.standard.string(forKey: "bridge_url") {
            self.baseURL = Self.makeBaseURL(from: raw)
        }
    }

    func updateBridgeURL(_ raw: String) {
        UserDefaults.standard.set(raw, forKey: "bridge_url")
        baseURL = Self.makeBaseURL(from: raw)
    }

    func bootstrap() async {
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
            }
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func createSession() async {
        guard let baseURL else {
            errorMessage = "先填写 Mac Bridge 地址"
            return
        }
        isLoading = true
        defer { isLoading = false }
        do {
            let detail = try await bridge.createSession(baseURL: baseURL)
            let summary = SessionSummary(
                id: detail.id,
                title: detail.title,
                createdAt: detail.createdAt,
                updatedAt: detail.updatedAt,
                threadID: detail.threadID,
                cwd: detail.cwd,
                source: detail.source,
                originator: detail.originator,
                imported: detail.imported,
                desktopThread: detail.desktopThread,
                dataSource: detail.dataSource,
                rolloutPath: detail.rolloutPath,
                modelProvider: detail.modelProvider,
                cliVersion: detail.cliVersion,
                bridgeReplyAvailable: detail.bridgeReplyAvailable,
                running: detail.running,
                messageCount: detail.messageCount,
                lastMessagePreview: detail.lastMessagePreview
            )
            sessions.insert(summary, at: 0)
            apply(detail: detail)
            connectStream(for: detail.id, after: detail.lastEventSequence)
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
        guard !text.isEmpty else { return }
        guard let sessionID = selectedSessionID else {
            errorMessage = "先选择一个桌面线程"
            return
        }
        guard let baseURL else { return }
        guard bridgeReplyAvailable else {
            errorMessage = "当前线程只允许监督，不允许回写"
            return
        }

        draft = ""
        isRunning = true
        statusText = "Running"
        do {
            try await bridge.sendMessage(baseURL: baseURL, sessionID: sessionID, text: text)
            errorMessage = nil
        } catch {
            draft = text
            isRunning = false
            statusText = "Failed"
            errorMessage = error.localizedDescription
        }
    }

    private func apply(detail: SessionDetail) {
        selectedSessionID = detail.id
        title = detail.title
        currentCWD = detail.cwd
        currentSource = detail.source
        currentOriginator = detail.originator
        isImportedSession = detail.imported
        currentDataSource = detail.dataSource
        currentRolloutPath = detail.rolloutPath
        currentModelProvider = detail.modelProvider
        currentCLIVersion = detail.cliVersion
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
            source: detail.source,
            originator: detail.originator,
            imported: detail.imported,
            desktopThread: detail.desktopThread,
            dataSource: detail.dataSource,
            rolloutPath: detail.rolloutPath,
            modelProvider: detail.modelProvider,
            cliVersion: detail.cliVersion,
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

    private static func makeBaseURL(from raw: String) -> URL? {
        let cleaned = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty else { return nil }
        return URL(string: cleaned)
    }
}
