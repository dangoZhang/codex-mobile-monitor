import SwiftUI

struct ContentView: View {
    @StateObject private var state = AppState()
    @AppStorage("bridge_url") private var bridgeURL = ""

    var body: some View {
        NavigationSplitView {
            sidebar
                .navigationSplitViewColumnWidth(min: 300, ideal: 340)
        } detail: {
            detail
        }
        .navigationSplitViewStyle(.balanced)
        .preferredColorScheme(.dark)
        .tint(CodexPalette.accent)
        .task {
            state.updateBridgeURL(bridgeURL)
            await state.bootstrap()
        }
        .task(id: bridgeURL) {
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(8))
                await state.refreshSessions(selectFirst: false)
            }
        }
        .onChange(of: bridgeURL) { _, newValue in
            state.updateBridgeURL(newValue)
            Task {
                await state.refreshSessions(selectFirst: true)
            }
        }
    }

    private var sidebar: some View {
        ZStack {
            CodexPalette.sidebar.ignoresSafeArea()

            VStack(alignment: .leading, spacing: 18) {
                sidebarHeader
                bridgePanel
                sessionList
            }
            .padding(18)
        }
    }

    private var sidebarHeader: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .center) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Codex")
                        .font(.system(size: 26, weight: .semibold))
                        .foregroundStyle(CodexPalette.foreground)
                    Text("Desktop Monitor")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(CodexPalette.muted)
                }
                Spacer()
                ConnectionBadge(
                    text: state.errorMessage == nil ? "Connected" : "Disconnected",
                    style: state.errorMessage == nil ? .connected : .error
                )
            }

            Text("Directly supervising real Codex Desktop threads from your Mac.")
                .font(.system(size: 13, weight: .regular))
                .foregroundStyle(CodexPalette.muted)
        }
    }

    private var bridgePanel: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Bridge")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(CodexPalette.muted)

            TextField("http://your-mac.local:8765", text: $bridgeURL)
                .disableAutocorrectionForURL()
                .autocorrectionDisabled()
                .font(.system(size: 14, weight: .regular, design: .monospaced))
                .foregroundStyle(CodexPalette.foreground)
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
                .background(CodexPalette.panel, in: RoundedRectangle(cornerRadius: 16))
                .overlay(
                    RoundedRectangle(cornerRadius: 16)
                        .stroke(CodexPalette.border, lineWidth: 1)
                )

            HStack(spacing: 10) {
                Button {
                    Task { await state.refreshSessions(selectFirst: true) }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .buttonStyle(CodexSecondaryButtonStyle())

                if state.isLoading {
                    ProgressView()
                        .controlSize(.small)
                        .tint(CodexPalette.accent)
                }
            }

            if let errorMessage = state.errorMessage {
                Text(errorMessage)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(CodexPalette.error)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(16)
        .background(CodexPalette.panel, in: RoundedRectangle(cornerRadius: 22))
        .overlay(
            RoundedRectangle(cornerRadius: 22)
                .stroke(CodexPalette.border, lineWidth: 1)
        )
    }

    private var sessionList: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 12) {
                ForEach(state.sessions) { session in
                    Button {
                        Task { await state.loadSession(id: session.id) }
                    } label: {
                        SessionRowView(session: session, selected: state.selectedSessionID == session.id)
                    }
                    .buttonStyle(.plain)
                }

                if state.sessions.isEmpty {
                    EmptySidebarStateView()
                }
            }
            .padding(.bottom, 16)
        }
        .scrollIndicators(.hidden)
    }

    private var detail: some View {
        ZStack {
            CodexPalette.canvas
                .overlay(alignment: .topTrailing) {
                    Circle()
                        .fill(CodexPalette.accent.opacity(0.12))
                        .frame(width: 360, height: 360)
                        .blur(radius: 80)
                        .offset(x: 120, y: -140)
                }
                .ignoresSafeArea()

            if state.selectedSessionID == nil {
                EmptyDetailStateView()
            } else {
                VStack(spacing: 0) {
                    detailHeader
                    Divider().overlay(CodexPalette.border)
                    detailMetadata
                    Divider().overlay(CodexPalette.border)
                    messagesView
                    Divider().overlay(CodexPalette.border)
                    composer
                }
            }
        }
    }

    private var detailHeader: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(state.title)
                        .font(.system(size: 28, weight: .semibold))
                        .foregroundStyle(CodexPalette.foreground)
                        .fixedSize(horizontal: false, vertical: true)

                    HStack(spacing: 8) {
                        DetailBadge(text: "Desktop", style: .neutral)
                        DetailBadge(text: state.isRunning ? "Running" : "Monitoring", style: state.isRunning ? .connected : .neutral)
                        DetailBadge(text: state.currentSource.uppercased(), style: .neutral)
                    }
                }

                Spacer()

                if let output = state.lastOutputLine, !output.isEmpty {
                    Text(output)
                        .font(.system(size: 12, weight: .regular, design: .monospaced))
                        .foregroundStyle(CodexPalette.muted)
                        .multilineTextAlignment(.trailing)
                        .lineLimit(4)
                        .frame(maxWidth: 300, alignment: .trailing)
                }
            }
        }
        .padding(.horizontal, 28)
        .padding(.vertical, 22)
    }

    private var detailMetadata: some View {
        VStack(alignment: .leading, spacing: 16) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    MetadataPill(title: "Origin", value: state.currentOriginator ?? "Codex Desktop")
                    MetadataPill(title: "Provider", value: state.currentModelProvider ?? "unknown")
                    MetadataPill(title: "CLI", value: state.currentCLIVersion ?? "unknown")
                    MetadataPill(title: "Data", value: state.currentDataSource)
                    MetadataPill(title: "Thread", value: state.selectedSessionID ?? "unknown")
                }
            }

            VStack(alignment: .leading, spacing: 10) {
                if let cwd = state.currentCWD, !cwd.isEmpty {
                    ProvenanceRow(label: "Workspace", value: cwd)
                }
                if let rolloutPath = state.currentRolloutPath, !rolloutPath.isEmpty {
                    ProvenanceRow(label: "Rollout", value: rolloutPath)
                }
            }
        }
        .padding(.horizontal, 28)
        .padding(.vertical, 18)
    }

    private var messagesView: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 18) {
                    if state.messages.isEmpty {
                        EmptyThreadStateView()
                    } else {
                        ForEach(state.messages) { message in
                            MessageRowView(message: message)
                                .id(message.id)
                        }
                    }
                }
                .padding(.horizontal, 28)
                .padding(.vertical, 24)
            }
            .scrollIndicators(.hidden)
            .onChange(of: state.messages.last?.id) { _, newValue in
                guard let newValue else { return }
                withAnimation(.easeOut(duration: 0.18)) {
                    proxy.scrollTo(newValue, anchor: .bottom)
                }
            }
        }
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(bridgeReplyHint)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(CodexPalette.muted)
                Spacer()
                if !state.bridgeReplyAvailable {
                    DetailBadge(text: "Read Only", style: .warning)
                }
            }

            HStack(alignment: .bottom, spacing: 12) {
                TextEditor(text: $state.draft)
                    .frame(minHeight: 74, maxHeight: 140)
                    .scrollContentBackground(.hidden)
                    .padding(12)
                    .background(CodexPalette.panel, in: RoundedRectangle(cornerRadius: 18))
                    .overlay(
                        RoundedRectangle(cornerRadius: 18)
                            .stroke(CodexPalette.border, lineWidth: 1)
                    )
                    .font(.system(size: 15, weight: .regular))
                    .foregroundStyle(CodexPalette.foreground)

                Button {
                    Task { await state.sendDraft() }
                } label: {
                    Image(systemName: "arrow.up")
                        .font(.system(size: 16, weight: .semibold))
                        .frame(width: 44, height: 44)
                }
                .buttonStyle(CodexPrimaryButtonStyle())
                .disabled(
                    state.isRunning ||
                    !state.bridgeReplyAvailable ||
                    state.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                )
            }
        }
        .padding(.horizontal, 28)
        .padding(.vertical, 18)
        .background(CodexPalette.sidebar.opacity(0.8))
    }

    private var bridgeReplyHint: String {
        if state.bridgeReplyAvailable {
            return "Replies resume the same desktop thread on your Mac, not a separate CLI-only session."
        }
        return "This view is supervising a real desktop thread. Reply is disabled for the current connection state."
    }
}

