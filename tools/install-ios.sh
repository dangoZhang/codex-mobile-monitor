#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DERIVED_DATA="${ROOT_DIR}/dist/DerivedData"
SCHEME="CodeXMobile"
PROJECT="${ROOT_DIR}/CodeXMobile.xcodeproj"
APP_PATH="${DERIVED_DATA}/Build/Products/Debug-iphoneos/CodeXMobile.app"

if ! command -v xcodebuild >/dev/null 2>&1; then
  echo "xcodebuild not found. Install Xcode first." >&2
  exit 1
fi

if ! command -v xcrun >/dev/null 2>&1; then
  echo "xcrun not found. Install Xcode command line tools first." >&2
  exit 1
fi

TEAM_ID="${DEVELOPMENT_TEAM:-${TEAM_ID:-}}"
if [[ -z "${TEAM_ID}" ]]; then
  echo "Set DEVELOPMENT_TEAM or TEAM_ID to your Apple development team id." >&2
  exit 1
fi

DEVICE_ID="${DEVICE_ID:-}"
if [[ -z "${DEVICE_ID}" ]]; then
  DEVICE_ID="$(xcrun devicectl list devices 2>/dev/null | awk '/Connected/ && /iPhone|iPad/ {print $NF; exit}')"
fi

if [[ -z "${DEVICE_ID}" ]]; then
  echo "No connected iPhone/iPad found. Connect a device, unlock it, and enable Developer Mode." >&2
  exit 1
fi

mkdir -p "${ROOT_DIR}/dist"

xcodebuild \
  -project "${PROJECT}" \
  -scheme "${SCHEME}" \
  -configuration Debug \
  -destination "generic/platform=iOS" \
  -derivedDataPath "${DERIVED_DATA}" \
  DEVELOPMENT_TEAM="${TEAM_ID}" \
  CODE_SIGN_STYLE=Automatic \
  -allowProvisioningUpdates \
  build

if [[ ! -d "${APP_PATH}" ]]; then
  echo "Built app not found at ${APP_PATH}" >&2
  exit 1
fi

xcrun devicectl device install app --device "${DEVICE_ID}" "${APP_PATH}"
xcrun devicectl device process launch --device "${DEVICE_ID}" io.github.codexmobile.monitor

echo
echo "Installed ${SCHEME} on device ${DEVICE_ID}."
