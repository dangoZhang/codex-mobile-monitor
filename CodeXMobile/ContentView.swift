import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @StateObject private var monitorState = AppState()
    @StateObject private var boardState = BoardState()
    @AppStorage("bridge_url") private var bridgeURL = ""

    var body: some View {
        ResponsiveCodexShellView(
            monitorState: monitorState,
            boardState: boardState,
            bridgeURL: $bridgeURL
        )
    }
}

private enum ShellRoute: Hashable {
    case session(String)
    case boards
    case settings
}

private struct SessionProjectBucket: Identifiable, Hashable {
    let id: String
    let name: String
    let path: String?
    let updatedAt: Date?
    let sessions: [SessionSummary]
    let projectOnly: Bool
}

private struct ResponsiveCodexShellView: View {
    @ObservedObject var monitorState: AppState
    @ObservedObject var boardState: BoardState
    @Binding var bridgeURL: String

    @State private var selection: ShellRoute?
    @State private var isCompactSidebarPresented = false
    @State private var splitVisibility: NavigationSplitViewVisibility = .all
    @State private var hasResolvedInitialSelection = false
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass

    var body: some View {
        Group {
            if horizontalSizeClass == .compact {
                compactShell
            } else {
                regularShell
            }
        }
        .preferredColorScheme(.light)
        .tint(CodexPalette.accent)
        .task {
            monitorState.updateBridgeURL(bridgeURL)
            boardState.updateBridgeURL(bridgeURL)
            await monitorState.bootstrap()
            await boardState.bootstrap()
            syncSelectionIfNeeded(forceDefault: true)
        }
        .task(id: bridgeURL) {
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(8))
                await monitorState.refreshSessions(selectFirst: false)
                await boardState.refreshBoards(selectFirst: false)
                syncSelectionIfNeeded(forceDefault: true)
            }
        }
        .onChange(of: bridgeURL) { _, newValue in
            monitorState.updateBridgeURL(newValue)
            boardState.updateBridgeURL(newValue)
            Task {
                await monitorState.refreshSessions(selectFirst: true)
                await boardState.refreshBoards(selectFirst: true)
                syncSelectionIfNeeded(forceDefault: true)
            }
        }
        .onChange(of: monitorState.selectedSessionID) { _, _ in
            syncSelectionIfNeeded(forceDefault: true)
        }
        .onChange(of: monitorState.sessions.map(\.id)) { _, _ in
            syncSelectionIfNeeded(forceDefault: true)
        }
    }

    private var regularShell: some View {
        NavigationSplitView(columnVisibility: $splitVisibility) {
            ShellSidebarView(
                selection: selection,
                monitorState: monitorState,
                boardState: boardState,
                onNewThread: createNewThread,
                onSelectRoute: selectRoute,
                onSelectSession: selectSession
            )
            .navigationSplitViewColumnWidth(min: 290, ideal: 360, max: 420)
        } detail: {
            shellDetail
        }
        .navigationSplitViewStyle(.balanced)
        .onAppear {
            splitVisibility = .all
        }
    }

    private var compactShell: some View {
        NavigationStack {
            shellDetail
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) {
                        Button {
                            isCompactSidebarPresented = true
                        } label: {
                            Image(systemName: "sidebar.left")
                        }
                    }

                    ToolbarItem(placement: .principal) {
                        Text(currentTitle)
                            .font(.system(size: 16, weight: .semibold))
                            .lineLimit(1)
                    }
                }
        }
        .sheet(isPresented: $isCompactSidebarPresented) {
            NavigationStack {
                ShellSidebarView(
                    selection: selection,
                    monitorState: monitorState,
                    boardState: boardState,
                    onNewThread: createNewThread,
                    onSelectRoute: { route in
                        selectRoute(route)
                        isCompactSidebarPresented = false
                    },
                    onSelectSession: { sessionID in
                        selectSession(sessionID)
                        isCompactSidebarPresented = false
                    }
                )
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("关闭") {
                            isCompactSidebarPresented = false
                        }
                    }
                }
            }
            .presentationDetents([.large])
        }
    }

    @ViewBuilder
    private var shellDetail: some View {
        switch resolvedSelection {
        case .session:
            MonitorWorkspaceDetailView(state: monitorState)
        case .boards:
            BoardsWorkspaceDetailView(state: boardState)
        case .settings:
            SettingsWorkspaceDetailView(
                monitorState: monitorState,
                boardState: boardState,
                bridgeURL: $bridgeURL
            )
        }
    }

    private var resolvedSelection: ShellRoute {
        if let selection {
            return selection
        }
        if let sessionID = monitorState.selectedSessionID {
            return .session(sessionID)
        }
        return .settings
    }

    private var currentTitle: String {
        switch resolvedSelection {
        case .session:
            return monitorState.title
        case .boards:
            return boardState.board?.title ?? "看板"
        case .settings:
            return "设置"
        }
    }

    private func syncSelectionIfNeeded(forceDefault: Bool) {
        if case .session(let sessionID) = selection,
           !monitorState.sessions.contains(where: { $0.id == sessionID }) {
            selection = nil
        }

        if !hasResolvedInitialSelection {
            if let sessionID = monitorState.selectedSessionID ?? monitorState.sessions.first?.id {
                selection = .session(sessionID)
                hasResolvedInitialSelection = true
                return
            }

            if forceDefault {
                selection = .settings
                hasResolvedInitialSelection = true
                return
            }
        }

        if selection == nil, let sessionID = monitorState.selectedSessionID ?? monitorState.sessions.first?.id {
            selection = .session(sessionID)
        } else if selection == nil, forceDefault {
            selection = .settings
        }
    }

    private func createNewThread() {
        Task {
            await monitorState.createSession()
            if let sessionID = monitorState.selectedSessionID {
                selection = .session(sessionID)
                hasResolvedInitialSelection = true
            }
            isCompactSidebarPresented = false
        }
    }

    private func selectRoute(_ route: ShellRoute) {
        selection = route
        hasResolvedInitialSelection = true
        switch route {
        case .boards:
            Task {
                await boardState.refreshBoards(selectFirst: boardState.board == nil)
            }
        case .settings:
            monitorState.updateBridgeURL(bridgeURL)
            boardState.updateBridgeURL(bridgeURL)
        default:
            break
        }
    }

    private func selectSession(_ sessionID: String) {
        selection = .session(sessionID)
        hasResolvedInitialSelection = true
        Task {
            await monitorState.loadSession(id: sessionID)
        }
    }
}

private struct ShellSidebarView: View {
    let selection: ShellRoute?
    @ObservedObject var monitorState: AppState
    @ObservedObject var boardState: BoardState
    let onNewThread: () -> Void
    let onSelectRoute: (ShellRoute) -> Void
    let onSelectSession: (String) -> Void

    private var recentSessions: [SessionSummary] {
        Array(monitorState.sessions.prefix(6))
    }

    private var projectBuckets: [SessionProjectBucket] {
        bucketizeSessions(monitorState.sessions, knownProjects: monitorState.projects)
    }