private extension View {
    @ViewBuilder
    func disableAutocorrectionForURL() -> some View {
        #if os(iOS)
        self.textInputAutocapitalization(.never)
        #else
        self
        #endif
    }
}

private struct SessionRowView: View {
    let session: SessionSummary
    let selected: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(session.title)
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(CodexPalette.foreground)
                        .lineLimit(2)

                    HStack(spacing: 8) {
                        DetailBadge(text: "Desktop", style: .neutral)
                        DetailBadge(text: session.running ? "Live" : "Idle", style: session.running ? .connected : .neutral)
                    }
                }

                Spacer()

                Circle()
                    .fill(session.running ? CodexPalette.success : CodexPalette.muted.opacity(0.35))
                    .frame(width: 8, height: 8)
                    .padding(.top, 4)
            }

            if let cwd = session.cwd, !cwd.isEmpty {
                Text(cwd)
                    .font(.system(size: 11, weight: .regular, design: .monospaced))
                    .foregroundStyle(CodexPalette.muted)
                    .lineLimit(1)
            }

            if !session.lastMessagePreview.isEmpty {
                Text(session.lastMessagePreview)
                    .font(.system(size: 12, weight: .regular))
                    .foregroundStyle(CodexPalette.muted)
                    .lineLimit(3)
            }

            HStack {
                Text(session.modelProvider?.uppercased() ?? session.source.uppercased())
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(CodexPalette.subtleText)
                Spacer()
                Text(Self.relative.string(for: session.updatedAt) ?? "")
                    .font(.system(size: 11, weight: .regular))
                    .foregroundStyle(CodexPalette.subtleText)
            }
        }
        .padding(16)
        .background(backgroundShape.fill(CodexPalette.panel))
        .overlay(
            backgroundShape
                .stroke(selected ? CodexPalette.accent : CodexPalette.border, lineWidth: 1)
        )
    }

    private var backgroundShape: RoundedRectangle {
        RoundedRectangle(cornerRadius: 20, style: .continuous)
    }

    private static let relative: RelativeDateTimeFormatter = {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .short
        return formatter
    }()
}

