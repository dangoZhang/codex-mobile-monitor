# Codex Mobile Monitor

English | 中文

Codex Mobile Monitor is an iPhone and iPad client for supervising real Codex Desktop work running on a Mac.

It does not fake a terminal UI. The Mac bridge reads Codex Desktop's own local state and rollout logs, then streams the same thread content to iOS.

Codex Mobile Monitor 是一个 iPhone / iPad 客户端，用来监督 Mac 上真实运行中的 Codex Desktop 工作。

它不是 CLI 聊天壳。Mac 端 bridge 直接读取 Codex Desktop 自己的本地线程状态和 rollout 日志，再把同一份内容同步到 iOS。

## Highlights / 特性

- Reads real desktop threads from Codex local state instead of scraping terminal output
- Streams user messages, assistant messages, commentary, and tool activity
- Uses separate iPhone and iPad layouts: compact on iPhone, full sidebar on iPad
- Groups threads by project folder and keeps a dedicated Boards page
- Can resume the same desktop thread when mobile reply is allowed
- 从 Codex 本地状态读取真实桌面线程，而不是抓取终端文本
- 流式展示用户消息、助手消息、commentary 与工具执行
- iPhone 使用瘦身布局，iPad 使用完整侧栏布局
- 按项目文件夹分组线程，并保留独立看板页面
- 在允许回写时，可以继续同一个桌面线程

## How It Works / 工作原理

Mac bridge:

- reads thread metadata from Codex Desktop's local thread database
- parses each thread `rollout_path` JSONL log incrementally
- exposes HTTP + SSE endpoints for iOS
- exposes recent project roots so the mobile sidebar stays close to Codex Desktop

iOS client:

- renders the same thread content on iPhone and iPad
- keeps project grouping and live thread updates
- uses a separate Boards page for sibling collaboration projects

Mac bridge：

- 从 Codex Desktop 本地线程数据库读取元数据
- 增量解析每条线程的 `rollout_path` JSONL 日志
- 对 iOS 暴露 HTTP + SSE 接口
- 额外暴露最近项目根目录，使移动端侧栏更接近 Codex Desktop

iOS 客户端：

- 在 iPhone / iPad 上渲染同一份线程内容
- 保留项目分组与实时线程更新
- 用独立 Boards 页面承载同目录协作项目

## Requirements / 环境要求

- macOS with Codex Desktop already installed and used locally
- Python 3.11+
- Xcode 16+
- iPhone or iPad

## Quick Start / 快速开始

### 1. Run the Mac bridge / 启动 Mac bridge

```bash
./tools/run-bridge.sh
```

Default bind:

```text
0.0.0.0:8765
```

Useful environment variables:

```bash
PORT=8765
CODEX_BRIDGE_BIND=0.0.0.0
CODEX_BRIDGE_CWD="$(pwd)"
CODEX_BRIDGE_IMPORT_LIMIT=60
CODEX_BRIDGE_POLL_SECONDS=2
CODEX_BRIDGE_ALLOWED_SOURCES=vscode,app
CODEX_BRIDGE_BOARD_ROOTS=/absolute/path/to/board-one,/absolute/path/to/board-two
```

### 2. Install to iPhone or iPad / 安装到 iPhone 或 iPad

If your Apple ID is already added in Xcode, this is the closest thing to one-click install:

```bash
DEVELOPMENT_TEAM=YOUR_TEAM_ID ./tools/install-ios.sh
```

What it does:

- builds the app for a connected physical device
- uses Xcode automatic signing
- installs the app with `devicectl`
- launches the app after install

If `install-ios.sh` cannot sign on your machine, open `CodeXMobile.xcodeproj` in Xcode once, select your Personal Team, then run the script again.

如果你的 Apple ID 已经登录到 Xcode，这基本就是当前项目的“一键安装”方式：

```bash
DEVELOPMENT_TEAM=你的 Team ID ./tools/install-ios.sh
```

脚本会：

- 为已连接真机构建应用
- 使用 Xcode 自动签名
- 通过 `devicectl` 安装到设备
- 安装后自动启动