    var body: some View {
        ZStack {
            CodexPalette.sidebar.ignoresSafeArea()

            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    header
                    quickActions

                    if !recentSessions.isEmpty {
                        sectionHeader("最近")
                        VStack(spacing: 4) {
                            ForEach(recentSessions) { session in
                                SidebarThreadRowView(
                                    session: session,
                                    selected: selection == .session(session.id),
                                    compact: false,
                                    onTap: { onSelectSession(session.id) }
                                )
                            }
                        }
                    }

                    sectionHeader("线程", showsControls: true)

                    if projectBuckets.isEmpty {
                        EmptySidebarStateView()
                    } else {
                        VStack(alignment: .leading, spacing: 16) {
                            ForEach(projectBuckets) { bucket in
                                SidebarProjectBucketView(
                                    bucket: bucket,
                                    selection: selection,
                                    onSelectSession: onSelectSession
                                )
                            }
                        }
                    }
                }
                .padding(.horizontal, 18)
                .padding(.top, 18)
                .padding(.bottom, 90)
            }
        }
        .safeAreaInset(edge: .bottom) {
            VStack(spacing: 0) {
                Divider().overlay(CodexPalette.border)
                Button {
                    onSelectRoute(.settings)
                } label: {
                    HStack(spacing: 10) {
                        Image(systemName: "gearshape")
                        Text("设置")
                            .font(.system(size: 15, weight: .medium))
                        Spacer()
                    }
                    .foregroundStyle(selection == .settings ? CodexPalette.foreground : CodexPalette.mutedBright)
                    .padding(.horizontal, 18)
                    .padding(.vertical, 16)
                }
                .buttonStyle(.plain)
                .background(CodexPalette.sidebar)
            }
        }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 12) {
            RoundedRectangle(cornerRadius: 9)
                .fill(CodexPalette.panel)
                .frame(width: 34, height: 34)
                .overlay(
                    Image(systemName: "rectangle.inset.filled.leftthird")
                        .font(.system(size: 16, weight: .medium))
                        .foregroundStyle(CodexPalette.foreground)
                )

            VStack(alignment: .leading, spacing: 2) {
                Text("Codex")
                    .font(.system(size: 19, weight: .semibold))
                    .foregroundStyle(CodexPalette.foreground)
                Text("桌面线程")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(CodexPalette.subtleText)
            }

            Spacer()
            ConnectionBadge(
                text: monitorState.errorMessage == nil ? "已连接" : "未连接",
                style: monitorState.errorMessage == nil ? .connected : .error
            )
        }
    }

    private var quickActions: some View {
        VStack(alignment: .leading, spacing: 4) {
            SidebarActionButton(
                icon: "square.and.pencil",
                title: "新线程",
                selected: false,
                action: onNewThread
            )
            SidebarActionButton(
                icon: "rectangle.grid.1x2",
                title: "看板",
                selected: selection == .boards,
                action: { onSelectRoute(.boards) }
            )
        }
    }

    private func sectionHeader(_ title: String, showsControls: Bool = false) -> some View {
        HStack(spacing: 10) {
            Text(title)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(CodexPalette.subtleText)
                .textCase(.uppercase)
            Spacer()
            if showsControls {
                Image(systemName: "folder.badge.plus")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(CodexPalette.subtleText)
                Image(systemName: "line.3.horizontal.decrease")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(CodexPalette.subtleText)
            }
        }
    }
}

private struct MonitorWorkspaceDetailView: View {
    @ObservedObject var state: AppState
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    @State private var isAttachmentDialogPresented = false
    @State private var isFileImporterPresented = false
    @State private var isPhotoPickerPresented = false
    @State private var selectedPhotoItems: [PhotosPickerItem] = []

    var body: some View {
        ZStack {
            CodexPalette.canvas.ignoresSafeArea()

            if state.selectedSessionID == nil {
                EmptyDetailStateView()
            } else {
                messagesView
            }
        }
        .safeAreaInset(edge: .bottom) {
            if state.selectedSessionID != nil {
                composerDock
            }
        }
        .confirmationDialog("添加附件", isPresented: $isAttachmentDialogPresented, titleVisibility: .visible) {
            Button("照片或图片") {
                isPhotoPickerPresented = true
            }
            Button("文件") {
                isFileImporterPresented = true
            }
            Button("取消", role: .cancel) {}
        }
        .photosPicker(
            isPresented: $isPhotoPickerPresented,
            selection: $selectedPhotoItems,
            maxSelectionCount: 10,
            matching: .images
        )
        .fileImporter(
            isPresented: $isFileImporterPresented,
            allowedContentTypes: [.item],
            allowsMultipleSelection: true
        ) { result in
            handleFileImport(result)
        }
        .onChange(of: selectedPhotoItems) { _, newItems in
            guard !newItems.isEmpty else { return }
            Task {
                await importSelectedPhotos(newItems)
                selectedPhotoItems = []
            }
        }
        .navigationTitle(state.title)
        .navigationBarTitleDisplayMode(.inline)
    }