private struct MessageRowView: View {
    let message: ChatMessage

    var body: some View {
        Group {
            if message.kind == "tool_call" {
                toolRow
            } else if message.kind == "commentary" {
                commentaryRow
            } else {
                conversationRow
            }
        }
    }

    private var conversationRow: some View {
        VStack(alignment: message.role == "user" ? .trailing : .leading, spacing: 8) {
            rowLabel(title: labelText, tint: labelColor)

            Text(message.text)
                .font(.system(size: 16, weight: .regular))
                .foregroundStyle(CodexPalette.foreground)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: message.role == "user" ? .trailing : .leading)
        }
        .padding(18)
        .frame(maxWidth: 900, alignment: message.role == "user" ? .trailing : .leading)
        .frame(maxWidth: .infinity, alignment: message.role == "user" ? .trailing : .leading)
        .background(backgroundColor, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 22, style: .continuous)
                .stroke(borderColor, lineWidth: 1)
        )
    }

    private var commentaryRow: some View {
        VStack(alignment: .leading, spacing: 10) {
            rowLabel(title: "Commentary", tint: CodexPalette.warning)

            Text(message.text)
                .font(.system(size: 14, weight: .regular))
                .foregroundStyle(CodexPalette.mutedBright)
                .textSelection(.enabled)
        }
        .padding(16)
        .frame(maxWidth: 900, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(CodexPalette.panel.opacity(0.78), in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .stroke(CodexPalette.border, lineWidth: 1)
        )
    }

    private var toolRow: some View {
        VStack(alignment: .leading, spacing: 10) {
            rowLabel(title: message.toolName ?? "Tool", tint: CodexPalette.accent)

            Text(message.text)
                .font(.system(size: 13, weight: .regular, design: .monospaced))
                .foregroundStyle(CodexPalette.mutedBright)
                .textSelection(.enabled)
        }
        .padding(16)
        .frame(maxWidth: 900, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(CodexPalette.sidebar.opacity(0.72), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .stroke(CodexPalette.border, lineWidth: 1)
        )
    }

    private func rowLabel(title: String, tint: Color) -> some View {
        HStack(spacing: 8) {
            Circle()
                .fill(tint)
                .frame(width: 8, height: 8)
            Text(title)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(tint)
            Spacer()
            Text(Self.time.string(from: message.createdAt))
                .font(.system(size: 11, weight: .regular))
                .foregroundStyle(CodexPalette.subtleText)
        }
    }

    private var labelText: String {
        switch message.role {
        case "user":
            return "You"
        case "assistant":
            return "Codex"
        default:
            return "System"
        }
    }

    private var labelColor: Color {
        switch message.role {
        case "user":
            return CodexPalette.accent
        case "assistant":
            return CodexPalette.foreground
        default:
            return CodexPalette.warning
        }
    }

    private var backgroundColor: Color {
        switch message.role {
        case "user":
            return CodexPalette.accent.opacity(0.16)
        case "assistant":
            return CodexPalette.panel
        default:
            return CodexPalette.panel
        }
    }

    private var borderColor: Color {
        switch message.role {
        case "user":
            return CodexPalette.accent.opacity(0.5)
        default:
            return CodexPalette.border
        }
    }

    private static let time: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm"
        return formatter
    }()
}

