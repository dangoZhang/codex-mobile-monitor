# Codex Mobile Monitor

An iPhone and iPad client for supervising real Codex Desktop work running on a Mac.

This project does not fake a terminal chat UI. The Mac bridge reads Codex Desktop's own local state:

- thread metadata from `~/.codex/state_5.sqlite`
- live thread content from each thread's `rollout_path` JSONL log

The iOS app then renders those threads in a Codex-style split view and can optionally resume the same desktop thread when replies are enabled.

## What It Does

- Lists real Codex Desktop threads already running on your Mac
- Shows user messages, assistant messages, commentary, and tool activity
- Streams live updates over SSE
- Supports both iPhone and iPad layouts
- Keeps thread provenance visible: workspace path, rollout path, provider, CLI version
- Can reply back into the same desktop thread instead of creating a fake parallel CLI session

## Architecture

### Mac bridge

`bridge/bridge_server.py`

- Reads `threads` from `~/.codex/state_5.sqlite`
- Filters desktop sources such as `vscode` and `app`
- Parses rollout JSONL incrementally
- Exposes a small HTTP + SSE API for the mobile client

### iOS client

`CodeXMobile/`

- SwiftUI app with a two-column monitor layout
- Uses Codex Dark inspired colors and activity presentation
- Distinguishes normal messages, commentary, and tool calls

## Why This Is Different From a CLI Wrapper

This app is built around Codex Desktop's persisted thread data, not around scraping terminal output or opening a fake standalone CLI chat.

The thread list comes from Codex Desktop's SQLite database. Message history and live updates come from the same rollout logs the desktop app writes. The mobile app is therefore supervising the same work the desktop app is already running.

## Project Layout

```text
CodeXMobile.xcodeproj
CodeXMobile/
bridge/
requirements.txt
```

## Requirements

- macOS with Codex Desktop / CLI already installed and working
- Python 3.11+
- Xcode 16+
- iPhone or iPad on the same local network as the Mac

## Mac Setup

Create a virtual environment and install the bridge dependency:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Start the bridge:

```bash
./.venv/bin/python bridge/bridge_server.py
```

By default it listens on:

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
```

## iOS Setup

1. Open `CodeXMobile.xcodeproj` in Xcode
2. Select your iPhone or iPad target
3. Build and run
4. Enter your Mac bridge URL, for example:

```text
http://your-mac.local:8765
```

or:

```text
http://192.168.x.x:8765
```

5. The sidebar will load existing Codex Desktop threads automatically
6. Select a thread to monitor its current work in real time

## API

- `GET /healthz`
- `GET /api/sessions`
- `GET /api/sessions/{id}`
- `POST /api/sessions/{id}/messages`
- `GET /api/sessions/{id}/events?after=<seq>`

## Privacy Notes

- This repository is prepared for publishing without local machine paths or personal bundle identifiers in the main project files.
- The public project is scoped to the mobile monitor and bridge only.
- Local, unrelated directories and temporary environments are excluded from version control.

## Current Limitations

- The bridge depends on Codex Desktop's local SQLite and rollout log formats
- The Mac and iOS device must be on the same network
- Only one active resume/send operation is allowed per session at a time

## Verification

The project has been verified with:

- `python3 -m py_compile bridge/bridge_server.py`
- `xcrun swiftc -sdk "$(xcrun --sdk iphoneos --show-sdk-path)" -target arm64-apple-ios17.0 -typecheck CodeXMobile/*.swift`
- `xcodebuild -project CodeXMobile.xcodeproj -scheme CodeXMobile -configuration Debug -destination 'generic/platform=iOS' CODE_SIGNING_ALLOWED=NO build`

## License

No license is included by default. Add one before making the repository public if you want to grant reuse rights.