    private var messagesView: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 24) {
                    if state.messages.isEmpty {
                        EmptyThreadStateView()
                            .padding(.top, 40)
                    } else {
                        ForEach(state.messages) { message in
                            MessageRowView(message: message)
                                .id(message.id)
                        }
                    }
                }
                .padding(.horizontal, contentHorizontalPadding)
                .padding(.top, horizontalSizeClass == .compact ? 14 : 18)
                .padding(.bottom, 180)
                .frame(maxWidth: 980, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .center)
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

    private var composerDock: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: state.isRunning ? "terminal" : "sparkles.rectangle.stack")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(state.isRunning ? CodexPalette.success : CodexPalette.muted)
                Text(bridgeReplyHint)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(CodexPalette.mutedBright)
                Spacer()
            }

            if !state.draftAttachments.isEmpty {
                attachmentTray
            }

            HStack(alignment: .bottom, spacing: 12) {
                ZStack(alignment: .topLeading) {
                    TextEditor(text: $state.draft)
                        .frame(minHeight: 92, maxHeight: 140)
                        .scrollContentBackground(.hidden)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 10)
                        .background(CodexPalette.canvas, in: RoundedRectangle(cornerRadius: 20))
                        .overlay(
                            RoundedRectangle(cornerRadius: 20)
                                .stroke(CodexPalette.border, lineWidth: 1)
                        )
                        .font(.system(size: 16, weight: .regular))
                        .foregroundStyle(CodexPalette.foreground)
                        .disabled(!state.bridgeReplyAvailable)

                    if state.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        Text(state.bridgeReplyAvailable ? "继续在这个桌面线程里输入..." : "当前线程仅支持监督查看")
                            .font(.system(size: 16, weight: .regular))
                            .foregroundStyle(CodexPalette.subtleText)
                            .padding(.horizontal, 18)
                            .padding(.vertical, 18)
                            .allowsHitTesting(false)
                    }
                }

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
                    !canSendDraft
                )
            }

            HStack(spacing: 14) {
                Button {
                    isAttachmentDialogPresented = true
                } label: {
                    Image(systemName: "plus")
                        .font(.system(size: 18, weight: .regular))
                        .foregroundStyle(CodexPalette.muted)
                }
                .buttonStyle(.plain)
                .disabled(!state.bridgeReplyAvailable || state.isRunning)

                Menu {
                    ForEach(ComposerModelOption.allCases) { option in
                        Button {
                            state.updateSelectedModel(option)
                        } label: {
                            HStack {
                                Text(option.title)
                                if state.selectedModel == option {
                                    Spacer()
                                    Image(systemName: "checkmark")
                                }
                            }
                        }
                    }
                } label: {
                    ComposerMenuLabel(
                        title: modelSelectionTitle,
                        systemImage: "chevron.down",
                        tint: CodexPalette.mutedBright
                    )
                }
                .disabled(!state.bridgeReplyAvailable || state.isRunning)

                Menu {
                    ForEach(ComposerAccessMode.allCases) { mode in
                        Button {
                            state.updateSelectedAccessMode(mode)
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(mode.title)
                                    Text(mode.subtitle)
                                        .font(.system(size: 11, weight: .regular))
                                }
                                if state.selectedAccessMode == mode {
                                    Spacer()
                                    Image(systemName: "checkmark")
                                }
                            }
                        }
                    }
                } label: {
                    ComposerMenuLabel(
                        title: state.selectedAccessMode.title,
                        systemImage: "shield",
                        tint: accessModeTint
                    )
                }
                .disabled(!state.bridgeReplyAvailable || state.isRunning)

                Spacer()

                if !state.draftAttachments.isEmpty {
                    Text("\(state.draftAttachments.count) 个附件")
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(CodexPalette.subtleText)
                }
            }
        }
        .padding(16)
        .frame(maxWidth: 980)
        .background(CodexPalette.panel, in: RoundedRectangle(cornerRadius: 24))
        .overlay(
            RoundedRectangle(cornerRadius: 24)
                .stroke(CodexPalette.border, lineWidth: 1)
        )
        .padding(.horizontal, 16)
        .padding(.top, 8)
        .padding(.bottom, 12)
        .frame(maxWidth: .infinity)
        .background(CodexPalette.canvas.opacity(0.96))
    }

    private var bridgeReplyHint: String {
        if state.isRunning {
            return "正在监督这条桌面线程的运行状态"
        }
        if state.bridgeReplyAvailable {
            return "回复会继续写入同一个桌面线程，可切换模型、权限并附加图片或文件"
        }
        return "当前线程只支持监督，不允许从移动端回写"
    }

    private var contentHorizontalPadding: CGFloat {
        horizontalSizeClass == .compact ? 18 : 28
    }

    private var canSendDraft: Bool {
        !state.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !state.draftAttachments.isEmpty
    }

    private var modelSelectionTitle: String {
        if state.selectedModel == .automatic {
            return state.currentModelProvider?.uppercased() ?? ComposerModelOption.automatic.title
        }
        return state.selectedModel.title
    }

    private var accessModeTint: Color {
        switch state.selectedAccessMode {
        case .readOnly:
            return CodexPalette.muted
        case .workspaceWrite:
            return CodexPalette.warning
        case .dangerFullAccess:
            return CodexPalette.error
        }
    }

    private var attachmentTray: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 10) {
                ForEach(state.draftAttachments) { attachment in
                    DraftAttachmentChip(attachment: attachment) {
                        state.removeAttachment(id: attachment.id)
                    }
                }
            }
        }
    }

    private func handleFileImport(_ result: Result<[URL], Error>) {
        do {
            let urls = try result.get()
            try state.importAttachments(from: urls)
            state.errorMessage = nil
        } catch {
            state.errorMessage = error.localizedDescription
        }
    }

    private func importSelectedPhotos(_ items: [PhotosPickerItem]) async {
        for (index, item) in items.enumerated() {
            do {
                guard let data = try await item.loadTransferable(type: Data.self) else {
                    continue
                }
                let contentType = item.supportedContentTypes.first
                let mimeType = contentType?.preferredMIMEType ?? "image/jpeg"
                let fileExtension = contentType?.preferredFilenameExtension ?? "jpg"
                try state.addAttachment(
                    data: data,
                    suggestedName: "photo-\(index + 1).\(fileExtension)",
                    contentType: mimeType,
                    isImage: true
                )
                state.errorMessage = nil
            } catch {
                state.errorMessage = error.localizedDescription
            }
        }
    }

    private static let time: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "M月d日 EEEE"
        formatter.locale = Locale(identifier: "zh_CN")
        return formatter
    }()
}

private struct BoardsWorkspaceDetailView: View {
    @ObservedObject var state: BoardState

    var body: some View {
        ZStack {
            CodexPalette.canvas.ignoresSafeArea()

            if state.boards.isEmpty, !state.isLoading {
                EmptyBoardDetailStateView()
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 24) {
                        header
                        folderSelector
                        boardSelector

                        if let board = state.board {
                            BoardHeroView(board: board)
                            BoardRepoStripView(board: board)
                            BoardColumnsView(columns: board.columns)
                            BoardThreadsSectionView(threads: board.threads)
                        } else if state.isLoading {
                            ProgressView()
                                .tint(CodexPalette.accent)
                        } else {
                            EmptyBoardDetailStateView()
                        }
                    }
                    .padding(.horizontal, 28)
                    .padding(.vertical, 24)
                }
                .scrollIndicators(.hidden)
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("项目看板")
                .font(.system(size: 30, weight: .semibold))
                .foregroundStyle(CodexPalette.foreground)
            Text("先切换文件夹，再看这个文件夹下的协作看板项目。")
                .font(.system(size: 14, weight: .regular))
                .foregroundStyle(CodexPalette.muted)
        }
    }

    private var folderSelector: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("文件夹")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(CodexPalette.muted)

            if state.folders.isEmpty {
                Text("当前没有发现可切换的看板文件夹")
                    .font(.system(size: 13, weight: .regular))
                    .foregroundStyle(CodexPalette.subtleText)
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 10) {
                        ForEach(state.folders) { folder in
                            Button {
                                Task { await state.selectFolder(path: folder.path) }
                            } label: {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(folder.name)
                                        .font(.system(size: 14, weight: .semibold))
                                        .lineLimit(1)
                                    Text(folder.path)
                                        .font(.system(size: 11, weight: .regular, design: .monospaced))
                                        .lineLimit(1)
                                    Text("\(folder.boardCount) 个看板")
                                        .font(.system(size: 11, weight: .medium))
                                        .foregroundStyle(CodexPalette.subtleText)
                                }
                                .foregroundStyle(state.selectedFolderPath == folder.path ? CodexPalette.foreground : CodexPalette.mutedBright)
                                .padding(.horizontal, 14)
                                .padding(.vertical, 12)
                                .frame(width: 280, alignment: .leading)
                                .background(
                                    RoundedRectangle(cornerRadius: 18)
                                        .fill(state.selectedFolderPath == folder.path ? CodexPalette.panel : CodexPalette.sidebar.opacity(0.72))
                                )
                                .overlay(
                                    RoundedRectangle(cornerRadius: 18)
                                        .stroke(state.selectedFolderPath == folder.path ? CodexPalette.accent : CodexPalette.border, lineWidth: 1)
                                )
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.vertical, 4)
                }
            }
        }
    }

    private var boardSelector: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 14) {
                ForEach(state.boards) { board in
                    Button {
                        Task { await state.loadBoard(id: board.id) }
                    } label: {
                        BoardSummaryRowView(board: board, selected: state.selectedBoardID == board.id)
                            .frame(width: 320)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.vertical, 4)
        }
    }
}

private struct SettingsWorkspaceDetailView: View {
    @ObservedObject var monitorState: AppState
    @ObservedObject var boardState: BoardState
    @Binding var bridgeURL: String

