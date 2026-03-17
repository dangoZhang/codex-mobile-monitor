#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DERIVED_DATA="${ROOT_DIR}/dist/DerivedData"
SCHEME="CodeXMobile"
PROJECT="${ROOT_DIR}/CodeXMobile.xcodeproj"
APP_PATH="${DERIVED_DATA}/Build/Products/Debug-iphoneos/CodeXMobile.app"
DEVICE_JSON="$(mktemp)"
cleanup() {
  rm -f "${DEVICE_JSON}"
}
trap cleanup EXIT

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
  xcrun devicectl list devices --json-output "${DEVICE_JSON}" >/dev/null
  DEVICE_ID="$(python3 - "${DEVICE_JSON}" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    payload = json.load(fh)

devices = payload.get("result", {}).get("devices", [])

for device in devices:
    platform = device.get("hardwareProperties", {}).get("platform")
    reality = device.get("hardwareProperties", {}).get("reality")
    device_type = device.get("hardwareProperties", {}).get("deviceType")
    developer_mode = device.get("deviceProperties", {}).get("developerModeStatus")
    udid = device.get("hardwareProperties", {}).get("udid")
    if platform == "iOS" and reality == "physical" and device_type in {"iPhone", "iPad"} and developer_mode == "enabled" and udid:
        print(udid)
        raise SystemExit(0)
PY
)"
fi

if [[ -z "${DEVICE_ID}" ]]; then
  DEVICE_HINT="$(python3 - "${DEVICE_JSON}" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    payload = json.load(fh)

devices = payload.get("result", {}).get("devices", [])
for device in devices:
    platform = device.get("hardwareProperties", {}).get("platform")
    reality = device.get("hardwareProperties", {}).get("reality")
    device_type = device.get("hardwareProperties", {}).get("deviceType")
    if platform != "iOS" or reality != "physical" or device_type not in {"iPhone", "iPad"}:
        continue
    name = device.get("deviceProperties", {}).get("name", "iPhone/iPad")
    developer_mode = device.get("deviceProperties", {}).get("developerModeStatus", "unknown")
    ddi = device.get("deviceProperties", {}).get("ddiServicesAvailable", False)
    print(f"Detected {name}, but Developer Mode is {developer_mode} and DDI available is {str(ddi).lower()}.")
    raise SystemExit(0)
PY
)"
  if [[ -n "${DEVICE_HINT}" ]]; then
    echo "${DEVICE_HINT}" >&2
  fi
  echo "No installable iPhone/iPad found. Unlock the device, enable Developer Mode, reconnect it, and try again." >&2
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