如果脚本因为签名失败，先用 Xcode 打开 `CodeXMobile.xcodeproj`，为 target 选一次 Personal Team，再重新运行脚本。

## Connect the App to Your Mac / 让 App 连接你的 Mac

The app needs the bridge URL. Three practical options:

### Option A: Local network / 局域网

Best for home or office use.

1. Keep the Mac bridge running
2. Put the iPhone/iPad and Mac on the same LAN
3. Find your Mac IP, for example:

```bash
ipconfig getifaddr en0
```

4. In the app, enter:

```text
http://YOUR_MAC_IP:8765
```

适合家里或办公室同一网络环境，延迟最低，配置最简单。

### Option B: Tailscale

Best when the phone is outside the local network.

1. Install Tailscale on the Mac and the iPhone/iPad
2. Join the same tailnet
3. Start the bridge on the Mac
4. In the app, use the Mac's Tailscale IP or MagicDNS hostname:

```text
http://YOUR-MAC-NAME.tailnet-name.ts.net:8765
```

适合不在同一局域网时远程监督 Mac 上的 Codex 工作。

### Option C: OpenClaw or your own tunnel / OpenClaw 或自有隧道

If you already run OpenClaw or another secure reverse proxy in your workflow, you can expose the bridge through that channel instead of raw LAN.

Recommended constraints:

- keep the bridge behind auth or a private network
- do not expose it directly to the public internet without access control
- prefer a tunnel that only your own devices can reach

如果你已经在用 OpenClaw 或自建安全隧道，也可以把 bridge 通过那个通道暴露出来，而不是直接公网开放。

建议：

- 放在鉴权之后或私有网络中
- 不要无保护地直接暴露到公网
- 优先使用只有你自己设备可达的隧道

## API / 接口

- `GET /healthz`
- `GET /api/sessions`
- `GET /api/projects`
- `GET /api/sessions/{id}`
- `POST /api/sessions/{id}/messages`
- `GET /api/sessions/{id}/events?after=<seq>`
- `GET /api/boards`
- `GET /api/boards/{id}`

## Privacy / 隐私

- The published repository excludes local environments, coordination workspaces, and temporary build output
- The bridge reads your own local Codex data on your Mac; that data is not included in this repository
- Example addresses in this README are placeholders, not real machine addresses
- 公开仓库已排除本地环境、协作目录和临时构建产物
- bridge 读取的是你自己 Mac 上的本地 Codex 数据，这些数据不会进入本仓库
- README 中的地址示例都是占位符，不是你的真实机器地址

## Release / 发布说明

This repository can be published publicly, but a truly universal public iOS download still depends on Apple signing.

Current release shape:

- public GitHub repo
- GitHub Release with source code and simulator build artifact
- local one-command device install via `./tools/install-ios.sh`
- optional self-hosted bridge access over LAN, Tailscale, or your own secure tunnel

What is not included by default:

- App Store release
- TestFlight distribution
- universally signed IPA for arbitrary devices

如果要做到真正意义上的“任何人一键下载安装 iOS App”，最终仍然取决于 Apple 的分发签名体系。

当前仓库提供的是：

- 公开 GitHub 仓库
- GitHub Release 和模拟器构建产物
- 通过 `./tools/install-ios.sh` 的本地一键装机路径
- 可通过局域网、Tailscale 或自有安全隧道连接到你的 Mac bridge

默认不包含：

- App Store 发布
- TestFlight 分发
- 对任意设备通用可安装的签名 IPA

## Validation / 验证

- `python3 -m py_compile bridge/bridge_server.py`
- `xcrun swiftc -sdk "$(xcrun --sdk iphoneos --show-sdk-path)" -target arm64-apple-ios17.0 -typecheck CodeXMobile/*.swift`
- `xcodebuild -project CodeXMobile.xcodeproj -scheme CodeXMobile -configuration Debug -destination 'generic/platform=iOS Simulator' -derivedDataPath /tmp/CodeXMobileSim build`

## Project Layout / 项目结构

```text
.github/workflows/release.yml
CodeXMobile.xcodeproj
CodeXMobile/
bridge/
tools/
requirements.txt
```