    var body: some View {
        ZStack {
            CodexPalette.canvas.ignoresSafeArea()

            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("设置")
                            .font(.system(size: 30, weight: .semibold))
                            .foregroundStyle(CodexPalette.foreground)
                        Text("这里集中放 bridge 地址、同步状态和手动刷新，不占用主线程页面。")
                            .font(.system(size: 14, weight: .regular))
                            .foregroundStyle(CodexPalette.muted)
                    }

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
                                Task {
                                    await monitorState.refreshSessions(selectFirst: true)
                                    await boardState.refreshBoards(selectFirst: true)
                                }
                            } label: {
                                Label("全部刷新", systemImage: "arrow.clockwise")
                            }
                            .buttonStyle(CodexSecondaryButtonStyle())

                            DetailBadge(text: "\(monitorState.sessions.count) 线程", style: .neutral)
                            DetailBadge(text: "\(boardState.boards.count) 看板", style: .neutral)
                        }

                        if let error = monitorState.errorMessage ?? boardState.errorMessage {
                            Text(error)
                                .font(.system(size: 12, weight: .medium))
                                .foregroundStyle(CodexPalette.error)
                        }
                    }
                    .padding(18)
                    .background(CodexPalette.panel, in: RoundedRectangle(cornerRadius: 22))
                    .overlay(
                        RoundedRectangle(cornerRadius: 22)
                            .stroke(CodexPalette.border, lineWidth: 1)
                    )
                }
                .padding(.horizontal, 28)
                .padding(.vertical, 24)
            }
        }
    }
}

private struct PlaceholderWorkspaceDetailView: View {
    let title: String
    let message: String

    var body: some View {
        ZStack {
            CodexPalette.canvas.ignoresSafeArea()

            VStack(alignment: .leading, spacing: 12) {
                Text(title)
                    .font(.system(size: 30, weight: .semibold))
                    .foregroundStyle(CodexPalette.foreground)
                Text(message)
                    .font(.system(size: 15, weight: .regular))
                    .foregroundStyle(CodexPalette.muted)
                    .frame(maxWidth: 560, alignment: .leading)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
            .padding(40)
        }
    }
}

private struct SidebarActionButton: View {
    let icon: String
    let title: String
    let selected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .font(.system(size: 15, weight: .medium))
                Text(title)
                    .font(.system(size: 16, weight: .medium))
                Spacer()
            }
            .foregroundStyle(selected ? CodexPalette.foreground : CodexPalette.mutedBright)
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: 12)
                    .fill(selected ? CodexPalette.panel : .clear)
            )
        }
        .buttonStyle(.plain)
    }
}

private struct SidebarProjectBucketView: View {
    let bucket: SessionProjectBucket
    let selection: ShellRoute?
    let onSelectSession: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 8) {
                Image(systemName: "folder")
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(CodexPalette.muted)
                Text(bucket.name)
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(CodexPalette.foreground)
                Spacer()
                Text("\(bucket.sessions.count)")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(CodexPalette.subtleText)
            }

            if let path = bucket.path, !path.isEmpty {
                Text(path)
                    .font(.system(size: 11, weight: .regular, design: .monospaced))
                    .foregroundStyle(CodexPalette.subtleText)
                    .lineLimit(1)
            }

            if bucket.sessions.isEmpty {
                Text("无线程")
                    .font(.system(size: 14, weight: .regular))
                    .foregroundStyle(CodexPalette.subtleText)
                    .padding(.leading, 12)
                    .padding(.vertical, 6)
                if bucket.projectOnly {
                    Text("来自 Codex 最近项目历史")
                        .font(.system(size: 11, weight: .regular))
                        .foregroundStyle(CodexPalette.subtleText)
                        .padding(.leading, 12)
                }
            } else {
                VStack(spacing: 2) {
                    ForEach(bucket.sessions) { session in
                        SidebarThreadRowView(
                            session: session,
                            selected: selection == .session(session.id),
                            compact: true,
                            onTap: { onSelectSession(session.id) }
                        )
                    }
                }
                .padding(.leading, 10)
            }
        }
    }
}

private struct SidebarThreadRowView: View {
    let session: SessionSummary
    let selected: Bool
    let compact: Bool
    let onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            HStack(alignment: .center, spacing: 10) {
                Circle()
                    .strokeBorder(CodexPalette.muted.opacity(0.65), lineWidth: 1.3)
                    .background(
                        Circle()
                            .fill(session.running ? CodexPalette.success : .clear)
                            .padding(3)
                    )
                    .frame(width: 16, height: 16)

                Text(session.title)
                    .font(.system(size: compact ? 14 : 15, weight: selected ? .semibold : .medium))
                    .foregroundStyle(CodexPalette.foreground)
                    .lineLimit(1)

                Spacer(minLength: 8)

                Text(Self.relative.string(for: session.updatedAt) ?? "")
                    .font(.system(size: 12, weight: .regular))
                    .foregroundStyle(CodexPalette.subtleText)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, compact ? 8 : 10)
            .background(
                RoundedRectangle(cornerRadius: 12)
                    .fill(selected ? CodexPalette.panel : Color.clear)
            )
        }
        .buttonStyle(.plain)
    }

    private static let relative: RelativeDateTimeFormatter = {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .short
        formatter.locale = Locale(identifier: "zh_CN")
        return formatter
    }()
}

private func bucketizeSessions(_ sessions: [SessionSummary], knownProjects: [ProjectSummary]) -> [SessionProjectBucket] {
    var grouped: [String: [SessionSummary]] = [:]
    var names: [String: String] = [:]
    var paths: [String: String?] = [:]
    var updatedAt: [String: Date] = [:]

    for project in knownProjects {
        grouped[project.id, default: []] = grouped[project.id, default: []]
        names[project.id] = project.name
        paths[project.id] = project.path
        updatedAt[project.id] = project.updatedAt
    }

    for session in sessions {
        let identity = projectIdentity(for: session)
        grouped[identity.id, default: []].append(session)
        names[identity.id] = identity.name
        paths[identity.id] = identity.path
        updatedAt[identity.id] = max(updatedAt[identity.id] ?? .distantPast, session.updatedAt)
    }

    return grouped
        .map { key, value in
            SessionProjectBucket(
                id: key,
                name: names[key] ?? key,
                path: paths[key] ?? nil,
                updatedAt: updatedAt[key],
                sessions: value.sorted { $0.updatedAt > $1.updatedAt },
                projectOnly: value.isEmpty
            )
        }
        .sorted {
            let lhsDate = $0.updatedAt ?? $0.sessions.first?.updatedAt ?? .distantPast
            let rhsDate = $1.updatedAt ?? $1.sessions.first?.updatedAt ?? .distantPast
            return lhsDate > rhsDate
        }
}

private func projectIdentity(for session: SessionSummary) -> (id: String, name: String, path: String?) {
    if let projectRoot = session.projectRoot?.trimmingCharacters(in: .whitespacesAndNewlines),
       !projectRoot.isEmpty {
        let projectName = session.projectName?.trimmingCharacters(in: .whitespacesAndNewlines)
        return (projectRoot, projectName?.isEmpty == false ? projectName! : URL(fileURLWithPath: projectRoot).lastPathComponent, projectRoot)
    }

    guard let cwd = session.cwd?.trimmingCharacters(in: .whitespacesAndNewlines), !cwd.isEmpty else {
        return ("__no_project__", "未分组", nil)
    }

    let url = URL(fileURLWithPath: cwd)
    let lastComponent = url.lastPathComponent.trimmingCharacters(in: .whitespacesAndNewlines)
    let name = lastComponent.isEmpty ? cwd : lastComponent
    return (cwd, name, cwd)
}