private struct MetadataPill: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title.uppercased())
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(CodexPalette.subtleText)
            Text(value)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(CodexPalette.foreground)
                .lineLimit(1)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(CodexPalette.panel, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(CodexPalette.border, lineWidth: 1)
        )
    }
}

private struct ProvenanceRow: View {
    let label: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(CodexPalette.subtleText)
            Text(value)
                .font(.system(size: 12, weight: .regular, design: .monospaced))
                .foregroundStyle(CodexPalette.mutedBright)
                .textSelection(.enabled)
        }
    }
}

private struct ConnectionBadge: View {
    let text: String
    let style: DetailBadge.Style

    var body: some View {
        DetailBadge(text: text, style: style)
    }
}

private struct DetailBadge: View {
    enum Style {
        case neutral
        case connected
        case warning
        case error
    }

    let text: String
    let style: Style

    var body: some View {
        Text(text)
            .font(.system(size: 11, weight: .semibold))
            .foregroundStyle(foreground)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(background, in: Capsule())
    }

    private var background: Color {
        switch style {
        case .neutral:
            return CodexPalette.panel
        case .connected:
            return CodexPalette.success.opacity(0.18)
        case .warning:
            return CodexPalette.warning.opacity(0.18)
        case .error:
            return CodexPalette.error.opacity(0.18)
        }
    }

    private var foreground: Color {
        switch style {
        case .neutral:
            return CodexPalette.mutedBright
        case .connected:
            return CodexPalette.success
        case .warning:
            return CodexPalette.warning
        case .error:
            return CodexPalette.error
        }
    }
}

private struct EmptySidebarStateView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("No desktop threads")
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(CodexPalette.foreground)
            Text("Check the bridge URL and refresh. The list only shows real Codex Desktop threads from your Mac.")
                .font(.system(size: 13, weight: .regular))
                .foregroundStyle(CodexPalette.muted)
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(CodexPalette.panel, in: RoundedRectangle(cornerRadius: 20))
        .overlay(
            RoundedRectangle(cornerRadius: 20)
                .stroke(CodexPalette.border, lineWidth: 1)
        )
    }
}

private struct EmptyDetailStateView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Select a desktop thread")
                .font(.system(size: 28, weight: .semibold))
                .foregroundStyle(CodexPalette.foreground)
            Text("This client reads Codex Desktop thread metadata from SQLite and live content from rollout logs on your Mac.")
                .font(.system(size: 15, weight: .regular))
                .foregroundStyle(CodexPalette.muted)
                .frame(maxWidth: 560, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
        .padding(40)
    }
}

private struct EmptyThreadStateView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("No messages loaded")
                .font(.system(size: 20, weight: .semibold))
                .foregroundStyle(CodexPalette.foreground)
            Text("This thread exists in Codex Desktop, but the current rollout has no parsed user or assistant messages yet.")
                .font(.system(size: 14, weight: .regular))
                .foregroundStyle(CodexPalette.muted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(20)
        .background(CodexPalette.panel, in: RoundedRectangle(cornerRadius: 22))
        .overlay(
            RoundedRectangle(cornerRadius: 22)
                .stroke(CodexPalette.border, lineWidth: 1)
        )
    }
}

private struct CodexSecondaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .semibold))
            .foregroundStyle(CodexPalette.foreground)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(CodexPalette.panel, in: RoundedRectangle(cornerRadius: 14))
            .overlay(
                RoundedRectangle(cornerRadius: 14)
                    .stroke(CodexPalette.border, lineWidth: 1)
            )
            .opacity(configuration.isPressed ? 0.78 : 1)
    }
}

private struct CodexPrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(CodexPalette.foreground)
            .background(
                Circle()
                    .fill(CodexPalette.accent.opacity(configuration.isPressed ? 0.75 : 1))
            )
    }
}

private enum CodexPalette {
    static let canvas = Color(hex: 0x111111)
    static let sidebar = Color(hex: 0x131313)
    static let panel = Color(hex: 0x171717)
    static let border = Color(hex: 0x232323)
    static let foreground = Color(hex: 0xFCFCFC)
    static let mutedBright = Color(hex: 0xC6C6C6)
    static let muted = Color(hex: 0x8F8F8F)
    static let subtleText = Color(hex: 0x6E6E6E)
    static let accent = Color(hex: 0x0169CC)
    static let success = Color(hex: 0x30D158)
    static let warning = Color(hex: 0xF4C430)
    static let error = Color(hex: 0xFF6B6B)
}

private extension Color {
    init(hex: UInt32, opacity: Double = 1) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xff) / 255,
            green: Double((hex >> 8) & 0xff) / 255,
            blue: Double(hex & 0xff) / 255,
            opacity: opacity
        )
    }
}
