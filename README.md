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

## Installation / 安装流程

### A. Prepare the Mac / 准备 Mac

English:

1. Install and open Codex Desktop at least once on the Mac
2. Install Xcode 16 or newer
3. Open Xcode once so it can finish first-launch setup
4. Make sure Python 3.11+ is available in Terminal

中文：

1. 先在 Mac 上安装并至少启动过一次 Codex Desktop
2. 安装 Xcode 16 或更新版本
3. 至少打开一次 Xcode，让它完成首次初始化
4. 确保终端里可用 Python 3.11+

### B. Prepare Xcode Signing / 配置 Xcode 签名

English:

1. Open Xcode
2. Go to `Xcode -> Settings -> Accounts`
3. Sign in with your Apple ID
4. Open `CodeXMobile.xcodeproj`
5. Select the `CodeXMobile` target
6. Open `Signing & Capabilities`
7. Keep `Automatically manage signing` enabled
8. Choose your `Personal Team` or paid team in `Team`
9. Wait for Xcode to generate the provisioning profile once

中文：

1. 打开 Xcode
2. 进入 `Xcode -> 设置 -> Accounts`
3. 登录你的 Apple ID
4. 打开 `CodeXMobile.xcodeproj`
5. 选中 `CodeXMobile` target
6. 打开 `Signing & Capabilities`
7. 保持 `Automatically manage signing` 开启
8. 在 `Team` 里选择你的 `Personal Team` 或付费团队
9. 等 Xcode 首次生成好 provisioning profile

### C. Prepare the iPhone or iPad / 准备 iPhone 或 iPad

English:

1. Connect the device to the Mac with a cable
2. Unlock the device
3. Tap `Trust This Computer` if prompted
4. Enable `Developer Mode` on the device:
   `Settings -> Privacy & Security -> Developer Mode`
5. Reboot the device if iOS asks for it
6. Reconnect the device after reboot

中文：

1. 用数据线把设备连接到 Mac
2. 保持设备解锁
3. 如果有提示，点 `信任此电脑`
4. 在设备上开启 `开发者模式`：
   `设置 -> 隐私与安全性 -> 开发者模式`
5. 如果 iOS 要求重启，就按提示重启
6. 重启后重新连接设备

### D. Start the Mac Bridge / 启动 Mac Bridge

English:

```bash
./tools/run-bridge.sh
```

中文：

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
CODEX_BRIDGE_BOARD_FOLDERS=/absolute/path/to/folder-one,/absolute/path/to/folder-two
CODEX_BRIDGE_BOARD_ROOTS=/absolute/path/to/board-one,/absolute/path/to/board-two
```

### E. Install the App from Terminal / 用终端安装 App

English:

After Xcode signing is ready, run:

```bash
DEVELOPMENT_TEAM=YOUR_TEAM_ID ./tools/install-ios.sh
```

The script will:

- build the app for the connected physical device
- use Xcode automatic signing
- enable developer disk image services when available
- install the app with `devicectl`
- launch the app after install

中文：

当 Xcode 签名准备好后，运行：

```bash
DEVELOPMENT_TEAM=你的TEAM_ID ./tools/install-ios.sh
```

脚本会：

- 为已连接真机构建应用
- 使用 Xcode 自动签名
- 在可用时启用 developer disk image services
- 通过 `devicectl` 安装到设备
- 安装后自动启动

### F. Trust the Developer Certificate on First Install / 首次安装后信任开发者证书

English:

If the app installs but does not open, trust the developer certificate on the device:

1. Open `Settings -> General -> VPN & Device Management`
2. Find your Apple Development certificate
3. Tap `Trust`
4. Return to the Home Screen and open `CodeXMobile`

中文：

如果 App 已安装但无法打开，需要在设备上手动信任开发者证书：

1. 打开 `设置 -> 通用 -> VPN与设备管理`
2. 找到你的 Apple Development 证书
3. 点击 `信任`
4. 回到桌面重新打开 `CodeXMobile`

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

## Troubleshooting / 故障排查

English:

- `No connected iPhone/iPad found`
  Check cable, unlock the device, trust the Mac, and make sure Developer Mode is enabled.
- `Set DEVELOPMENT_TEAM or TEAM_ID`
  Open Xcode once and select a valid Team in `Signing & Capabilities`.
- App installs but does not launch
  Trust the developer certificate in `VPN & Device Management`.
- The iPhone cannot reach the Mac bridge
  Verify the Mac and iPhone are on the same network, and test `http://YOUR_MAC_IP:8765/healthz`.
- Board page does not show the expected project
  Set `CODEX_BRIDGE_BOARD_FOLDERS` or `CODEX_BRIDGE_BOARD_ROOTS` before starting the bridge.

中文：

- `No connected iPhone/iPad found`
  检查数据线、设备是否解锁、是否已信任这台 Mac、是否已开启开发者模式。
- `Set DEVELOPMENT_TEAM or TEAM_ID`
  先用 Xcode 打开工程，并在 `Signing & Capabilities` 里选好 Team。
- App 已安装但无法启动
  去 `VPN与设备管理` 信任开发者证书。
- iPhone 连不到 Mac bridge
  确认 Mac 和 iPhone 在同一网络，并测试 `http://你的Mac局域网IP:8765/healthz`。
- 看板页没有你想要的项目
  启动 bridge 前配置 `CODEX_BRIDGE_BOARD_FOLDERS` 或 `CODEX_BRIDGE_BOARD_ROOTS`。

## API / 接口

- `GET /healthz`
- `GET /api/sessions`
- `GET /api/projects`
- `GET /api/board-folders`
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