private enum RootPage: Hashable {
    case monitor
    case board
}

private struct MonitorPageView: View {
    @ObservedObject var state: AppState
    @Binding var bridgeURL: String

    var body: some View {
        NavigationSplitView {
            sidebar
                .navigationSplitViewColumnWidth(min: 300, ideal: 340)
        } detail: {
            detail
        }
        .navigationSplitViewStyle(.balanced)
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
                        DetailBadge(text: state.bridgeReplyAvailable ? "Desktop" : state.currentSourceKind.uppercased(), style: .neutral)
                        DetailBadge(text: state.isRunning ? "Running" : "Monitoring", style: state.isRunning ? .connected : .neutral)
                        DetailBadge(text: state.currentSourceKind.uppercased(), style: .neutral)
                        if let agentNickname = state.currentAgentNickname, !agentNickname.isEmpty {
                            DetailBadge(text: agentNickname, style: .warning)
                        }
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
                    if let gitBranch = state.currentGitBranch, !gitBranch.isEmpty {
                        MetadataPill(title: "Branch", value: gitBranch)
                    }
                    if let agentRole = state.currentAgentRole, !agentRole.isEmpty {
                        MetadataPill(title: "Agent", value: agentRole)
                    }
                    MetadataPill(title: "Thread", value: state.selectedSessionID ?? "unknown")
                }
            }

            VStack(alignment: .leading, spacing: 10) {
                if let cwd = state.currentCWD, !cwd.isEmpty {
                    ProvenanceRow(label: "Workspace", value: cwd)
                }
                if let parentThreadID = state.currentParentThreadID, !parentThreadID.isEmpty {
                    ProvenanceRow(label: "Parent Thread", value: parentThreadID)
                }
                if let rolloutPath = state.currentRolloutPath, !rolloutPath.isEmpty {
                    ProvenanceRow(label: "Rollout", value: rolloutPath)
                }
                if !state.currentSource.isEmpty, state.currentSource != state.currentSourceKind {
                    ProvenanceRow(label: "Source", value: state.currentSource)
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

private struct BoardPageView: View {
    @ObservedObject var state: BoardState
    @Binding var bridgeURL: String

    var body: some View {
        NavigationSplitView {
            boardSidebar
                .navigationSplitViewColumnWidth(min: 300, ideal: 340)
        } detail: {
            boardDetail
        }
        .navigationSplitViewStyle(.balanced)
    }

    private var boardSidebar: some View {
        ZStack {
            CodexPalette.sidebar.ignoresSafeArea()

            VStack(alignment: .leading, spacing: 18) {
                boardSidebarHeader
                boardBridgePanel
                boardList
            }
            .padding(18)
        }
    }

    private var boardSidebarHeader: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .center) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Coordination")
                        .font(.system(size: 26, weight: .semibold))
                        .foregroundStyle(CodexPalette.foreground)
                    Text("Board Monitor")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(CodexPalette.muted)
                }
                Spacer()
                ConnectionBadge(
                    text: state.errorMessage == nil ? "Synced" : "Error",
                    style: state.errorMessage == nil ? .connected : .error
                )
            }

            Text("Dedicated page for collaboration boards discovered next to this project on your Mac.")
                .font(.system(size: 13, weight: .regular))
                .foregroundStyle(CodexPalette.muted)
        }
    }

    private var boardBridgePanel: some View {
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
                    Task { await state.refreshBoards(selectFirst: true) }
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

    private var boardList: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 12) {
                ForEach(state.boards) { board in
                    Button {
                        Task { await state.loadBoard(id: board.id) }
                    } label: {
                        BoardSummaryRowView(board: board, selected: state.selectedBoardID == board.id)
                    }
                    .buttonStyle(.plain)
                }

                if state.boards.isEmpty {
                    EmptyBoardSidebarStateView()
                }
            }
            .padding(.bottom, 16)
        }
        .scrollIndicators(.hidden)
    }

    private var boardDetail: some View {
        ZStack {
            CodexPalette.canvas
                .overlay(alignment: .topLeading) {
                    Circle()
                        .fill(CodexPalette.warning.opacity(0.08))
                        .frame(width: 320, height: 320)
                        .blur(radius: 80)
                        .offset(x: -90, y: -130)
                }
                .ignoresSafeArea()

            if let board = state.board {
                ScrollView {
                    VStack(alignment: .leading, spacing: 24) {
                        BoardHeroView(board: board)
                        BoardRepoStripView(board: board)
                        BoardColumnsView(columns: board.columns)
                        BoardThreadsSectionView(threads: board.threads)
                    }
                    .padding(.horizontal, 28)
                    .padding(.vertical, 24)
                }
                .scrollIndicators(.hidden)
            } else {
                EmptyBoardDetailStateView()
            }
        }
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
                        DetailBadge(text: session.desktopThread ? "Desktop" : (session.sourceKind ?? session.source).uppercased(), style: .neutral)
                        if session.sourceKind == "subagent" {
                            DetailBadge(
                                text: session.agentRole?.isEmpty == false ? (session.agentRole ?? "SubAgent") : "SubAgent",
                                style: .warning
                            )
                        }
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
                Text(sessionRowFootnote)
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

    private var sessionRowFootnote: String {
        if session.sourceKind == "subagent" {
            let nickname = session.agentNickname?.trimmingCharacters(in: .whitespacesAndNewlines)
            if let nickname, !nickname.isEmpty {
                return nickname
            }
            return "SUBAGENT"
        }
        return session.modelProvider?.uppercased() ?? session.sourceKind?.uppercased() ?? session.source.uppercased()
    }

    private static let relative: RelativeDateTimeFormatter = {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .short
        formatter.locale = Locale(identifier: "zh_CN")
        return formatter
    }()
}

private struct BoardSummaryRowView: View {
    let board: BoardSummary
    let selected: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(board.title)
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(CodexPalette.foreground)
                        .lineLimit(2)

                    HStack(spacing: 8) {
                        DetailBadge(text: "\(board.taskCount) Tasks", style: .neutral)
                        DetailBadge(text: "\(board.totals.inProgress) Live", style: board.totals.inProgress > 0 ? .connected : .neutral)
                    }
                }

                Spacer()

                Circle()
                    .fill(board.totals.inProgress > 0 ? CodexPalette.success : CodexPalette.muted.opacity(0.35))
                    .frame(width: 8, height: 8)
                    .padding(.top, 4)
            }

            Text(board.path)
                .font(.system(size: 11, weight: .regular, design: .monospaced))
                .foregroundStyle(CodexPalette.muted)
                .lineLimit(1)

            HStack(spacing: 8) {
                BoardMiniStat(label: "B", value: board.totals.blocked, tint: CodexPalette.error)
                BoardMiniStat(label: "IP", value: board.totals.inProgress, tint: CodexPalette.success)
                BoardMiniStat(label: "TD", value: board.totals.todo, tint: CodexPalette.warning)
                BoardMiniStat(label: "DN", value: board.totals.done, tint: CodexPalette.accent)
                Spacer()
                Text(Self.relative.string(for: board.updatedAt) ?? "")
                    .font(.system(size: 11, weight: .regular))
                    .foregroundStyle(CodexPalette.subtleText)
            }
        }
        .padding(16)
        .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(CodexPalette.panel))
        .overlay(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .stroke(selected ? CodexPalette.accent : CodexPalette.border, lineWidth: 1)
        )
    }

    private static let relative: RelativeDateTimeFormatter = {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .short
        formatter.locale = Locale(identifier: "zh_CN")
        return formatter
    }()
}

private struct BoardHeroView: View {
    let board: BoardDetail

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(board.title)
                        .font(.system(size: 28, weight: .semibold))
                        .foregroundStyle(CodexPalette.foreground)
                        .fixedSize(horizontal: false, vertical: true)

                    Text(board.path)
                        .font(.system(size: 12, weight: .regular, design: .monospaced))
                        .foregroundStyle(CodexPalette.muted)
                        .textSelection(.enabled)

                    HStack(spacing: 8) {
                        DetailBadge(text: "Board", style: .neutral)
                        DetailBadge(text: "\(board.threadCount) Threads", style: .neutral)
                        DetailBadge(text: "\(board.taskCount) Tasks", style: .connected)
                    }
                }

                Spacer()

                VStack(alignment: .trailing, spacing: 8) {
                    Text("Updated")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(CodexPalette.subtleText)
                    Text(Self.timestamp.string(from: board.updatedAt))
                        .font(.system(size: 12, weight: .regular, design: .monospaced))
                        .foregroundStyle(CodexPalette.mutedBright)
                }
            }

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    MetadataPill(title: "Blocked", value: "\(board.totals.blocked)")
                    MetadataPill(title: "In Progress", value: "\(board.totals.inProgress)")
                    MetadataPill(title: "Todo", value: "\(board.totals.todo)")
                    MetadataPill(title: "Done", value: "\(board.totals.done)")
                    if let baseBranch = board.baseBranch, !baseBranch.isEmpty {
                        MetadataPill(title: "Base", value: baseBranch)
                    }
                }
            }
        }
    }

    private static let timestamp: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm"
        return formatter
    }()
}

private struct BoardRepoStripView: View {
    let board: BoardDetail

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Runtime")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(CodexPalette.muted)

            if let targetRepoRoot = board.targetRepoRoot, !targetRepoRoot.isEmpty {
                ProvenanceRow(label: "Target Repo", value: targetRepoRoot)
            }

            if let repo = board.repo {
                if let currentBranch = repo.currentBranch, !currentBranch.isEmpty {
                    ProvenanceRow(label: "Current Branch", value: currentBranch)
                }
                if let error = repo.error, !error.isEmpty {
                    Text(error)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(CodexPalette.error)
                } else {
                    HStack(spacing: 8) {
                        DetailBadge(text: repo.configured ? "Configured" : "Missing Config", style: repo.configured ? .connected : .warning)
                        if repo.dirty == true {
                            DetailBadge(text: "Dirty", style: .warning)
                        }
                        if let legacy = repo.legacyLocalBranches, !legacy.isEmpty {
                            DetailBadge(text: "\(legacy.count) Legacy Branches", style: .warning)
                        }
                    }
                }
            } else {
                Text("No coordination.config.json found. The board still loads from TASK_BOARD.md, THREADS.json, and COMM_LOG.md.")
                    .font(.system(size: 13, weight: .regular))
                    .foregroundStyle(CodexPalette.muted)
            }
        }
        .padding(18)
        .background(CodexPalette.panel, in: RoundedRectangle(cornerRadius: 22))
        .overlay(
            RoundedRectangle(cornerRadius: 22)
                .stroke(CodexPalette.border, lineWidth: 1)
        )
    }
}

private struct BoardColumnsView: View {
    let columns: [BoardColumn]

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Kanban")
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(CodexPalette.foreground)

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(alignment: .top, spacing: 18) {
                    ForEach(columns) { column in
                        BoardColumnView(column: column)
                    }
                }
                .padding(.vertical, 4)
            }
        }
    }
}

private struct BoardColumnView: View {
    let column: BoardColumn

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text(column.title)
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(CodexPalette.foreground)
                Spacer()
                DetailBadge(text: "\(column.count)", style: badgeStyle)
            }

            if column.tasks.isEmpty {
                Text("No tasks")
                    .font(.system(size: 13, weight: .regular))
                    .foregroundStyle(CodexPalette.muted)
                    .frame(maxWidth: .infinity, minHeight: 84, alignment: .center)
            } else {
                LazyVStack(alignment: .leading, spacing: 12) {
                    ForEach(column.tasks) { task in
                        BoardTaskCardView(task: task)
                    }
                }
            }
        }
        .padding(16)
        .frame(width: 320, alignment: .topLeading)
        .background(CodexPalette.panel, in: RoundedRectangle(cornerRadius: 22))
        .overlay(
            RoundedRectangle(cornerRadius: 22)
                .stroke(CodexPalette.border, lineWidth: 1)
        )
    }

    private var badgeStyle: DetailBadge.Style {
        switch column.status {
        case "BLOCKED":
            return .error
        case "IN_PROGRESS":
            return .connected
        case "DONE":
            return .neutral
        default:
            return .warning
        }
    }
}

private struct BoardTaskCardView: View {
    let task: BoardTask

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(task.title)
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(CodexPalette.foreground)
                        .fixedSize(horizontal: false, vertical: true)

                    HStack(spacing: 8) {
                        DetailBadge(text: task.displayName, style: .neutral)
                        DetailBadge(text: task.statusLabel, style: task.statusStyle)
                    }
                }

                Spacer()
            }

            Text(task.role)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(CodexPalette.mutedBright)

            if task.dependsOn != "-" {
                Text("Depends on \(task.dependsOn)")
                    .font(.system(size: 12, weight: .regular))
                    .foregroundStyle(CodexPalette.muted)
            }

            Text(task.output)
                .font(.system(size: 12, weight: .regular))
                .foregroundStyle(CodexPalette.muted)
                .fixedSize(horizontal: false, vertical: true)

            if let latestLog = task.latestLog {
                HStack(spacing: 8) {
                    DetailBadge(text: latestLog.type.uppercased(), style: boardLogStyle(latestLog.type))
                    Text(latestLog.timestamp)
                        .font(.system(size: 11, weight: .regular, design: .monospaced))
                        .foregroundStyle(CodexPalette.subtleText)
                }
                Text(latestLog.message)
                    .font(.system(size: 12, weight: .regular))
                    .foregroundStyle(CodexPalette.mutedBright)
                    .lineLimit(3)
            } else if let runtime = task.runtime, let preview = runtime.lastMessagePreview, !preview.isEmpty {
                Text(preview)
                    .font(.system(size: 12, weight: .regular))
                    .foregroundStyle(CodexPalette.mutedBright)
                    .lineLimit(3)
            }

            if let runtime = task.runtime {
                HStack(spacing: 8) {
                    DetailBadge(
                        text: runtime.running ? "Live" : (runtime.stale ? "Stale" : "Recent"),
                        style: runtime.running ? .connected : (runtime.stale ? .warning : .neutral)
                    )
                    if runtime.subagentCount > 0 {
                        DetailBadge(text: "\(runtime.subagentCount) SubAgents", style: .warning)
                    }
                }
            }

            if let branches = task.branches {
                Text("Local \(branches.local.count) · Remote \(branches.remote.count)")
                    .font(.system(size: 11, weight: .regular, design: .monospaced))
                    .foregroundStyle(CodexPalette.subtleText)
            } else if let runtime = task.runtime, let gitBranch = runtime.gitBranch, !gitBranch.isEmpty {
                Text(gitBranch)
                    .font(.system(size: 11, weight: .regular, design: .monospaced))
                    .foregroundStyle(CodexPalette.subtleText)
            }
        }
        .padding(14)
        .background(CodexPalette.sidebar.opacity(0.72), in: RoundedRectangle(cornerRadius: 18))
        .overlay(
            RoundedRectangle(cornerRadius: 18)
                .stroke(CodexPalette.border, lineWidth: 1)
        )
    }
}

private struct BoardThreadsSectionView: View {
    let threads: [BoardThread]

    private let columns = [GridItem(.adaptive(minimum: 280), spacing: 16)]

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Threads")
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(CodexPalette.foreground)

            LazyVGrid(columns: columns, alignment: .leading, spacing: 16) {
                ForEach(threads) { thread in
                    BoardThreadCardView(thread: thread)
                }
            }
        }
    }
}

private struct BoardThreadCardView: View {
    let thread: BoardThread

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("\(thread.slot) · \(thread.displayName)")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(CodexPalette.foreground)
                    Text(thread.role)
                        .font(.system(size: 12, weight: .regular))
                        .foregroundStyle(CodexPalette.muted)
                }
                Spacer()
                if let runtime = thread.runtime {
                    DetailBadge(
                        text: runtime.running ? "Live" : (runtime.stale ? "Stale" : "Recent"),
                        style: runtime.running ? .connected : (runtime.stale ? .warning : .neutral)
                    )
                }
                if let task = thread.task {
                    DetailBadge(text: task.statusLabel, style: task.statusStyle)
                } else {
                    DetailBadge(text: "Idle", style: .neutral)
                }
            }

            if let task = thread.task {
                Text(task.title)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(CodexPalette.mutedBright)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let lastLog = thread.lastLog {
                HStack(spacing: 8) {
                    DetailBadge(text: lastLog.type.uppercased(), style: boardLogStyle(lastLog.type))
                    Text(lastLog.timestamp)
                        .font(.system(size: 11, weight: .regular, design: .monospaced))
                        .foregroundStyle(CodexPalette.subtleText)
                }
                Text(lastLog.message)
                    .font(.system(size: 12, weight: .regular))
                    .foregroundStyle(CodexPalette.muted)
                    .lineLimit(3)
            } else if let runtime = thread.runtime, let preview = runtime.lastMessagePreview, !preview.isEmpty {
                Text(preview)
                    .font(.system(size: 12, weight: .regular))
                    .foregroundStyle(CodexPalette.muted)
                    .lineLimit(3)
            }

            HStack(spacing: 12) {
                if let lastInvocation = thread.lastInvocation {
                    Text(lastInvocation.elapsedText)
                        .font(.system(size: 11, weight: .regular, design: .monospaced))
                        .foregroundStyle(CodexPalette.subtleText)
                }

                if let branches = thread.branches {
                    Text("L\(branches.local.count)/R\(branches.remote.count)")
                        .font(.system(size: 11, weight: .regular, design: .monospaced))
                        .foregroundStyle(CodexPalette.subtleText)
                } else if let runtime = thread.runtime {
                    Text("S\(runtime.sessionCount)/A\(runtime.subagentCount)")
                        .font(.system(size: 11, weight: .regular, design: .monospaced))
                        .foregroundStyle(CodexPalette.subtleText)
                }

                if let runtime = thread.runtime, let gitBranch = runtime.gitBranch, !gitBranch.isEmpty {
                    Text(gitBranch)
                        .font(.system(size: 11, weight: .regular, design: .monospaced))
                        .foregroundStyle(CodexPalette.subtleText)
                        .lineLimit(1)
                }
            }
        }
        .padding(16)
        .background(CodexPalette.panel, in: RoundedRectangle(cornerRadius: 20))
        .overlay(
            RoundedRectangle(cornerRadius: 20)
                .stroke(CodexPalette.border, lineWidth: 1)
        )
    }
}

private func boardLogStyle(_ kind: String) -> DetailBadge.Style {
    switch kind.lowercased() {
    case "blocker":
        return .error
    case "kickoff":
        return .connected
    case "handoff":
        return .warning
    default:
        return .neutral
    }
}

private struct BoardMiniStat: View {
    let label: String
    let value: Int
    let tint: Color

    var body: some View {
        Text("\(label) \(value)")
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(tint)
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(tint.opacity(0.14), in: Capsule())
    }
}

private struct EmptyBoardSidebarStateView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("No boards found")
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(CodexPalette.foreground)
            Text("The bridge looks for sibling folders with TASK_BOARD.md, THREADS.json, and COMM_LOG.md.")
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

private struct EmptyBoardDetailStateView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Select a board")
                .font(.system(size: 28, weight: .semibold))
                .foregroundStyle(CodexPalette.foreground)
            Text("This page reads the real collaboration board files from your Mac through the bridge, separately from the desktop thread monitor.")
                .font(.system(size: 15, weight: .regular))
                .foregroundStyle(CodexPalette.muted)
                .frame(maxWidth: 580, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
        .padding(40)
    }
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
        VStack(alignment: .leading, spacing: 6) {
            if message.role == "user" {
                rowLabel(title: labelText, tint: labelColor)
            }
            MarkdownTextView(
                text: message.text,
                baseFont: .system(size: message.role == "user" ? 18 : 17, weight: message.role == "user" ? .semibold : .regular),
                foreground: CodexPalette.foreground,
                lineSpacing: 4
            )
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: 920, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var commentaryRow: some View {
        MarkdownTextView(
            text: message.text,
            baseFont: .system(size: 14, weight: .regular),
            foreground: CodexPalette.mutedBright,
            lineSpacing: 3
        )
        .frame(maxWidth: 920, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var toolRow: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let leading = toolLeadingLine {
                Text(leading)
                    .font(.system(size: 13, weight: .regular))
                    .foregroundStyle(CodexPalette.subtleText)
            }

            if let detail = toolDetailText, !detail.isEmpty {
                MarkdownTextView(
                    text: detail,
                    baseFont: .system(size: 13, weight: .regular),
                    foreground: CodexPalette.muted,
                    lineSpacing: 2
                )
            }
        }
        .frame(maxWidth: 920, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: .leading)
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
            return "你"
        case "assistant":
            return "Codex"
        default:
            return "系统"
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

    private var toolLeadingLine: String? {
        let tool = message.toolName ?? "tool"
        let lines = message.text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        let firstLine = lines.first ?? ""
        if tool == "exec_command" || tool == "write_stdin" {
            return firstLine.isEmpty ? "已运行命令" : "已运行 \(firstLine)"
        }
        if tool == "apply_patch" {
            return "已应用补丁"
        }
        if tool == "wait_agent" {
            return "已等待子 Agent"
        }
        if tool == "spawn_agent" {
            return "已创建子 Agent"
        }
        if tool == "send_input" {
            return "已向子 Agent 发送输入"
        }
        if tool == "close_agent" {
            return "已关闭子 Agent"
        }
        if tool == "web_search" {
            return firstLine.isEmpty ? "已执行网页检索" : "已执行 \(firstLine)"
        }
        return "已运行 \(tool)"
    }

    private var toolDetailText: String? {
        let lines = message.text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        guard !lines.isEmpty else { return nil }
        if message.toolName == "exec_command" || message.toolName == "write_stdin" || message.toolName == "web_search" {
            let trailing = Array(lines.dropFirst()).joined(separator: "\n")
            return trailing.isEmpty ? nil : trailing
        }
        if message.toolName == "apply_patch" {
            return nil
        }
        return message.text
    }

    private static let time: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm"
        return formatter
    }()
}

private struct MarkdownTextView: View {
    let text: String
    let baseFont: Font
    let foreground: Color
    let lineSpacing: CGFloat

    private var segments: [MarkdownSegment] {
        MarkdownSegment.parse(text)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(Array(segments.enumerated()), id: \.offset) { _, segment in
                switch segment {
                case .markdown(let value):
                    markdownSection(value)
                case .codeBlock(let code, let language):
                    codeSection(code: code, language: language)
                }
            }
        }
        .tint(CodexPalette.accent)
        .textSelection(.enabled)
    }

    @ViewBuilder
    private func markdownSection(_ value: String) -> some View {
        if let attributed = try? AttributedString(
            markdown: value,
            options: AttributedString.MarkdownParsingOptions(
                interpretedSyntax: .full,
                failurePolicy: .returnPartiallyParsedIfPossible
            )
        ) {
            Text(attributed)
                .font(baseFont)
                .foregroundStyle(foreground)
                .lineSpacing(lineSpacing)
                .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            Text(value)
                .font(baseFont)
                .foregroundStyle(foreground)
                .lineSpacing(lineSpacing)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func codeSection(code: String, language: String?) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if let language, !language.isEmpty {
                Text(language)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(CodexPalette.subtleText)
                    .textCase(.uppercase)
            }

            ScrollView(.horizontal, showsIndicators: false) {
                Text(code)
                    .font(.system(size: 13, weight: .regular, design: .monospaced))
                    .foregroundStyle(CodexPalette.foreground)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }
        }
        .padding(14)
        .background(CodexPalette.sidebar.opacity(0.72), in: RoundedRectangle(cornerRadius: 18))
        .overlay(
            RoundedRectangle(cornerRadius: 18)
                .stroke(CodexPalette.border, lineWidth: 1)
        )
    }
}

private enum MarkdownSegment: Hashable {
    case markdown(String)
    case codeBlock(String, language: String?)

    static func parse(_ source: String) -> [MarkdownSegment] {
        guard !source.isEmpty else { return [.markdown("")] }

        let normalized = source.replacingOccurrences(of: "\r\n", with: "\n")
        let lines = normalized.split(separator: "\n", omittingEmptySubsequences: false)

        var segments: [MarkdownSegment] = []
        var markdownBuffer: [String] = []
        var codeBuffer: [String] = []
        var inCodeBlock = false
        var codeLanguage: String?

        func flushMarkdown() {
            guard !markdownBuffer.isEmpty else { return }
            let value = markdownBuffer.joined(separator: "\n")
            if !value.isEmpty {
                segments.append(.markdown(value))
            }
            markdownBuffer.removeAll(keepingCapacity: true)
        }

        func flushCode() {
            let value = codeBuffer.joined(separator: "\n")
            segments.append(.codeBlock(value, language: codeLanguage))
            codeBuffer.removeAll(keepingCapacity: true)
            codeLanguage = nil
        }

        for line in lines {
            let rawLine = String(line)
            if rawLine.hasPrefix("```") {
                let marker = String(rawLine.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                if inCodeBlock {
                    flushCode()
                    inCodeBlock = false
                } else {
                    flushMarkdown()
                    inCodeBlock = true
                    codeLanguage = marker.isEmpty ? nil : marker
                }
                continue
            }

            if inCodeBlock {
                codeBuffer.append(rawLine)
            } else {
                markdownBuffer.append(rawLine)
            }
        }

        if inCodeBlock {
            flushCode()
        } else {
            flushMarkdown()
        }

        return segments.isEmpty ? [.markdown(normalized)] : segments
    }
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

private struct ComposerMenuLabel: View {
    let title: String
    let systemImage: String
    let tint: Color

    var body: some View {
        Label(title, systemImage: systemImage)
            .labelStyle(.titleAndIcon)
            .font(.system(size: 14, weight: .medium))
            .foregroundStyle(tint)
    }
}

private struct DraftAttachmentChip: View {
    let attachment: DraftAttachment
    let onRemove: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: attachment.isImage ? "photo" : "doc")
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(attachment.isImage ? CodexPalette.accent : CodexPalette.mutedBright)

            VStack(alignment: .leading, spacing: 2) {
                Text(attachment.displayName)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(CodexPalette.foreground)
                    .lineLimit(1)
                Text(ByteCountFormatter.string(fromByteCount: attachment.byteCount, countStyle: .file))
                    .font(.system(size: 11, weight: .regular))
                    .foregroundStyle(CodexPalette.subtleText)
            }

            Button(action: onRemove) {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(CodexPalette.muted)
                    .frame(width: 20, height: 20)
                    .background(CodexPalette.canvas, in: Circle())
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .background(CodexPalette.canvas, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(CodexPalette.border, lineWidth: 1)
        )
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

private extension BoardTask {
    var statusLabel: String {
        switch status {
        case "IN_PROGRESS":
            return "In Progress"
        case "BLOCKED":
            return "Blocked"
        case "DONE":
            return "Done"
        default:
            return "Todo"
        }
    }

    var statusStyle: DetailBadge.Style {
        switch status {
        case "IN_PROGRESS":
            return .connected
        case "BLOCKED":
            return .error
        case "DONE":
            return .neutral
        default:
            return .warning
        }
    }
}

private extension BoardInvocation {
    var elapsedText: String {
        let minutes = elapsedSeconds / 60
        if minutes > 0 {
            return "\(minutes)m"
        }
        return "\(elapsedSeconds)s"
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
            .foregroundStyle(Color.white)
            .background(
                Circle()
                    .fill(CodexPalette.accent.opacity(configuration.isPressed ? 0.75 : 1))
            )
    }
}

private enum CodexPalette {
    static let canvas = Color(hex: 0xFFFFFF)
    static let sidebar = Color(hex: 0xF5F4F1)
    static let panel = Color(hex: 0xFCFBF8)
    static let border = Color(hex: 0xE6E2DB)
    static let foreground = Color(hex: 0x171717)
    static let mutedBright = Color(hex: 0x444444)
    static let muted = Color(hex: 0x7E7E7E)
    static let subtleText = Color(hex: 0xA3A3A3)
    static let accent = Color(hex: 0x0B6CF0)
    static let success = Color(hex: 0x22A55A)
    static let warning = Color(hex: 0xB98500)
    static let error = Color(hex: 0xD94747)
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
